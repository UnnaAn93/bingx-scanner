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

# --- МАТЕМАТИЧНІ ФУНКЦІЇ ---
def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return 0.0
    tr_list = []
    for i in range(1, len(candles)):
        high = float(candles[i]['high'])
        low = float(candles[i]['low'])
        prev_close = float(candles[i-1]['close'])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)
    return sum(tr_list[-period:]) / period

def calculate_ema(candles, period=50):
    if len(candles) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(float(x['close']) for x in candles[:period]) / period
    for i in range(period, len(candles)):
        close = float(candles[i]['close'])
        ema = (close - ema) * multiplier + ema
    return ema

def find_last_fractal(candles, fractal_type="high"):
    for i in range(len(candles) - 3, 2, -1):
        if fractal_type == "high":
            if (candles[i]['high'] > candles[i-1]['high'] and 
                candles[i]['high'] > candles[i-2]['high'] and 
                candles[i]['high'] > candles[i+1]['high'] and 
                candles[i]['high'] > candles[i+2]['high']):
                return candles[i]['high']
        elif fractal_type == "low":
            if (candles[i]['low'] < candles[i-1]['low'] and 
                candles[i]['low'] < candles[i-2]['low'] and 
                candles[i]['low'] < candles[i+1]['low'] and 
                candles[i]['low'] < candles[i+2]['low']):
                return candles[i]['low']
                
    if fractal_type == "high":
        return max(c['high'] for c in candles[-15:])
    else:
        return min(c['low'] for c in candles[-15:])

def find_last_two_fractals(candles, fractal_type='high'):
    found = []
    for i in range(len(candles) - 3, 2, -1):
        if fractal_type == 'high':
            if (candles[i]['high'] > candles[i-1]['high'] and 
                candles[i]['high'] > candles[i-2]['high'] and 
                candles[i]['high'] > candles[i+1]['high'] and 
                candles[i]['high'] > candles[i+2]['high']):
                found.append(candles[i]['high'])
                if len(found) == 2:
                    return found
        elif fractal_type == 'low':
            if (candles[i]['low'] < candles[i-1]['low'] and 
                candles[i]['low'] < candles[i-2]['low'] and 
                candles[i]['low'] < candles[i+1]['low'] and 
                candles[i]['low'] < candles[i+2]['low']):
                found.append(candles[i]['low'])
                if len(found) == 2:
                    return found
    return []

def count_recent_tests(candles_last_15, level_price, threshold, test_type="resistance"):
    tests = 0
    for c in candles_last_15:
        if test_type == "resistance":
            if abs(float(c['high']) - level_price) <= threshold and float(c['close']) <= level_price:
                tests += 1
        else:
            if abs(float(c['low']) - level_price) <= threshold and float(c['close']) >= level_price:
                tests += 1
    return tests

