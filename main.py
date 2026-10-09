import asyncio
import aiohttp
import os
import time
import hmac
import hashlib
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import traceback
import urllib.parse
import math
import websockets

# --- КОНСТАНТИ ТА НАЛАШТУВАННЯ ---[span_0](start_span)[span_0](end_span)
API_KEY = os.environ.get("BINGX_API_KEY", "")
API_SECRET = os.environ.get("BINGX_SECRET_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
RENDER_URL = os.environ.get("RENDER_URL", "")
BINGX_BASE_URL = "https://open-api.bingx.com"

LEVERAGE = 10
MARGIN_USD = 0.5
MAX_RISK_POSITIONS = 2
server_time_offset = 0
active_trade_monitors = {}
coin_cooldowns = {} # {symbol: timestamp паузи після помилки}[span_1](start_span)[span_1](end_span)

BLACKLIST = {"BTCUSDT", "LTCUSDT", "USDUSDT", "USD-USDT", "BTC", "LTC"}

def is_blacklisted(symbol):
    if symbol in BLACKLIST or symbol.startswith(("NCF", "NCS")):
        return True
    return False

# --- ДОПОМІЖНИЙ HTTP-СЕРВЕР ДЛЯ САМОПІНГУ ( Render Keep-Alive ) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive and running!")

def run_http_server():
    server = HTTPServer(('0.0.0.0', 10000), HealthCheckHandler)
    server.serve_forever()

# --- TELEGRAM NOTIFICATIONS & LOGS ---
async def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload, timeout=10) as resp:
                pass
        except Exception as e:
            print(f"[LOG ERROR] Помилка відправки в Telegram: {e}")

async def log_and_alert(error_title, error_message, symbol=None):
    full_text = f"❌ *{error_title}*"
    if symbol:
        full_text += f"\nМонета: `{symbol}`"
    full_text += f"\nПомилка: `{error_message}`"
    
    print(f"[ERROR] {error_title} | Symbol: {symbol} | Msg: {error_message}")
    print(traceback.format_exc())
    await send_telegram(full_text)

# --- BINGX API SIGNATURE & REQUESTS ---
def get_sign(api_secret, payload):
    return hmac.new(api_secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()

async def bingx_request(method, endpoint, params=None):
    if params is None:
        params = {}
    params['timestamp'] = int(time.time() * 1000) + server_time_offset
    query_string = urllib.parse.urlencode(sorted(params.items()))
    signature = get_sign(API_SECRET, query_string)
    url = f"{BINGX_BASE_URL}{endpoint}?{query_string}&signature={signature}"
    
    headers = {"X-BingX-API-KEY": API_KEY}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.request(method, url, headers=headers, timeout=15) as resp:
                data = await resp.json()
                if data.get("code") != 0:
                    print(f"[API WARN] Endpoint {endpoint} returned code {data.get('code')}: {data.get('msg')}")
                return data
        except Exception as e:
            await log_and_alert("Помилка запиту до API", str(e), endpoint)
            return None

async def get_klines(symbol, interval, limit=40):
    url = f"{BINGX_BASE_URL}/openApi/swap/v2/quote/klines?symbol={symbol}&interval={interval}&limit={limit}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=10) as resp:
                res = await resp.json()
                if res.get("code") == 0:
                    return res.get("data", [])
        except Exception as e:
            print(f"[LOG ERROR] Klines error {symbol}: {e}")
    return []

# --- АНАЛІЗ РИНКУ ТА РІВНІВ (RESONANCE + ATR) ---
def calculate_atr(klines, period=14):
    if len(klines) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(klines)):
        high = float(klines[i]['high'])
        low = float(klines[i]['low'])
        prev_close = float(klines[i-1]['close'])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs[-period:]) / period

