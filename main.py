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

# --- КОНСТАНТИ ТА НАЛАШТУВАННЯ ---
API_KEY = os.environ.get("BINGX_API_KEY", "")
API_SECRET = os.environ.get("BINGX_SECRET_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
RENDER_URL = os.environ.get("RENDER_URL", "")
BINGX_BASE_URL = "https://open-api.bingx.com"

LEVERAGE = 10
MARGIN_USD = 0.5
MAX_RISK_POSITIONS = 2
MIN_24H_VOLUME = 300_000
MAX_24H_VOLUME = 30_000_000

server_time_offset = 0
active_trade_monitors = {}
coin_cooldowns = {} 

BLACKLIST = {"BTCUSDT", "LTCUSDT", "USDUSDT", "USD-USDT", "BTC", "LTC", "BTC-USDT", "LTC-USDT"}

def is_blacklisted(symbol):
    if (symbol in BLACKLIST or 
        symbol.startswith(("NCF", "NCS", "NCC")) or 
        "USD" not in symbol or 
        "." in symbol):
        return True
    return False

# --- ДОПОМІЖНИЙ HTTP-СЕРВЕР ДЛЯ САМОПІНГУ ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive and running!")
        
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

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
            print(f"[LOG ERROR] Помилка відправки в Telegram: {e}", flush=True)

async def log_and_alert(error_title, error_message, symbol=None):
    full_text = f"❌ *{error_title}*"
    if symbol:
        full_text += f"\nМонета: `{symbol}`"
    full_text += f"\nПомилка: `{error_message}`"
    
    print(f"[ERROR] {error_title} | Symbol: {symbol} | Msg: {error_message}", flush=True)
    print(traceback.format_exc(), flush=True)
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
    
    headers = {"X-BX-APIKEY": API_KEY}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.request(method, url, headers=headers, timeout=15) as resp:
                data = await resp.json()
                if isinstance(data, dict) and data.get("code") != 0:
                    print(f"[API WARN] Endpoint {endpoint} returned code {data.get('code')}: {data.get('msg')}", flush=True)
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
                if isinstance(res, dict) and res.get("code") == 0:
                    return res.get("data", [])
                elif isinstance(res, list):
                    return res
        except Exception as e:
            print(f"[LOG ERROR] Klines error {symbol}: {e}", flush=True)
    return []

# --- АНАЛІЗ РИНКУ ---
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
            return None, False
            
        klines_15m = [{'time': k['time'], 'open': float(k['open']), 'high': float(k['high']), 
                       'low': float(k['low']), 'close': float(k['close']), 'volume': float(k['volume'])} for k in raw_15m]
        klines_1h = [{'high': float(k['high']), 'low': float(k['low']), 'close': float(k['close']), 'volume': float(k['volume'])} for k in raw_1h]
        
        atr_15m = calculate_atr(klines_15m)
        current_price = klines_15m[-1]['close']
        
        max_40_high = max([x['high'] for x in klines_15m])
        min_40_low = min([x['low'] for x in klines_15m])
        price_range = max_40_high - min_40_low
        
        if price_range <= 0 or atr_15m <= 0:
            return None, False
            
        price_position = (current_price - min_40_low) / price_range
        
        avg_volume = sum([x['volume'] for x in klines_15m[-20:]]) / 20
        last_candle = klines_15m[-1]
        prev_candle = klines_15m[-2]
        is_volume_spike = last_candle['volume'] > avg_volume * 1.5
        
        signal = None
        stop_loss = 0
        tp1 = tp2 = tp3 = 0
        
        # 1. LONG: Заборона купувати на хаях або під час обвалу
        if price_position < 0.7 and is_volume_spike:
            body_last = last_candle['close'] - last_candle['open']
            is_sharp_dump = body_last < -atr_15m * 0.8

            if not is_sharp_dump and (current_price <= min_40_low * 1.015 or (prev_candle['close'] > prev_candle['open'] and last_candle['close'] > last_candle['open'])):
                signal = "LONG"
                stop_loss = min_40_low - atr_15m
                
                resistances_15m = sorted([x['high'] for x in klines_15m if x['high'] > current_price])
                nearest_res = resistances_15m[0] if resistances_15m else current_price + (atr_15m * 4)
                
                # TP1 нижче опору, але не ближче ніж 2 * ATR
                tp1 = min(nearest_res - (atr_15m * 0.5), current_price + (atr_15m * 2.0))
                tp2 = tp1 + (atr_15m * 1.5)
                tp3 = tp2 + (atr_15m * 1.5)
                
                if not (stop_loss < current_price < tp1 < tp2 < tp3):
                    return None, is_volume_spike

        # 2. SHORT: ЖОРСТКА ЗАБОРОНА шортити на лоях або після різкого проливу вниз
        if not signal and price_position > 0.3 and is_volume_spike:
            body_last = last_candle['close'] - last_candle['open']
            is_sharp_dump = body_last < -atr_15m * 0.8

            if not is_sharp_dump and (current_price >= max_40_high * 0.985 or (prev_candle['close'] < prev_candle['open'] and last_candle['close'] < last_candle['open'])):
                signal = "SHORT"
                stop_loss = max_40_high + atr_15m
                
                supports_15m = sorted([x['low'] for x in klines_15m if x['low'] < current_price], reverse=True)
                nearest_sup = supports_15m[0] if supports_15m else current_price - (atr_15m * 4)
                
                # TP1 вище підтримки, але не ближче ніж 2 * ATR
                tp1 = max(nearest_sup + (atr_15m * 0.5), current_price - (atr_15m * 2.0))
                tp2 = tp1 - (atr_15m * 1.5)
                tp3 = tp2 - (atr_15m * 1.5)
                
                if not (stop_loss > current_price > tp1 > tp2 > tp3):
                    return None, is_volume_spike

        if not signal:
            return None, is_volume_spike

        risk = (current_price - stop_loss) if signal == "LONG" else (stop_loss - current_price)
        reward_tp1 = (tp1 - current_price) if signal == "LONG" else (current_price - tp1)

        # Перевірка співвідношення ризику до прибутку для TP1 (>= 1.3)
        if risk <= 0 or reward_tp1 <= 0 or (reward_tp1 / risk) < 1.3:
            return None, is_volume_spike

        return {
            "symbol": symbol, "signal": signal, "entry": current_price,
            "stop_loss": stop_loss, "tp1": tp1, "tp2": tp2, "tp3": tp3, "atr": atr_15m
        }, is_volume_spike
    except Exception as e:
        await log_and_alert("Помилка під час аналізу ринку", str(e), symbol)
        return None, False
        

# --- ПЕРЕВІРКА ПОЗИЦІЙ ---
async def get_exchange_positions():
    res = await bingx_request("GET", "/openApi/swap/v2/user/positions")
    if isinstance(res, dict) and res.get("code") == 0:
        return res.get("data", [])
    elif isinstance(res, list):
        return res
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
                if symbol not in active_trade_monitors:
                    active_trade_monitors[symbol] = {"in_breakeven": False}
                
                if not active_trade_monitors[symbol].get('in_breakeven', False):
                    risk_count += 1
        
        print(f"[LOG STATUS] Всього відкритих позицій на біржі: {total_open} | Ризикових: {risk_count}", flush=True)
        return risk_count
    except Exception as e:
        await log_and_alert("Помилка підрахунку ризикових позицій", str(e))
        return MAX_RISK_POSITIONS


# --- ВІДКРИТТЯ ПОЗИЦІЇ ТА ВИСТАВЛЕННЯ 3 TP (50/25/25) ТА SL ---
async def open_position(setup):
    symbol = setup['symbol']
    side = setup['signal']
    try:
        total_qty = round(MARGIN_USD * LEVERAGE / setup['entry'], 4)
        
        qty_50 = round(total_qty * 0.5, 4)
        qty_25_1 = round(total_qty * 0.25, 4)
        qty_25_2 = round(total_qty - qty_50 - qty_25_1, 4)

        # 1. Ринковий ордер на вхід
        order_payload = {
            "symbol": symbol,
            "side": "BUY" if side == "LONG" else "SELL",
            "positionSide": "LONG" if side == "LONG" else "SHORT",
            "type": "MARKET",
            "quantity": total_qty
        }
        
        res = await bingx_request("POST", "/openApi/swap/v2/trade/order", order_payload)
        if not res or (isinstance(res, dict) and res.get("code") != 0):
            err_msg = res.get("msg", "Unknown API error") if isinstance(res, dict) else "Connection failed"
            raise Exception(f"Помилка маркет ордеру: {err_msg}")
            
        sl_side = "SELL" if side == "LONG" else "BUY"

        # 2. Стоп-лосс на весь обсяг
        sl_payload = {
            "symbol": symbol,
            "side": sl_side,
            "positionSide": "LONG" if side == "LONG" else "SHORT",
            "type": "STOP_MARKET",
            "stopPrice": round(setup['stop_loss'], 4),
            "workingType": "MARK_PRICE",
            "reduceOnly": "true"
        }
        sl_res = await bingx_request("POST", "/openApi/swap/v2/trade/order", sl_payload)
        if not sl_res or (isinstance(sl_res, dict) and sl_res.get("code") != 0):
            err_msg = sl_res.get("msg", "Unknown error") if isinstance(sl_res, dict) else "No response"
            await log_and_alert("ПОМИЛКА СТОП-ЛОСУ", f"Не вдалося виставити стоп: {err_msg}", symbol)

        # 3. Виставлення трьох Тейк-Профітів (50% / 25% / 25%)
        for i, (tp_price, tp_qty) in enumerate([(setup['tp1'], qty_50), (setup['tp2'], qty_25_1), (setup['tp3'], qty_25_2)], 1):
            if tp_qty > 0:
                tp_payload = {
                    "symbol": symbol,
                    "side": sl_side,
                    "positionSide": "LONG" if side == "LONG" else "SHORT",
                    "type": "TAKE_PROFIT_MARKET",
                    "stopPrice": round(tp_price, 4),
                    "quantity": tp_qty,
                    "workingType": "MARK_PRICE",
                    "reduceOnly": "true"
                }
                tp_res = await bingx_request("POST", "/openApi/swap/v2/trade/order", tp_payload)
                if not tp_res or (isinstance(tp_res, dict) and tp_res.get("code") != 0):
                    err_msg = tp_res.get("msg", "Unknown error") if isinstance(tp_res, dict) else "No response"
                    await log_and_alert(f"ПОМИЛКА TP{i}", f"Не вдалося виставити TP{i}: {err_msg}", symbol)

        active_trade_monitors[symbol] = {
            "side": side, "entry": setup['entry'], "stop_loss": setup['stop_loss'],
            "tp1": setup['tp1'], "in_breakeven": False, "tp1_hit": False
        }
        
        msg = (f"🚀 *Відкрито позицію ({side}) з 3 TP (50/25/25)*\n"
               f"Монета: `{symbol}`\n"
               f"Вхід: `{setup['entry']}`\n"
               f"Стоп: `{setup['stop_loss']}`\n"
               f"TP1 (50%): `{setup['tp1']}`\n"
               f"TP2 (25%): `{setup['tp2']}`\n"
               f"TP3 (25%): `{setup['tp3']}`")
        await send_telegram(msg)
        print(f"[SUCCESS] Успішно відкрито {side} по {symbol} та надіслано ордери.", flush=True)
    except Exception as e:
        await log_and_alert("Помилка відкриття позиції", str(e), symbol)
        raise e
        
# --- МОНІТОРИНГ УГОД ---
async def monitor_trades_loop():
    while True:
        try:
            positions = await get_exchange_positions()
            active_symbols = [p['symbol'] for p in positions if float(p.get('positionAmt', 0)) != 0]

            for symbol, data in list(active_trade_monitors.items()):
                if symbol not in active_symbols:
                    await send_telegram(f"❌ Позиція по {symbol} закрита на біржі.")
                    print(f"[INFO] Позиція {symbol} закрита на біржі.", flush=True)
                    del active_trade_monitors[symbol]
                    continue

                if 'tp1' not in data:
                    continue

                # --- Перевірка зустрічного об'єму для дострокової фіксації плюсу ---
                try:
                    current_pos_data = next((p for p in positions if p.get('symbol') == symbol), None)
                    if current_pos_data:
                        unreal_pnl = float(current_pos_data.get('unrealizedProfit', 0))
                        pos_side = current_pos_data.get('positionSide', data.get('side'))
                        
                        klines_check = await get_klines(symbol, "15m", 25)
                        if len(klines_check) >= 20:
                            last_c = klines_check[-1]
                            avg_vol = sum([x['volume'] for x in klines_check[-20:]]) / 20
                            is_opp_spike = last_c['volume'] > avg_vol * 1.8
                            
                            if unreal_pnl > 0 and is_opp_spike:
                                if pos_side == "SHORT" and last_c['close'] > last_c['open']:
                                    close_payload = {
                                        "symbol": symbol,
                                        "side": "BUY",
                                        "positionSide": "SHORT",
                                        "type": "MARKET",
                                        "quantity": abs(float(current_pos_data.get('positionAmt', 0)))
                                    }
                                    await bingx_request('POST', '/openApi/swap/v2/trade/order', close_payload)
                                    await send_telegram(f"🛡 Рівень захищено! Закрито SHORT по {symbol} в плюс (PnL: {unreal_pnl:.2f}) через зустрічний об'єм.")
                                    del active_trade_monitors[symbol]
                                    continue
                                elif pos_side == "LONG" and last_c['close'] < last_c['open']:
                                    close_payload = {
                                        "symbol": symbol,
                                        "side": "SELL",
                                        "positionSide": "LONG",
                                        "type": "MARKET",
                                        "quantity": abs(float(current_pos_data.get('positionAmt', 0)))
                                    }
                                    await bingx_request('POST', '/openApi/swap/v2/trade/order', close_payload)
                                    await send_telegram(f"🛡 Рівень захищено! Закрито LONG по {symbol} в плюс (PnL: {unreal_pnl:.2f}) через зустрічний об'єм.")
                                    del active_trade_monitors[symbol]
                                    continue
                except Exception as ex:
                    print(f"Помилка захисту рівня у моніторингу: {ex}")
                # -------------------------------------------------------------

                current_price_data = await get_klines(symbol, "1h", 1)
                if not current_price_data:
                    continue
                current_price = float(current_price_data[0]['close'])

                if not data.get('tp1_hit', False):
                    hit_tp1 = (data['side'] == "LONG" and current_price >= data['tp1']) or \
                              (data['side'] == "SHORT" and current_price <= data['tp1'])
                    if hit_tp1:
                        data['tp1_hit'] = True
                        data['in_breakeven'] = True
                        await send_telegram(f"✅ TP1 досягнуто для {symbol}!")
                        print(f"[SUCCESS] TP1 досягнуто для {symbol}.", flush=True)

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
                print("[LOG REPORT] Немає відкритих позицій для звіту.", flush=True)
                continue
                
            report = f"📊 *Звіт по відкритих позиціях (15 хв):*\nВсього активних: `{len(open_positions_list)}`\n"
            for p in open_positions_list:
                sym = p.get('symbol')
                pnl = p.get('unrealizedProfit', '0')
                report += f"• `{sym}` | PnL: `{pnl} USD`\n"
            await send_telegram(report)
        except Exception as e:
            await log_and_alert("Помилка генерації звіту", str(e))

# --- САМОПІНГ ---
async def self_ping_loop():
    while True:
        await asyncio.sleep(420)
        if RENDER_URL:
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.get(RENDER_URL, timeout=10) as resp:
                        pass
                except Exception as e:
                    print(f"[LOG WARN] Самопінг не вдався: {e}", flush=True)

# --- ГОЛОВНИЙ ЦИКЛ СКАНУВАННЯ ---
async def main_scanner():
    asyncio.create_task(monitor_trades_loop())
    asyncio.create_task(report_loop())
    asyncio.create_task(self_ping_loop())
    
    threading.Thread(target=run_http_server, daemon=True).start()
    
    while True:
        try:
            print("🔍 Початок нового циклу сканування ринку...", flush=True)
            
            risk_pos_count = await count_risk_positions()
            if risk_pos_count >= MAX_RISK_POSITIONS:
                print(f"🛑 Зупинка сканування: вже є {risk_pos_count} ризикових позицій (ліміт: {MAX_RISK_POSITIONS})", flush=True)
                await asyncio.sleep(30)
                continue

            tickers_res = await bingx_request("GET", "/openApi/swap/v2/quote/ticker")
            ticker_volumes = {}
            if tickers_res and isinstance(tickers_res, dict) and tickers_res.get("code") == 0:
                for t in tickers_res.get("data", []):
                    sym = t.get("symbol")
                    q_vol = float(t.get("quoteVolume", 0) or t.get("volume", 0) or 0)
                    ticker_volumes[sym] = q_vol

            res = await bingx_request("GET", "/openApi/swap/v2/quote/contracts")
            if not res:
                await asyncio.sleep(30)
                continue
                
            contracts_data = []
            if isinstance(res, list):
                contracts_data = res
            elif isinstance(res, dict):
                if res.get("code") != 0:
                    await log_and_alert("Помилка отримання контракту", res.get("msg", "Unknown"))
                    await asyncio.sleep(30)
                    continue
                data_field = res.get("data", [])
                if isinstance(data_field, list):
                    contracts_data = data_field
                elif isinstance(data_field, dict):
                    contracts_data = data_field.get("contracts", [])

            symbols = [item['symbol'] for item in contracts_data if isinstance(item, dict) and 'symbol' in item and item['symbol'].endswith('USDT')]
            
            scanned_count = 0
            volume_spikes_count = 0
            signals_found = 0

            for symbol in symbols:
                if is_blacklisted(symbol):
                    continue
                
                vol_24h = ticker_volumes.get(symbol, 0)
                if vol_24h > 0 and not (MIN_24H_VOLUME <= vol_24h <= MAX_24H_VOLUME):
                    continue
                
                if symbol in coin_cooldowns and time.time() < coin_cooldowns[symbol]:
                    continue

                scanned_count += 1
                try:
                    setup, has_spike = await analyze_market(symbol)
                    if has_spike:
                        volume_spikes_count += 1
                    if setup:
                        signals_found += 1
                        await open_position(setup)
                        break 
                except Exception as e:
                    coin_cooldowns[symbol] = time.time() + 300
                    print(f"[ERROR] Помилка аналізу {symbol}: {e}", flush=True)

            print(f"📊 [СКАНУВАННЯ ЗАВЕРШЕНО] Перевірено пар: {scanned_count} | Сплесків об'єму: {volume_spikes_count} | Знайдено сигналів: {signals_found}", flush=True)

        except Exception as e:
            await log_and_alert("Помилка в головному циклі сканування", str(e))
            
        await asyncio.sleep(30)

if __name__ == "__main__":
    try:
        asyncio.run(main_scanner())
    except KeyboardInterrupt:
        print("[INFO] Бот зупинений користувачем.", flush=True)