# --- ОСНОВНИЙ АНАЛІЗ РИНКУ ---
# --- ОСНОВНИЙ АНАЛІЗ РИНКУ ---
async def analyze_market(symbol):
    try:
        raw_15m = await get_klines(symbol, "15m", 100)
        raw_1m = await get_klines(symbol, "1m", 50)

        if len(raw_15m) < 100 or len(raw_1m) < 30:
            return None, False, "Недостатньо історичних даних"

        klines_15m = [
            {
                'time': k['time'],
                'open': float(k['open']),
                'high': float(k['high']),
                'low': float(k['low']),
                'close': float(k['close']),
                'volume': float(k['volume'])
            } for k in raw_15m
        ]

        klines_1m = [
            {
                'time': k['time'],
                'open': float(k['open']),
                'high': float(k['high']),
                'low': float(k['low']),
                'close': float(k['close']),
                'volume': float(k['volume'])
            } for k in raw_1m
        ]

        # Крок 1: Перевірка всплеску об'єму на 1м за останні 10 свічок
        avg_volume_1m = sum(float(x.get('volume', 0)) for x in klines_1m[-30:-10]) / 20
        has_volume_spike_1m = any(
            float(x.get('volume', 0)) >= avg_volume_1m * 2.0
            for x in klines_1m[-10:]
        )

        if not has_volume_spike_1m:
            return None, False, "Немає всплеску об'єму на 1м (>= 2.0x за останні 10 свічок)"

        closed_candles = klines_15m[:-1]
        atr_15m = calculate_atr(closed_candles, period=14)
        current_price = klines_15m[-1]['close']

        if atr_15m == 0:
            return None, True, "Нульовий ATR"

        ema_50 = calculate_ema(closed_candles, period=50)
        if ema_50 is None:
            return None, False, "Недостатньо даних для EMA 50"

        base_100_candles = closed_candles[-100:]
        level_base_85 = base_100_candles[-85:]
        recent_15_candles = base_100_candles[-15:]

        max_85_high = max(float(x['high']) for x in level_base_85)
        min_85_low = min(float(x['low']) for x in level_base_85)

        last_candle = closed_candles[-1]
        last_lower_wick = min(last_candle['open'], last_candle['close']) - last_candle['low']
        last_upper_wick = last_candle['high'] - max(last_candle['open'], last_candle['close'])
        candle_range = float(last_candle['high']) - float(last_candle['low'])

        volumes_20 = [float(c['volume']) for c in closed_candles[-20:]]
        avg_volume_20 = sum(volumes_20) / len(volumes_20) if volumes_20 else 0
        recent_volume = float(last_candle['volume'])

        # ============ 1. ТРЕНДОВА ЛОГІКА (15хв) ============
        distance_to_ema = abs(current_price - ema_50)
        has_stable_volume = recent_volume >= (avg_volume_20 * 0.8)
        candle_body = abs(last_candle['close'] - last_candle['open'])
        has_movement = candle_body >= (atr_15m * 0.1)

        if distance_to_ema <= (atr_15m * 6.0) and has_stable_volume and has_movement:
            fractal_support = find_last_fractal(closed_candles, fractal_type='low')
            fractal_resistance = find_last_fractal(closed_candles, fractal_type='high')

            # --- Висхідний тренд (ЛОНГ) ---
            if current_price > ema_50:
                highs_struct = find_last_two_fractals(closed_candles, fractal_type='high')
                lows_struct = find_last_two_fractals(closed_candles, fractal_type='low')
                is_bullish_structure = (
                    (highs_struct[1] > highs_struct[0] if len(highs_struct) == 2 else True) and
                    (lows_struct[1] > lows_struct[0] if len(lows_struct) == 2 else True)
                )
                space_to_resistance = fractal_resistance - current_price if fractal_resistance else atr_15m * 2
                has_room_for_long = space_to_resistance >= (atr_15m * 1.0)
                has_not_broken_support = last_candle['close'] >= fractal_support if fractal_support else True

                if last_candle['open'] > last_candle['close'] and is_bullish_structure and has_room_for_long and has_not_broken_support:
                    is_near_support = fractal_support and abs(current_price - fractal_support) <= (atr_15m * 1.5)

                    if is_near_support:
                        entry_type = "Від підтримки"
                        stop_loss = round(min(last_candle['low'], fractal_support) - (atr_15m * 0.3), 6)
                        target_by_resistance = fractal_resistance - current_price if fractal_resistance else atr_15m * 3.0
                        tp1_distance = target_by_resistance * 0.8 if fractal_resistance else atr_15m * 2.0
                        tp1 = round(current_price + tp1_distance, 6)
                        tp2 = round(tp1 + (atr_15m * 2.0), 6)
                        tp3 = round(tp2 + (atr_15m * 2.0), 6)
                    else:
                        entry_type = "Від EMA"
                        stop_loss = round(ema_50 - (atr_15m * 1.0), 6)
                        target_by_resistance = fractal_resistance - current_price if fractal_resistance else atr_15m * 3.0
                        tp1_distance = max(target_by_resistance - atr_15m, atr_15m * 0.5)
                        tp1 = round(current_price + tp1_distance, 6)
                        tp2 = round(tp1 + (atr_15m * 2.0), 6)
                        tp3 = round(tp2 + (atr_15m * 2.0), 6)

                    risk = current_price - stop_loss
                    reward = tp1 - current_price

                    if risk > 0 and (reward / risk) >= 1.3:
                        return {
                            "symbol": symbol, "signal": "LONG", "entry": current_price,
                            "stop_loss": stop_loss, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                            "atr": atr_15m, "entry_type": entry_type
                        }, True, f"OK (15m Trend Bullish Long - {entry_type})"
                    else:
                        return None, True, "Тренд лонг: R:R < 1.3"
                else:
                    return None, True, "Тренд лонг: не виконано умови структури або свічки"

            # --- Низхідний тренд (ШОРТ) ---
            elif current_price < ema_50:
                lows_struct = find_last_two_fractals(closed_candles, fractal_type='low')
                highs_struct = find_last_two_fractals(closed_candles, fractal_type='high')
                is_bearish_structure = (
                    (lows_struct[1] < lows_struct[0] if len(lows_struct) == 2 else True) and
                    (highs_struct[1] < highs_struct[0] if len(highs_struct) == 2 else True)
                )
                space_to_support = current_price - fractal_support if fractal_support else atr_15m * 2
                has_room_for_short = space_to_support >= (atr_15m * 1.0)
                has_not_broken_resistance = last_candle['close'] <= fractal_resistance if fractal_resistance else True

                if last_candle['open'] < last_candle['close'] and is_bearish_structure and has_room_for_short and has_not_broken_resistance:
                    is_near_resistance = fractal_resistance and abs(current_price - fractal_resistance) <= (atr_15m * 1.5)

                    if is_near_resistance:
                        entry_type = "Від опору"
                        stop_loss = round(max(last_candle['high'], fractal_resistance) + (atr_15m * 0.3), 6)
                        target_by_support = current_price - fractal_support if fractal_support else atr_15m * 3.0
                        tp1_distance = target_by_support * 0.8 if fractal_support else atr_15m * 2.0
                        tp1 = round(current_price - tp1_distance, 6)
                        tp2 = round(tp1 - (atr_15m * 2.0), 6)
                        tp3 = round(tp2 - (atr_15m * 2.0), 6)
                    else:
                        entry_type = "Від EMA"
                        stop_loss = round(ema_50 + (atr_15m * 1.0), 6)
                        target_by_support = current_price - fractal_support if fractal_support else atr_15m * 3.0
                        tp1_distance = max(target_by_support - atr_15m, atr_15m * 0.5)
                        tp1 = round(current_price - tp1_distance, 6)
                        tp2 = round(tp1 - (atr_15m * 2.0), 6)
                        tp3 = round(tp2 - (atr_15m * 2.0), 6)

                    risk = stop_loss - current_price
                    reward = current_price - tp1

                    if risk > 0 and (reward / risk) >= 1.3:
                        return {
                            "symbol": symbol, "signal": "SHORT", "entry": current_price,
                            "stop_loss": stop_loss, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                            "atr": atr_15m, "entry_type": entry_type
                        }, True, f"OK (15m Trend Bearish Short - {entry_type})"
                    else:
                        return None, True, "Тренд шорт: R:R < 1.3"
                else:
                    return None, True, "Тренд шорт: не виконано умови структури або свічки"

        # ============ 2. ФЛЕТ / ДІАПАЗОН (15хв біля EMA) ============
        is_market_ranging = distance_to_ema <= (atr_15m * 2.5)

        if is_market_ranging:
            zone_threshold = atr_15m * 0.35
            has_spike_volume = recent_volume >= (avg_volume_20 * 1.2)

            is_near_resistance = current_price >= (max_85_high - zone_threshold)
            if is_near_resistance and has_spike_volume:
                recent_tests = count_recent_tests(recent_15_candles, max_85_high, zone_threshold, test_type="resistance")
                has_upper_wick_range = last_upper_wick >= (candle_range * 0.4) if candle_range > 0 else False

                if recent_tests >= 2 and has_upper_wick_range:
                    entry_type = "Флет від опору"
                    stop_loss = round(max(max_85_high, last_candle['high']) + (atr_15m * 0.2), 6)
                    risk = stop_loss - current_price

                    fractal_support_list = find_last_two_fractals(closed_candles, fractal_type='low')
                    target_by_support = (current_price - fractal_support_list[0]) if fractal_support_list else atr_15m * 3.0
                    tp1_distance = target_by_support * 0.8 if fractal_support_list else atr_15m * 2.0

                    tp1 = round(current_price - tp1_distance, 6)
                    tp2 = round(tp1 - (atr_15m * 2.0), 6)
                    tp3 = round(tp2 - (atr_15m * 2.0), 6)
                    reward = current_price - tp1

                    if risk > 0 and (reward / risk) >= 1.3:
                        return {
                            "symbol": symbol, "signal": "SHORT", "entry": current_price,
                            "stop_loss": stop_loss, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                            "atr": atr_15m, "entry_type": entry_type
                        }, True, f"OK (15m Range Resistance Short - {entry_type})"
                    else:
                        return None, True, "Флет шорт: R:R < 1.3"
                else:
                    return None, True, "Флет шорт: мало тестів рівня або немає верхнього ґнота"
            else:
                return None, True, "Флет: ціна не біля опору або немає об'єму для шорта"

        return None, True, ""

    except Exception as e:
        return None, True, f"Помилка аналізу: {str(e)}"
        
        
