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

STATE_FILE = "trades_state.json"

def load_trades_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_trades_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[ERROR] Помилка збереження стану: {e}", flush=True)
        
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
active_trade_monitors = load_trades_state()
coin_cooldowns = {} 

BLACKLIST = {"BTCUSDT", "LTCUSDT", "USDUSDT", "USD-USDT", "BTC", "LTC", "BTC-USDT", "LTC-USDT"}

def is_blacklisted(symbol):
    if not symbol.endswith("-USDT"):
        return True
    if (symbol in BLACKLIST or 
        symbol.startswith(("NCF", "NCS", "NCC")) or 
        "," in symbol):
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
async def get_exchange_positions():
    try:
        res = await bingx_request('GET', '/openApi/swap/v2/user/positions')
        if isinstance(res, dict) and res.get('code') == 0:
            return res.get('data', [])
        if isinstance(res, list):
            return res
        return []
    except Exception as e:
        print(f"[LOG ERROR] Get positions error: {e}", flush=True)
        return []


def calculate_atr(klines, period=14):
    if len(klines) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(klines)):
        high = float(klines[i]['high'])
        low = float(klines[i]['low'])
        prev_close = float(klines[i - 1]['close'])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs[-period:]) / period

async def analyze_market(symbol):
    try:
        raw_15m = await get_klines(symbol, "15m", 100)
        raw_1h = await get_klines(symbol, "1h", 100)
        if len(raw_15m) < 100 or len(raw_1h) < 100:
            return None, False, "Недостатньо історичних даних"

        klines_15m = [{
            'time': k['time'], 
            'open': float(k['open']), 
            'high': float(k['high']), 
            'low': float(k['low']), 
            'close': float(k['close']), 
            'volume': float(k['volume'])
        } for k in raw_15m]
        
        klines_1h = [{
            'time': k['time'], 
            'open': float(k['open']), 
            'high': float(k['high']), 
            'low': float(k['low']), 
            'close': float(k['close']), 
            'volume': float(k['volume'])
        } for k in raw_1h]

        atr_15m = calculate_atr(klines_15m, period=14)
        current_price = klines_15m[-1]['close']
        ema_50 = sum([x['close'] for x in klines_15m[-50:]]) / 50 if len(klines_15m) >= 50 else current_price

        if atr_15m == 0:
            return None, False, "Нульовий АТР"

        # 1. Фільтр об'єму (тепер вимагаємо >= 2.0)
        avg_volume = sum([x['volume'] for x in klines_15m[-20:]]) / 20
        last_candle = klines_15m[-1]
        is_volume_spike = last_candle['volume'] > avg_volume * 2.0

        if not is_volume_spike:
            return None, False, "Немає сплеску об'єму (< 2.0x)"

        # 2. Захисний фільтр EMA (щоб не відкривати позицію на імпульсі занадто далеко від EMA)
        if abs(current_price - ema_50) > (atr_15m * 4.0):
            return None, False, "Ціна занадто далеко від EMA (імпульсний виліт)"

        # 3. Визначення фази ринку (Флет чи Тренд) за останні 40 свічок
        min_40_low = min([x['low'] for x in klines_15m[-40:]])
        max_40_high = max([x['high'] for x in klines_15m[-40:]])
        recent_range = max_40_high - min_40_low
        
        is_flat = recent_range < (atr_15m * 2.5)

        # 4. Розрахунок індикатора KDJ на останніх 5 свічках (перетин знизу вгору для лонгу, зверху вниз для шорту)
        def calculate_kdj_cross(klines, lookback=5):
            # Простий розрахунок KDJ для останніх багів
            k_list, d_list = [], []
            for i in range(len(klines) - lookback - 10, len(klines)):
                sub = klines[max(0, i-9):i+1]
                low_n = min([x['low'] for x in sub])
                high_n = max([x['high'] for x in sub])
                rsv = ((sub[-1]['close'] - low_n) / (high_n - low_n) * 100) if high_n != low_n else 50
                k = rsv if not k_list else (2/3 * k_list[-1] + 1/3 * rsv)
                d = k if not d_list else (2/3 * d_list[-1] + 1/3 * k)
                k_list.append(k)
                d_list.append(d)
            
            # Шукаємо перетин на останніх lookback свічках
            crossed_bullish = False
            crossed_bearish = False
            for idx in range(-lookback, 0):
                if idx-1 >= -len(k_list):
                    # Бичачий перетин (K перетинає D знизу вгору в зоні перепроданості < 30)
                    if k_list[idx-1] <= d_list[idx-1] and k_list[idx] > d_list[idx] and k_list[idx] < 35:
                        crossed_bullish = True
                    # Ведмежий перетин (K перетинає D зверху вниз у зоні перекупленості > 65)
                    if k_list[idx-1] >= d_list[idx-1] and k_list[idx] < d_list[idx] and k_list[idx] > 65:
                        crossed_bearish = True
            return crossed_bullish, crossed_bearish

        kdj_bull, kdj_bear = calculate_kdj_cross(klines_15m, lookback=5)

        long_reasons = []
        short_reasons = []

        # --- ЛОГІКА ДЛЯ ЛОНГУ ---
        if kdj_bull:
            if is_flat:
                # Умова для флету: ціна близько до підтримки
                if current_price <= (min_40_low + (atr_15m * 1.5)):
                    stop_loss = round(min_40_low - (atr_15m * 1.0), 4)
                    tp1 = round(max_40_high - (atr_15m * 1.0), 4)
                    risk = current_price - stop_loss
                    reward = tp1 - current_price
                    if risk > 0 and (reward / risk) >= 1.3:
                        tp2 = round(tp1 + (atr_15m * 2.0), 4)
                        tp3 = round(tp2 + (atr_15m * 2.0), 4)
                        return {
                            "symbol": symbol, "signal": "LONG", "entry": current_price,
                            "stop_loss": stop_loss, "tp1": tp1, "tp2": tp2, "tp3": tp3, "atr": atr_15m
                        }, is_volume_spike, "OK (Flat Long)"
                else:
                    long_reasons.append("Флет Лонг: ціна не біля підтримки")
            else:
                # Умова для тренду
                local_min = min([x['low'] for x in klines_15m[-5:]])
                stop_loss = round(local_min - (atr_15m * 0.5), 4)
                risk = current_price - stop_loss
                tp1 = round(current_price + risk + (atr_15m * 1.0), 4)
                tp2 = round(tp1 + (atr_15m * 2.0), 4)
                tp3 = round(tp2 + (atr_15m * 2.0), 4)
                reward = tp1 - current_price
                if risk > 0 and (reward / risk) >= 1.3:
                    return {
                        "symbol": symbol, "signal": "LONG", "entry": current_price,
                        "stop_loss": stop_loss, "tp1": tp1, "tp2": tp2, "tp3": tp3, "atr": atr_15m
                    }, is_volume_spike, "OK (Trend Long)"
                else:
                    long_reasons.append("Тренд Лонг: малий Risk/Reward")
        else:
            long_reasons.append("Немає KDJ перетину внизу")

        # --- ЛОГІКА ДЛЯ ШОРТУ ---
        if kdj_bear:
            if is_flat:
                # Умова для флету: ціна близько до опору
                if current_price >= (max_40_high - (atr_15m * 1.5)):
                    stop_loss = round(max_40_high + (atr_15m * 1.0), 4)
                    tp1 = round(min_40_low + (atr_15m * 1.0), 4)
                    risk = stop_loss - current_price
                    reward = current_price - tp1
                    if risk > 0 and (reward / risk) >= 1.3:
                        tp2 = round(tp1 - (atr_15m * 2.0), 4)
                        tp3 = round(tp2 - (atr_15m * 2.0), 4)
                        return {
                            "symbol": symbol, "signal": "SHORT", "entry": current_price,
                            "stop_loss": stop_loss, "tp1": tp1, "tp2": tp2, "tp3": tp3, "atr": atr_15m
                        }, is_volume_spike, "OK (Flat Short)"
                else:
                    short_reasons.append("Флет Шорт: ціна не біля опору")
            else:
                # Умова для тренду
                local_max = max([x['high'] for x in klines_15m[-5:]])
                stop_loss = round(local_max + (atr_15m * 0.5), 4)
                risk = stop_loss - current_price
                tp1 = round(current_price - risk - (atr_15m * 1.0), 4)
                tp2 = round(tp1 - (atr_15m * 2.0), 4)
                tp3 = round(tp2 - (atr_15m * 2.0), 4)
                reward = current_price - tp1
                if risk > 0 and (reward / risk) >= 1.3:
                    return {
                        "symbol": symbol, "signal": "SHORT", "entry": current_price,
                        "stop_loss": stop_loss, "tp1": tp1, "tp2": tp2, "tp3": tp3, "atr": atr_15m
                    }, is_volume_spike, "OK (Trend Short)"
                else:
                    short_reasons.append("Тренд Шорт: малий Risk/Reward")
        else:
            short_reasons.append("Немає KDJ перетину вгорі")

        combined_reason = f"Long: {', '.join(long_reasons)} | Short: {', '.join(short_reasons)}"
        return None, is_volume_spike, combined_reason

    except Exception as e:
        print(f"[LOG ERROR] Помилка під час аналізу ринку: {e}", flush=True)
        return None, False, f"Помилка: {str(e)}"
        
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
                    pos_side = p.get('positionSide') or ('LONG' if float(p.get('positionAmt', 0)) > 0 else 'SHORT')
                    entry_p = float(p.get('entryPrice', 0))
                    active_trade_monitors[symbol] = {
                        'entry': entry_p,
                        'stop_loss': entry_p,
                        'tp1': entry_p,
                        'in_breakeven': False,
                        'tp1_hit': False,
                        'tp2_hit': False,
                        'tp3_hit': False,
                        'side': pos_side
                    }
                    risk_count += 1
                else:
                    monitor_data = active_trade_monitors[symbol]
                    is_safe = monitor_data.get('in_breakeven', False) or monitor_data.get('tp1_hit', False)
                    if not is_safe:
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
    stop_loss = setup['stop_loss']
    try:
        raw_qty = (MARGIN_USD * LEVERAGE) / setup['entry']
        total_qty = round(raw_qty, 4)
        if total_qty <= 0:
            total_qty = 0.001

        qty_50 = round(total_qty * 0.5, 4)
        qty_25_1 = round(total_qty * 0.25, 4)
        qty_25_2 = round(total_qty - qty_50 - qty_25_1, 4)
        if qty_50 <= 0: qty_50 = 0.001
        if qty_25_1 <= 0: qty_25_1 = 0.001
        if qty_25_2 <= 0: qty_25_2 = 0.001

        order_payload = {
            "symbol": symbol,
            "side": "BUY" if side == "LONG" else "SELL",
            "positionSide": "LONG" if side == "LONG" else "SHORT",
            "type": "MARKET",
            "quantity": total_qty,
        }

        res = await bingx_request('POST', '/openApi/swap/v2/trade/order', order_payload)
        if not res or (isinstance(res, dict) and res.get("code") != 0):
            err_msg = res.get("msg", "Unknown error") if isinstance(res, dict) else "Connection failed"
            raise Exception(f"Помилка маркет ордеру: {err_msg}")

        # Забираємо реальну кількість, яку підтвердила біржа, щоб уникнути конфліктів округлення
        data_res = res.get("data", {})
        if isinstance(data_res, dict):
            order_info = data_res.get("order", data_res)
            real_qty = float(order_info.get("executedQty", order_info.get("quantity", total_qty)))
            if real_qty > 0:
                total_qty = real_qty
                # Перераховуємо частини тейків під реальний обсяг біржі
                qty_50 = round(total_qty * 0.5, 6)
                qty_25_1 = round(total_qty * 0.25, 6)
                qty_25_2 = round(total_qty - qty_50 - qty_25_1, 6)
                
        sl_side = "SELL" if side == "LONG" else "BUY"

        sl_payload = {
            "symbol": symbol,
            "side": sl_side,
            "positionSide": "LONG" if side == "LONG" else "SHORT",
            "type": "STOP_MARKET",
            "stopPrice": float(f"{stop_loss:.6f}"),
            "quantity": total_qty,
            "workingType": "MARK_PRICE"
        }

        sl_res = await bingx_request('POST', '/openApi/swap/v2/trade/order', sl_payload)
        if not sl_res or (isinstance(sl_res, dict) and sl_res.get("code") != 0):
            err_msg = sl_res.get("msg", "Unknown error") if isinstance(sl_res, dict) else "No response"
            await log_and_alert("ПОМИЛКА СТОП-ЛОСУ", f"Не вдалося виставити стоп: {err_msg}", symbol)

        for i, (tp_price, tp_qty) in enumerate([(setup['tp1'], qty_50), (setup['tp2'], qty_25_1), (setup['tp3'], qty_25_2)]):
            if tp_qty > 0:
                tp_payload = {
                    "symbol": symbol,
                    "side": sl_side,
                    "positionSide": "LONG" if side == "LONG" else "SHORT",
                    "type": "TAKE_PROFIT_MARKET",
                    "stopPrice": float(f"{tp_price:.6f}"),
                    "quantity": tp_qty,
                    "workingType": "MARK_PRICE"
                }

                tp_res = await bingx_request('POST', '/openApi/swap/v2/trade/order', tp_payload)
                if not tp_res or (isinstance(tp_res, dict) and tp_res.get("code") != 0):
                    err_msg = tp_res.get("msg", "Unknown error") if isinstance(res, dict) else "No response"
                    await log_and_alert(f"ПОМИЛКА TP{i+1}", f"Не вдалося виставити TP{i+1}: {err_msg}", symbol)

        active_trade_monitors[symbol] = {
            "side": side, "entry": setup['entry'], "stop_loss": setup['stop_loss'],
            "tp1": setup['tp1'], "tp2": setup['tp2'], "tp3": setup['tp3'],
            "in_breakeven": False, "tp1_hit": False, "tp2_hit": False, "tp3_hit": False, "atr": setup['atr']
        }
        save_trades_state(active_trade_monitors)

        msg = (f"🚀 Відкрито позиція ({side}) з 3 TP (50/25/25)\n"
               f"📌 Монета: {symbol}\n"
               f"📍 Вхід: {setup['entry']}\n"
               f"🛑 Стоп: {setup['stop_loss']}\n"
               f"🎯 TP1 (50%): {setup['tp1']}\n"
               f"🎯 TP2 (25%): {setup['tp2']}\n"
               f"🎯 TP3 (25%): {setup['tp3']}")
        await send_telegram(msg)
        print(f"[SUCCESS] Успішно відкрито координатний ордер по {symbol}.", flush=True)
    except Exception as e:
        await log_and_alert("Помилка відкриття позиції", str(e), symbol)
        raise e