async def analyze_market(symbol):
    try:
        raw_15m = await get_klines(symbol, "15m", 40)
        raw_1h = await get_klines(symbol, "1h", 30)
        
        if len(raw_15m) < 35 or len(raw_1h) < 20:
            return None

        klines_15m = [{'time': k['time'], 'open': float(k['open']), 'high': float(k['high']),
                       'low': float(k['low']), 'close': float(k['close']), 'volume': float(k['volume'])} for k in raw_15m]
        klines_1h = [{'high': float(k['high']), 'low': float(k['low']), 'close': float(k['close'])} for k in raw_1h]

        atr_15m = calculate_atr(klines_15m)
        current_price = klines_15m[-1]['close']
        
        max_40_high = max([x['high'] for x in klines_15m])
        min_40_low = min([x['low'] for x in klines_15m])
        
        avg_volume = sum([x['volume'] for x in klines_15m[-20:]]) / 20
        last_candle = klines_15m[-1]
        is_volume_spike = last_candle['volume'] > avg_volume * 1.5

        signal = None
        if current_price <= min_40_low * 1.003 and is_volume_spike:
            body = abs(last_candle['close'] - last_candle['open'])
            lower_shadow = min(last_candle['open'], last_candle['close']) - last_candle['low']
            if lower_shadow > body * 1.5 and last_candle['close'] >= last_candle['open']:
                signal = "LONG"

        elif current_price >= max_40_high * 0.997 and is_volume_spike:
            body = abs(last_candle['close'] - last_candle['open'])
            upper_shadow = last_candle['high'] - max(last_candle['open'], last_candle['close'])
            if upper_shadow > body * 1.5 and last_candle['close'] <= last_candle['open']:
                signal = "SHORT"

        if not signal:
            return None

        if signal == "LONG":
            stop_loss = min_40_low * (1 - 0.003)
            risk = current_price - stop_loss
        else:
            stop_loss = max_40_high * (1 + 0.003)
            risk = stop_loss - current_price

        if risk <= 0:
            return None

        if signal == "LONG":
            tp1 = max([x['high'] for x in klines_15m if x['high'] > current_price], default=current_price + (atr_15m * 2)) + atr_15m + 1
            tp2 = max([x['high'] for x in klines_1h if x['high'] > tp1], default=tp1 + (atr_15m * 3)) + atr_15m + 1
            tp3 = max([x['high'] for x in klines_1h if x['high'] > tp2], default=tp2 + (atr_15m * 4)) + atr_15m + 1
            reward_tp1 = tp1 - current_price
        else:
            tp1 = min([x['low'] for x in klines_15m if x['low'] < current_price], default=current_price - (atr_15m * 2)) - atr_15m - 1
            tp2 = min([x['low'] for x in klines_1h if x['low'] < tp1], default=tp1 - (atr_15m * 3)) - atr_15m - 1
            tp3 = min([x['low'] for x in klines_1h if x['low'] < tp2], default=tp2 - (atr_15m * 4)) - atr_15m - 1
            reward_tp1 = current_price - tp1

        if (reward_tp1 / risk) < 1.3:
            return None

        return {
            "symbol": symbol, "signal": signal, "entry": current_price,
            "stop_loss": stop_loss, "tp1": tp1, "tp2": tp2, "tp3": tp3, "atr": atr_15m
        }
    except Exception as e:
        await log_and_alert("Помилка під час аналізу ринку", str(e), symbol)
        return None

# --- ПЕРЕВІРКА ПОЗИЦІЙ ТА РИЗИКУ НА БІРЖІ ---
async def get_exchange_positions():
    res = await bingx_request("GET", "/openApi/swap/v2/user/positions")
    if res and res.get("code") == 0:
        return res.get("data", [])
    return []

async def count_risk_positions():
    try:
        positions = await get_exchange_positions()
        total_open = 0
        risk_count = 0
        for p in positions:
            if float(p.get('positionAmt', 0)) != 0:
                total_open += 1
                symbol = p.get('symbol')
                if symbol in active_trade_monitors and not active_trade_monitors[symbol].get('in_breakeven', False):
                    risk_count += 1
        
        print(f"[LOG STATUS] Всього відкритих позицій на біржі: {total_open} | З них з ризиком: {risk_count}")
        return risk_count
    except Exception as e:
        await log_and_alert("Помилка підрахунку ризикових позицій", str(e))
        return MAX_RISK_POSITIONS

# --- ВІДКРИТТЯ ПОЗИЦІЇ ТА УПРАВЛІННЯ ---
async def open_position(setup):
    symbol = setup['symbol']
    side = setup['signal']
    try:
        lev_res = await bingx_request("POST", "/openApi/swap/v1/trade/leverage", {"symbol": symbol, "leverage": LEVERAGE, "side": side})
        if not lev_res or lev_res.get("code") != 0:
            raise Exception(f"Не вдалося встановити плече: {lev_res.get('msg') if lev_res else 'Network error'}")

        qty = round(MARGIN_USD * LEVERAGE / setup['entry'], 4)
        order_payload = {
            "symbol": symbol,
            "side": "BUY" if side == "LONG" else "SELL",
            "positionSide": "LONG" if side == "LONG" else "SHORT",
            "type": "MARKET",
            "quantity": qty
        }
        
        res = await bingx_request("POST", "/openApi/swap/v2/trade/order", order_payload)
        if not res or res.get("code") != 0:
            err_msg = res.get("msg", "Unknown API error") if res else "Connection failed"
            raise Exception(f"Помилка ордеру маркет: {err_msg}")
            
        active_trade_monitors[symbol] = {
            "side": side, "entry": setup['entry'], "stop_loss": setup['stop_loss'],
            "tp1": setup['tp1'], "tp2": setup['tp2'], "tp3": setup['tp3'],
            "in_breakeven": False, "tp1_hit": False
        }
        
        msg = f"🚀 *Відкрито позицію ({side})*\nМонета: `{symbol}`\nВхід: `{setup['entry']}`\nСтоп: `{setup['stop_loss']}`\nTP1: `{setup['tp1']}`"
        await send_telegram(msg)
        print(f"[SUCCESS] Успішно відкрито {side} по {symbol} за ціною {setup['entry']}")
    except Exception as e:
        await log_and_alert("Помилка відкриття позиції", str(e), symbol)
        raise e