# --- ВІДКРИТТЯ ПОЗИЦІЇ ---
async def open_position(setup):
    try:
        raw_qty = (MARGIN_USD * LEVERAGE) / setup['entry']
        total_qty = round(raw_qty, 6)
        if total_qty <= 0:
            total_qty = 0.001
        
        qty_50 = round(total_qty * 0.5, 6)
        qty_25_1 = round(total_qty * 0.25, 6)
        qty_25_2 = round(total_qty - qty_50 - qty_25_1, 6)
        if qty_50 <= 0: qty_50 = 0.001
        if qty_25_1 <= 0: qty_25_1 = 0.001
        if qty_25_2 <= 0: qty_25_2 = 0.001

        order_payload = {
            "symbol": setup['symbol'],
            "side": "BUY" if setup['signal'] == "LONG" else "SELL",
            "positionSide": setup['signal'],
            "type": "MARKET",
            "quantity": total_qty,
        }

        res = await bingx_request('POST', '/openApi/swap/v2/trade/order', order_payload)
        if not res or not isinstance(res, dict) or res.get('code') != 0:
            err_msg = res.get('msg', 'Unknown error') if isinstance(res, dict) else 'Connection failed'
            error_text = f"❌ [ПОМИЛКА ВІДКРИТТЯ] {setup['symbol']}: {err_msg}"
            print(error_text, flush=True)
            try:
                await send_telegram(error_text)
            except:
                pass
            raise Exception(error_text)
            
        data_res = res.get("data", {})
        if isinstance(data_res, dict):
            order_info = data_res.get("order", data_res)
            real_qty = float(order_info.get('executedQty', order_info.get('quantity', total_qty)))
            if real_qty > 0:
                total_qty = real_qty
                qty_50 = round(total_qty * 0.5, 6)
                qty_25_1 = round(total_qty * 0.25, 6)
                qty_25_2 = round(total_qty - qty_50 - qty_25_1, 6)

        sl_side = "SELL" if setup['signal'] == "LONG" else "BUY"

        sl_payload = {
            "symbol": setup['symbol'],
            "side": sl_side,
            "positionSide": setup['signal'],
            "type": "STOP_MARKET",
            "stopPrice": float(f"{setup['stop_loss']:.6f}"),
            "quantity": total_qty,
            "workingType": "MARK_PRICE"
        }

        sl_res = await bingx_request('POST', '/openApi/swap/v2/trade/order', sl_payload)
        if not sl_res or (isinstance(sl_res, dict) and sl_res.get('code') != 0):
            err_msg = sl_res.get("msg", "Unknown error") if isinstance(sl_res, dict) else "No response"
            await log_and_alert(f"ПОМИЛКА СТОП-ЛОСУ", f"Не вдалося виставити стоп: {err_msg}", setup['symbol'])

        for i, (tp_price, tp_qty) in enumerate(zip((setup['tp1'], setup['tp2'], setup['tp3']), (qty_50, qty_25_1, qty_25_2)), 1):
            tp_payload = {
                "symbol": setup['symbol'],
                "side": sl_side,
                "positionSide": setup['signal'],
                "type": "TAKE_PROFIT_MARKET",
                "stopPrice": float(f"{tp_price:.6f}"),
                "quantity": tp_qty,
                "workingType": "CONTRACT_PRICE"
            }

            tp_res = await bingx_request('POST', '/openApi/swap/v2/trade/order', tp_payload)
            if not tp_res or (isinstance(tp_res, dict) and tp_res.get('code') != 0):
                err_msg = tp_res.get("msg", "Unknown error") if isinstance(tp_res, dict) else "No response"
                await log_and_alert(f"ПОМИЛКА TP({i})", f"Не вдалося виставити TP({i}): {err_msg}", setup['symbol'])

        active_trade_monitors[setup['symbol']] = {
            "side": setup['signal'], "entry": setup['entry'], "stop_loss": setup['stop_loss'],
            "tp1": setup['tp1'], "tp2": setup['tp2'], "tp3": setup['tp3'],
            "in_breakeven": False, "tp1_hit": False, "tp2_hit": False, "tp3_hit": False, "atr": setup['atr']
        }
        save_trades_state(active_trade_monitors)
        
        entry_type_str = setup.get('entry_type', 'Стандартний')
        msg = (f"🚀 Відкрито позицію ({setup['signal']}) з 3 TP (50/25/25)\n"
               f"📌 Монета: {setup['symbol']}\n"
               f"📌 Тип входу: {entry_type_str}\n"
               f"📍 Вхід: {setup['entry']:.6f}\n"
               f"🛑 Стоп: {setup['stop_loss']:.6f}\n"
               f"🎯 TP1 (50%): {setup['tp1']:.6f}\n"
               f"🎯 TP2 (25%): {setup['tp2']:.6f}\n"
               f"🎯 TP3 (25%): {setup['tp3']:.6f}")
        await send_telegram(msg)
        print(f"[SUCCESS] Успішно відкрито ордер по {setup['symbol']}.", flush=True)

    except Exception as e:
        await log_and_alert("Помилка відкриття позиції", str(e), setup['symbol'])
        raise e