async def start_websocket_monitor():
    while True:
        try:
            ts = str(int(time.time() * 1000) + int(server_time_offset))
            params = {'timestamp': ts}
            query_str = urllib.parse.urlencode(sorted(params.items()))
            sig = get_sign(API_SECRET, query_str)
            url = f"{BINGX_BASE_URL}/openApi/swap/v2/user/listenKey?{query_str}&signature={sig}"
            headers = {"X-BX-APIKEY": API_KEY}
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers) as resp:
                    data = await resp.json()
                    listen_key = data.get("data", {}).get("listenKey") if data.get("code") == 0 else None

            if not listen_key:
                await asyncio.sleep(10)
                continue

            ws_url = f"wss://open-api-ws.bingx.com/market?listenKey={listen_key}"
            async with websockets.connect(ws_url) as ws:
                print("[WS] Підключено до WebSocket BingX для моніторингу тейків", flush=True)
                async for message in ws:
                    data = json.loads(message)
        except Exception as e:
            print(f"[WS Помилка] Обрив з'єднання: {e}. Перепідключення через 5 сек...", flush=True)
            await asyncio.sleep(5)
            
# --- МОНІТОРИНГ УГОД ---
async def monitor_trades_loop():
    while True:
        try:
            positions = await get_exchange_positions()
            active_symbols = [p['symbol'] for p in positions if float(p.get('positionAmt', 0)) != 0]

            for p in positions:
                if float(p.get('positionAmt', 0)) != 0:
                    sym = p.get('symbol')
                    if sym not in active_trade_monitors:
                        pos_side = p.get('positionSide') or ('LONG' if float(p.get('positionAmt', 0)) > 0 else 'SHORT')
                        entry_p = float(p.get('entryPrice', 0))
                        active_trade_monitors[sym] = {
                            'entry': entry_p,
                            'stop_loss': entry_p,
                            'tp1': entry_p,
                            'in_breakeven': False,
                            'tp1_hit': False,
                            'tp2_hit': False,
                            'tp3_hit': False,
                            'side': pos_side
                        }
                        save_trades_state(active_trade_monitors)
                        print(f"[INFO] Відновлено моніторинг позиції після перезапуску: {sym}", flush=True)

            for symbol, data in list(active_trade_monitors.items()):
                current_pos_data = next((p for p in positions if p.get('symbol') == symbol), None)
                
                if symbol not in active_symbols or not current_pos_data:
                    if data.get('tp3_hit', False):
                        await send_telegram(f"🚀 **Всі цілі досягнуто (ТР3)!** Позиція по `{symbol}` повністю закрита по максимальному профіту.")
                        print(f"[INFO] Позиція {symbol} закрита по ТР3.", flush=True)
                    else:
                        await send_telegram(f"🛑 **Стоп по позиції!** Позицію по `{symbol}` закрито за стоп-лоссом.")
                        print(f"[INFO] Позиція {symbol} закрита за стоп-лоссом.", flush=True)

                    del active_trade_monitors[symbol]
                    save_trades_state(active_trade_monitors)
                    continue

                # --- Перевірка зустрічного об'єму ---
                try:
                    if current_pos_data:
                        unreal_pnl = float(current_pos_data.get('unrealizedProfit', 0) or 0)
                        pos_side = current_pos_data.get('positionSide', data.get('side'))

                        klines_check = await get_klines(symbol, "15m", 25)
                        if len(klines_check) >= 20:
                            last_c = klines_check[-2]
                            avg_vol = sum([float(x.get('volume', 0)) for x in klines_check[-21:-1]]) / 20  
                            is_opp_spike = float(last_c.get('volume', 0)) > avg_vol * 2.5  

                            if unreal_pnl > 0 and is_opp_spike:
                                last_close = float(last_c.get('close', 0))
                                last_open = float(last_c.get('open', 0))
                                last_high = float(last_c.get('high', 0))
                                last_low = float(last_c.get('low', 0))

                                candle_range = last_high - last_low
                                body_size = abs(last_close - last_open)
                                body_ratio = (body_size / candle_range) if candle_range > 0 else 0.0
                                is_valid_result = body_ratio >= 0.50

                                if pos_side == "SHORT" and last_close > last_open and is_valid_result:
                                    await send_telegram(f"⚠️ **Увага!** Зустрічний об'єм проти SHORT по `{symbol}` (PnL: `{unreal_pnl:.4f} USD`)")
                                elif pos_side == "LONG" and last_close < last_open and is_valid_result:
                                    await send_telegram(f"⚠️ **Увага!** Зустрічний об'єм проти LONG по `{symbol}` (PnL: `{unreal_pnl:.4f} USD`)")
                except Exception as ex:
                    print(f"Помилка перевірки зустрічного об'єму: {ex}", flush=True)

                ticker_res = await bingx_request('GET', '/openApi/swap/v2/quote/ticker', {'symbol': symbol})
                if not ticker_res or not isinstance(ticker_res, dict) or ticker_res.get('code') != 0:
                    continue
                ticker_data = ticker_res.get('data', {})

                current_price = float(ticker_data.get('lastPrice', 0) or ticker_data.get('price', 0))
                if current_price <= 0:
                    print(f"[WARN] {symbol}: Отримано невалідну нульову ціну з біржі!", flush=True)
                    continue
                    
                entry_price = float(data.get('entry', 0))
                if entry_price == 0 and current_pos_data:
                    entry_price = float(
                        current_pos_data.get('avgPrice', 0) or
                        current_pos_data.get('entryPrice', 0) or
                        current_pos_data.get('averagePrice', 0) or
                        current_pos_data.get('price', 0) or 0
                    )
                if entry_price == 0:
                    entry_price = current_price

                data['entry'] = entry_price 
                atr_val = float(data.get('atr', current_price * 0.01))
                side_val = data.get('side', "LONG" if current_pos_data and current_pos_data.get('positionSide') == "LONG" else "SHORT")
                data['side'] = side_val

                tp1_price = float(data['tp1']) if 'tp1' in data and float(data['tp1']) > 0 else (entry_price + (atr_val * 2.0) if side_val == "LONG" else entry_price - (atr_val * 2.0))
                tp2_price = float(data.get('tp2', tp1_price + (atr_val * 2.0)))
                tp3_price = float(data.get('tp3', tp2_price + (atr_val * 2.0)))

                # 1. Перевірка TP1 та перенесення в БУ
                if not data.get('tp1_hit', False):
                    hit_tp1 = (side_val == "LONG" and current_price >= tp1_price) or (side_val == "SHORT" and current_price <= tp1_price)
                    if hit_tp1 and entry_price > 0:
                        debug_msg = f"🎯 [DEBUG] {symbol} спрацював TP1!\nЦіна: {current_price} | Ціль TP1: {tp1_price}"
                        print(debug_msg, flush=True)
                        await send_telegram(debug_msg)

                        data['tp1_hit'] = True
                        data['in_breakeven'] = True
                        save_trades_state(active_trade_monitors)

                        try:
                            orders_res = await bingx_request('GET', '/openApi/swap/v2/trade/openOrders', {'symbol': symbol})
                            orders_list = orders_res.get("data", []) if isinstance(orders_res, dict) else orders_res
                            if isinstance(orders_list, dict):
                                orders_list = orders_list.get("orders", [])

                            for ord_item in orders_list:
                                if ord_item.get("type") == "STOP_MARKET":
                                    ord_id = ord_item.get("orderId")
                                    if ord_id:
                                        await bingx_request('DELETE', '/openApi/swap/v2/trade/order', {'symbol': symbol, 'orderId': ord_id})

                            sl_side = "SELL" if side_val == "LONG" else "BUY"
                            current_qty = abs(float(current_pos_data.get('positionAmt', 0))) if current_pos_data else 0
                            sl_payload = {
                                "symbol": symbol,
                                "side": sl_side,
                                "positionSide": side_val,
                                "type": "STOP_MARKET",
                                "stopPrice": float(f"{entry_price:.6f}"),
                                "quantity": current_qty,
                                "workingType": "MARK_PRICE"
                            }
                            await bingx_request('POST', '/openApi/swap/v2/trade/order', sl_payload)
                        except Exception as ex:
                            print(f"[ERROR] Помилка перенесення стопа в БУ: {ex}", flush=True)

                        await send_telegram(f"🎯 **TP1 досягнуто!** Монета: `{symbol}`\n🛡️ Стоп-лосс успішно перенесено в безубиток (БУ).")

                # 2. Перевірка TP2
                if data.get('tp1_hit', False) and not data.get('tp2_hit', False):
                    hit_tp2 = (side_val == "LONG" and current_price >= tp2_price) or (side_val == "SHORT" and current_price <= tp2_price)
                    if hit_tp2:
                        data['tp2_hit'] = True
                        save_trades_state(active_trade_monitors)
                        await send_telegram(f"🎯 **TP2 досягнуто!** Монета: `{symbol}`\nЦіна: {current_price} | Ціль TP2: {tp2_price}\nУспішно зафіксовано другу частину прибутку.")

                # 3. Перевірка TP3
                if data.get('tp2_hit', False) and not data.get('tp3_hit', False):
                    hit_tp3 = (side_val == "LONG" and current_price >= tp3_price) or (side_val == "SHORT" and current_price <= tp3_price)
                    if hit_tp3:
                        data['tp3_hit'] = True
                        save_trades_state(active_trade_monitors)
                        await send_telegram(f"🔥 **TP3 досягнуто!** Монета: `{symbol}`\nЦіна: {current_price} | Ціль TP3: {tp3_price}\nФінальна фіксація прибутку!")

        except Exception as e:
            await log_and_alert("Помилка в моніторингу угод", str(e))

        await asyncio.sleep(60)