# --- ФОНОВИЙ МОНІТОРИНГ ТЕЙКІВ ТА СИНХРОНІЗАЦІЯ ---
async def monitor_trades_loop():
    while True:
        try:
            positions = await get_exchange_positions()
            active_symbols = {p['symbol'] for p in positions if float(p.get('positionAmt', 0)) != 0}
            
            for symbol, data in list(active_trade_monitors.items()):
                if symbol not in active_symbols:
                    await send_telegram(f"🏁 Позиція по `{symbol}` закрита на біржі (Стоп / Тейк спрацювали).")
                    print(f"[INFO] Позиція {symbol} закрита на біржі.")
                    del active_trade_monitors[symbol]
                    continue
                
                current_price_data = await get_klines(symbol, "1m", 1)
                if not current_price_data:
                    continue
                current_price = float(current_price_data[0]['close'])
                
                if not data['tp1_hit']:
                    hit_tp1 = (data['side'] == "LONG" and current_price >= data['tp1']) or \
                              (data['side'] == "SHORT" and current_price <= data['tp1'])
                    if hit_tp1:
                        data['tp1_hit'] = True
                        data['in_breakeven'] = True
                        await send_telegram(f"✅ TP1 досягнуто для `{symbol}`! Стоп переведено в безубиток (БУ).")
                        print(f"[SUCCESS] TP1 досягнуто для {symbol}, стоп переведено в БУ.")
                        
        except Exception as e:
            await log_and_alert("Помилка в моніторингу угод", str(e))
        await asyncio.sleep(10)

# --- 15-ХВИЛИННИЙ ЗВІТ ---
async def report_loop():
    while True:
        await asyncio.sleep(900)
        try:
            positions = await get_exchange_positions()
            open_positions_list = [p for p in positions if float(p.get('positionAmt', 0)) != 0]
            
            if not open_positions_list:
                print("[LOG REPORT] Немає відкритих позицій для звіту.")
                continue
                
            report = f"📊 *Звіт по відкритих позиціях (15 хв):*\nВсього активних: `{len(open_positions_list)}`\n"
            for p in open_positions_list:
                sym = p.get('symbol')
                pnl = p.get('unrealizedProfit', '0')
                report += f"• `{sym}` | PnL: `{pnl} USD`\n"
            await send_telegram(report)
        except Exception as e:
            await log_and_alert("Помилка генерації звіту", str(e))

# --- САМОПІНГ КОЖНІ 7 ХВИЛИН ---
async def self_ping_loop():
    while True:
        await asyncio.sleep(420)
        if RENDER_URL:
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.get(RENDER_URL, timeout=10) as resp:
                        pass
                except Exception as e:
                    print(f"[LOG WARN] Самопінг не вдався: {e}")

# --- ГОЛОВНИЙ ЦИКЛ СКАНУВАННЯ ---
async def main_scanner():
    asyncio.create_task(monitor_trades_loop())
    asyncio.create_task(report_loop())
    asyncio.create_task(self_ping_loop())
    
    threading.Thread(target=run_http_server, daemon=True).start()
    
    while True:
        try:
            print("🔍 Початок нового циклу сканування ринку...")
            
            res = await bingx_request("GET", "/openApi/swap/v2/quote/contracts")
            if not res or res.get("code") != 0:
                await log_and_alert("Помилка отримання контракту з біржі", res.get("msg") if res else "No response")
                await asyncio.sleep(30)
                continue
                
            symbols = [item['symbol'] for item in res['data'].get('contracts', []) if item['symbol'].endswith('-USDT') or item['symbol'].endswith('USDT')]
            
            for symbol in symbols:
                if is_blacklisted(symbol):
                    continue
                
                if symbol in coin_cooldowns and time.time() < coin_cooldowns[symbol]:
                    continue

                risk_pos_count = await count_risk_positions()
                if risk_pos_count >= MAX_RISK_POSITIONS:
                    print(f"🛑 Зупинка сканування: вже є {risk_pos_count} позиції з ризиком (ліміт: {MAX_RISK_POSITIONS})")
                    break

                try:
                    setup = await analyze_market(symbol)
                    if setup:
                        await open_position(setup)
                        break 
                except Exception as e:
                    coin_cooldowns[symbol] = time.time() + 300
                    await log_and_alert("Помилка при відкритті позиції", str(e), symbol)

        except Exception as e:
            await log_and_alert("Помилка в головному циклі сканування", str(e))
            
        await asyncio.sleep(30)

if __name__ == "__main__":
    try:
        asyncio.run(main_scanner())
    except KeyboardInterrupt:
        print("[INFO] Бот зупинений користувачем.")
    