# --- МОНІТОРИНГ УГОД ---
async def monitor_trades_loop():
    while True:
        try:
            positions = await get_exchange_positions()
            active_symbols = [p['symbol'] for p in positions if float(p.get('positionAmt', 0)) != 0]

            for p in positions:
                if float(p.get('positionAmt', 0)) != 0:
                    sym = p['symbol']
                    if sym not in active_trade_monitors:
                        pos_side = p.get('positionSide') or ('LONG' if float(p.get('positionAmt', 0)) > 0 else 'SHORT')
                        entry_p = float(p.get('entryPrice', 0))
                        current_qty = abs(float(p.get('positionAmt', 0)))
                        
                        # Перевіряємо реальні ордери на біржі, чи стоп на ТВХ або вже в плюсі
                        is_be = False
                        try:
                            orders_res = await bingx_request('GET', '/openApi/swap/v2/trade/openOrders', {'symbol': sym})
                            orders_list = orders_res.get('data', []) if isinstance(orders_res, dict) else []
                            if isinstance(orders_list, dict):
                                orders_list = orders_list.get('orders', [])
                            
                            for ord_item in orders_list:
                                if ord_item.get('type') == 'STOP_MARKET':
                                    stop_price = float(ord_item.get('stopPrice', 0))
                                    
                                    # Перевірка для Лонга та Шорта (безубиток або плюс)
                                    if pos_side == 'LONG' and stop_price <= entry_p:
                                        is_be = True
                                        break
                                    elif pos_side == 'SHORT' and stop_price >= entry_p:
                                        is_be = True
                                        break
                        except Exception as ex:
                            print(f"[INFO] Не вдалося перевірити стоп для {sym}: {ex}", flush=True)

                        active_trade_monitors[sym] = {
                            'entry': entry_p,
                            'stop_loss': entry_p if is_be else entry_p,
                            'initial_qty': current_qty,
                            'in_breakeven': is_be,
                            'tp1_hit': is_be,
                            'tp2_hit': False,
                            'tp3_hit': False,
                            'side': pos_side,
                            'opp_volume_count': 0,
                            'last_opp_alert_time': 0
                        }
                        print(f"[INFO] Відновлено позицію: {sym} (Стоп в БУ/плюсі: {is_be})", flush=True)
                        

            for symbol, data in list(active_trade_monitors.items()):
                current_pos_data = next((p for p in positions if p.get('symbol') == symbol), None)

                if symbol not in active_symbols or not current_pos_data or float(current_pos_data.get('positionAmt', 0)) == 0:
                    if data.get('tp3_hit', False):
                        await send_telegram(f"🔥 **Всі цілі (ТР3) досягнуто!** Позиція по {symbol} повністю закрита.")
                    else:
                        await send_telegram(f"🛑 **Стоп по позиції!** Позицію по {symbol} закрито за стоп-лоссом.")
                    del active_trade_monitors[symbol]
                    save_trades_state(active_trade_monitors)
                    continue

                current_qty = abs(float(current_pos_data.get('positionAmt', 0)))

                if 'initial_qty' not in data:
                    data['initial_qty'] = current_qty
                    save_trades_state(active_trade_monitors)
                    continue

                initial_qty = data['initial_qty']
                side_val = str(data.get('side', 'LONG')).upper()

                if current_qty < initial_qty:
                    ratio = current_qty / initial_qty

                    if ratio <= 0.05 and not data.get('tp3_hit', False):
                        data['tp3_hit'] = True
                        data['initial_qty'] = current_qty
                        save_trades_state(active_trade_monitors)
                        await send_telegram(f"🔥 **ТР3 досягнуто!** Всі цілі відпрацьовано по {symbol}")
                    elif ratio <= 0.30 and not data.get('tp2_hit', False):
                        data['tp2_hit'] = True
                        data['initial_qty'] = current_qty
                        save_trades_state(active_trade_monitors)
                        await send_telegram(f"🔴 **ТР2 досягнуто!** Монета: {symbol}")
                    elif ratio <= 0.55 and not data.get('tp1_hit', False):
                        data['tp1_hit'] = True
                        data['in_breakeven'] = True
                        data['initial_qty'] = current_qty
                        save_trades_state(active_trade_monitors)
                        await send_telegram(f"🔴 **TP1 досягнуто!** Стоп-лосс успішно перенесено в безубиток...")

                        try:
                            orders_res = await bingx_request('GET', '/openApi/swap/v2/trade/openOrders', {'symbol': symbol})
                            orders_list = orders_res.get('data', []) if isinstance(orders_res, dict) else []
                            if isinstance(orders_list, dict):
                                orders_list = orders_list.get('orders', [])

                            for ord_item in orders_list:
                                if ord_item.get('type') == 'STOP_MARKET':
                                    ord_id = ord_item.get('orderId')
                                    if ord_id:
                                        await bingx_request('DELETE', '/openApi/swap/v2/trade/order', {'symbol': symbol, 'orderId': ord_id})

                            
                            # Отримуємо свіжі дані по позиції прямо з біржі перед перенесенням стопу
                            pos_res = await bingx_request('GET', '/openApi/swap/v2/user/positions', {'symbol': symbol})
                            pos_data_list = pos_res.get('data', []) if isinstance(pos_res, dict) else []
        
                            # Знаходимо точний залишок і ціну входу
                            exact_qty = 0.0
                            entry_price = float(data.get('entry', 0))
        
                            for p in pos_data_list:
                                if float(p.get('positionAmt', 0)) != 0:
                                    exact_qty = abs(float(p.get('positionAmt', 0)))
                                    entry_price = float(p.get('entryPrice', entry_price))
                                    break
        
                            if exact_qty == 0:
                                exact_qty = abs(float(current_qty))
                             # Визначаємо сторона стоп-ордера (для LONG це SELL, для SHORT це BUY)
                            sl_side = "SELL" if side_val == "LONG" else "BUY"
                            sl_payload = {
                                "symbol": symbol,
                                "side": sl_side,
                                "positionSide": side_val,
                                "type": "STOP_MARKET",
                                "stopPrice": float(f"{entry_price:.6f}"),
                                "quantity": float(f"{exact_qty:.6f}"),
                                "workingType": "MARK_PRICE"
                            }
                            res = await bingx_request('POST', '/openApi/swap/v2/trade/order', sl_payload)
                            if isinstance(res, dict) and res.get('code') != 0:
                                err_msg = f"❌ [ПОМИЛКА БІРЖІ] Не вдалося поставити стоп у БУ для {symbol}: {res}"
                                print(err_msg, flush=True)
                                await send_telegram(err_msg)
                        except Exception as ex:
                            err_msg = f"❌ [КРИТИЧНА ПОМИЛКА] Помилка перенесення стопа в БУ для {symbol}: {str(ex)}"
                            print(err_msg, flush=True)
                            await send_telegram(err_msg)

        except Exception as ex:
            print(f"[LOG ERROR] Помилка в моніторингу угоди: {ex}", flush=True)

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