# --- 15-ХВИЛИННИЙ ЗВІТ ---
async def report_loop():
    while True:
        await asyncio.sleep(900)
        try:
            positions = await get_exchange_positions()
            open_positions_list = [p for p in positions if float(p.get('positionAmt', 0)) != 0]
            if not open_positions_list:
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
    asyncio.create_task(start_websocket_monitor())

    threading.Thread(target=run_http_server, daemon=True).start()

    while True:
        try:
            print("🔍 Початок нового циклу сканування ринку...", flush=True)

            risk_pos_count = await count_risk_positions()
            if risk_pos_count >= MAX_RISK_POSITIONS:
                await asyncio.sleep(30)
                continue

            tickers_res = await bingx_request('GET', '/openApi/swap/v2/quote/ticker')
            ticker_volumes = {}
            if tickers_res and isinstance(tickers_res, dict) and tickers_res.get('code') == 0:
                for t in tickers_res.get("data", []):
                    sym = t.get("symbol")
                    q_vol = float(t.get("quoteVolume", 0) or t.get("volume", 0) or 0)
                    ticker_volumes[sym] = q_vol

            res = await bingx_request('GET', '/openApi/swap/v2/quote/contracts')
            if not res:
                await asyncio.sleep(30)
                continue

            contracts_data = []
            if isinstance(res, list):
                contracts_data = res
            elif isinstance(res, dict):
                data_field = res.get("data", [])
                contracts_data = data_field if isinstance(data_field, list) else data_field.get("contracts", [])

            symbols = [item['symbol'] for item in contracts_data if isinstance(item, dict) and 'symbol' in item]
            open_pos = await get_exchange_positions()
            active_symbols = [p['symbol'] for p in open_pos if float(p.get('positionAmt', 0)) != 0]

            scanned_count = 0
            volume_spikes_count = 0
            signals_found = 0
            skipped_details = []

            for symbol in symbols:
                if symbol in active_symbols or is_blacklisted(symbol):
                    continue
            
                vol_24h = ticker_volumes.get(symbol, 0)
                if vol_24h > 0 and not (MIN_24H_VOLUME <= vol_24h <= MAX_24H_VOLUME):
                    continue

                if symbol in coin_cooldowns and time.time() < coin_cooldowns[symbol]:
                    continue

                scanned_count += 1
                try:
                    res = await analyze_market(symbol)
                    if isinstance(res, tuple) and len(res) == 3:
                        setup, has_spike, reason = res
                    else:
                        setup, has_spike = res
                        reason = "Невідома причина"

                    if has_spike:
                        volume_spikes_count += 1
                        if not setup:
                            skipped_details.append(f"• {symbol}: {reason}")

                    if setup:
                        signals_found += 1
                        await open_position(setup)
                        break
                except Exception as e:
                    coin_cooldowns[symbol] = time.time() + 300
                    print(f"[ERROR] Помилка аналізу {symbol}: {e}", flush=True)

            if skipped_details:
                report_msg = "🔍 Монети зі сплеском об'єму (відсіяні):\n" + "\n".join(skipped_details)
                await send_telegram(report_msg)

        except Exception as e:
            await log_and_alert("Помилка в головному циклі сканування", str(e))

        await asyncio.sleep(60)

if __name__ == "__main__":
    try:
        asyncio.run(main_scanner())
    except KeyboardInterrupt:
        print("[INFO] Бот зупинений користувачем.", flush=True)