async def count_risk_positions():
    try:
        risk_count = 0
        positions = await get_exchange_positions()
        open_symbols = [p['symbol'] for p in positions if float(p.get('positionAmt', 0)) != 0]

        for p in positions:
            qty = float(p.get('positionAmt', 0))
            if qty != 0:
                symbol = p['symbol']
                pos_side = p.get('positionSide') or ('LONG' if qty > 0 else 'SHORT')
                entry_p = float(p.get('entryPrice', 0))

                # Якщо позиція є в активному моніторингу і вона вже в БУ/плюсі — не рахуємо як ризик
                if symbol in active_trade_monitors:
                    if active_trade_monitors[symbol].get('in_breakeven', False):
                        continue  # Захищена, пропускаємо
                    else:
                        risk_count += 1  # Ще ризикова
                else:
                    # Якщо чомусь немає в моніторингу, перевіряємо стоп на біржі прямо тут
                    is_protected = False
                    try:
                        orders_res = await bingx_request('GET', '/openApi/swap/v2/trade/openOrders', {'symbol': symbol})
                        orders_list = orders_res.get('data', []) if isinstance(orders_res, dict) else []
                        if isinstance(orders_list, dict):
                            orders_list = orders_list.get('orders', [])
                        
                        for ord_item in orders_list:
                            if ord_item.get('type') == 'STOP_MARKET':
                                stop_price = float(ord_item.get('stopPrice', 0))
                                if pos_side == 'LONG' and stop_price <= entry_p:
                                    is_protected = True
                                    break
                                elif pos_side == 'SHORT' and stop_price >= entry_p:
                                    is_protected = True
                                    break
                    except Exception:
                        pass

                    if not is_protected:
                        risk_count += 1

        return risk_count
    except Exception as e:
        print(f"Помилка підрахунку ризикових позицій: {e}", flush=True)
        return 0
        
# --- ГОЛОВНИЙ ЦИКЛ СКАНУВАННЯ ---
async def main_scanner():
    asyncio.create_task(monitor_trades_loop())
    asyncio.create_task(report_loop())
    asyncio.create_task(self_ping_loop())

    threading.Thread(target=run_http_server, daemon=True).start()

    while True:
        try:
            print("Початок нового циклу сканування ринку...", flush=True)

            risk_pos_count = await count_risk_positions()
            print(f"[INFO] Кількість відкритих ризикових позицій: {risk_pos_count}", flush=True)

            # Якщо ризикових позицій 2 або більше - чекаємо 1 хвилину і перевіряємо знову
            if risk_pos_count >= 2:
                print(f"[INFO] Ліміт ризикових позицій вичерпано ({risk_pos_count}/2). Пауза 1 хвилина...", flush=True)
                await asyncio.sleep(60)
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

            skipped_reasons = {}
            scanned_count = 0
            volume_passed_count = 0

            for symbol in symbols:
                if symbol in active_symbols or is_blacklisted(symbol):
                    continue

                vol_24h = ticker_volumes.get(symbol, 0)
                if vol_24h > 0 and not (MIN_24H_VOLUME <= vol_24h <= MAX_24H_VOLUME):
                    continue

                if symbol in coin_cooldowns and time.time() < coin_cooldowns[symbol]:
                    continue

                try:
                    res = await analyze_market(symbol)
                    if isinstance(res, tuple) and len(res) == 3:
                        setup, has_spike, reason = res
                    else:
                        setup, has_spike, res_text = res, False, "Невідома примітка"
                        reason = res_text

                    # Якщо примітка НЕ пов'язана з відсутністю 1m об'єму, значить монета пройшла цей фільтр
                    if "Немає сплеску об'єму на 1m" not in reason:
                        volume_passed_count += 1

                    if setup:
                        await open_position(setup)
                        break
                    else:
                        if reason and isinstance(reason, str) and reason.strip():
                            if reason not in skipped_reasons:
                                skipped_reasons[reason] = []
                            skipped_reasons[reason].append(symbol)

                except Exception as e:
                    coin_cooldowns[symbol] = time.time() + 300
                    print(f"[ERROR] Помилка аналізу {symbol}: {e}", flush=True)

                scanned_count += 1

            # Виводимо розширену статистику в логи Render
            print(f"[INFO] Сканування завершено. Перевірено монет: {scanned_count} | Пройшли 1m об'єм: {volume_passed_count} | Зібрано причин: {len(skipped_reasons)}", flush=True)

            # Звіт по відсіяних монетах (фільтруємо масовий об'єм і залишаємо лише деталі по інших умовах)
            if skipped_reasons:
                # Виключаємо категорію відсутності хвилинного об'єму звітів
                filtered_reasons = {
                    reason: syms for reason, syms in skipped_reasons.items() 
                    if "об'єм" not in reason.lower()
                }

                if filtered_reasons:
                    print(f"[INFO] Сформовано детальний звіт (категорій: {len(filtered_reasons)}).", flush=True)
                    full_report = "📊 Детальний звіт сканування (Фільтри умов):\n\n"
                    
                    for reason_text, syms in filtered_reasons.items():
                        symbols_str = ", ".join(syms)
                        full_report += f"▪️ {reason_text} ({len(syms)} шт.):\n {symbols_str}\n\n"
                    
                    # Розбиваємо на частини при потребі
                    MAX_LEN = 3500
                    chunks = []
                    while len(full_report) > MAX_LEN:
                        split_idx = full_report.rfind('\n\n', 0, MAX_LEN)
                        if split_idx == -1:
                            split_idx = MAX_LEN
                        chunks.append(full_report[:split_idx])
                        full_report = full_report[split_idx:].lstrip()
                    if full_report:
                        chunks.append(full_report)

                    try:
                        print(f"[INFO] Надсилаємо детальний звіт у Telegram (частин: {len(chunks)})...", flush=True)
                        for chunk in chunks:
                            await send_telegram(chunk)
                            await asyncio.sleep(0.5)
                        print("[INFO] Детальний звіт успішно надіслано в Telegram!", flush=True)
                    except Exception as e:
                        print(f"[ERROR] Помилка надсилання звіту в Telegram: {e}", flush=True)
                else:
                    print("[INFO] Окрім об'єму інших відсіювань немає, звіт пропущено.", flush=True)
            else:
                print("[INFO] Словник skipped_reasons порожній.", flush=True)

        except Exception as e:
            log_and_alert("Помилка в головному циклі сканування", str(e))

        await asyncio.sleep(60)
        

if __name__ == "__main__":
    try:
        asyncio.run(main_scanner())
    except KeyboardInterrupt:
        print("[INFO] Бот зупинений користувачем.", flush=True)
        
