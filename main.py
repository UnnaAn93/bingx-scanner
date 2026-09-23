import asyncio
import aiohttp
import os
import time
import hmac
import hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# Налаштування параметрів сканування та супроводу (15m)
VOLUME_MULTIPLIER = 2.2           
APPROACH_PERCENT = 0.008          
TIMEFRAME = "15m"                 
LIMIT_CANDLES = 100               
TOP_COINS_LIMIT = 150             
MIN_24H_VOLUME_USDT = 5_000_000   
COOLDOWN_SECONDS = 300            

LEVERAGE = 10                     
BOT_MARGIN_USDT = 0.5             

API_KEY = os.environ.get("BINGX_API_KEY", "")
API_SECRET = os.environ.get("BINGX_SECRET_KEY", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
RENDER_URL = os.environ.get("RENDER_URL", "https://bingx-scanner-djbf.onrender.com")

BINGX_BASE_URL = "https://open-api.bingx.com"

last_alert_time = {}
last_position_alert_time = {}     
last_opposite_alert_time = {}     
handled_partial_positions = set() 

def get_sign(api_secret, payload):
    return hmac.new(api_secret.encode("utf-8"), payload.encode("utf-8"), digestmod=hashlib.sha256).hexdigest()

async def send_to_discord(session, webhook_url, message):
    if not webhook_url:
        return
    payload = {"content": message}
    try:
        async with session.post(webhook_url, json=payload) as response:
            pass
    except Exception as e:
        print(f"Помилка Discord: {e}", flush=True)

def calculate_ema(closes, period=50):
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def calculate_atr(kline_data, period=14):
    if len(kline_data) < 2:
        return 0.0
    tr_list = []
    for i in range(1, len(kline_data)):
        high = float(kline_data[i]['high'])
        low = float(kline_data[i]['low'])
        prev_close = float(kline_data[i - 1]['close'])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)
    
    if len(tr_list) < period:
        return tr_list[-1] if tr_list else 0.0
    return sum(tr_list[-period:]) / period

def calculate_kdj(kline_data, n=9, m1=3, m2=3):
    if len(kline_data) < n:
        return None, None, None

    k_list, d_list, j_list = [], [], []
    k, d = 50.0, 50.0

    for i in range(len(kline_data)):
        if i < n - 1:
            k_list.append(50.0)
            d_list.append(50.0)
            j_list.append(50.0)
            continue

        window = kline_data[i - n + 1 : i + 1]
        highest_high = max(x['high'] for x in window)
        lowest_low = min(x['low'] for x in window)
        close_price = kline_data[i]['close']

        if highest_high == lowest_low:
            rsv = 50.0
        else:
            rsv = (close_price - lowest_low) / (highest_high - lowest_low) * 100

        k = ( (m1 - 1) * k + rsv ) / m1
        d = ( (m2 - 1) * d + k ) / m2
        j = 3 * k - 2 * d

        k_list.append(k)
        d_list.append(d)
        j_list.append(j)

    return k_list, d_list, j_list

async def fetch_open_positions(session):
    if not API_KEY or not API_SECRET:
        return []
    path = "/openApi/swap/v2/user/positions"
    timestamp = str(int(time.time() * 1000))
    params_str = f"timestamp={timestamp}"
    signature = get_sign(API_SECRET, params_str)
    url = f"{BINGX_BASE_URL}{path}?{params_str}&signature={signature}"
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        async with session.get(url, headers=headers, timeout=5) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("code") == 0:
                    positions = data.get("data", [])
                    return [p for p in positions if float(p.get("positionAmt", 0)) != 0]
            else:
                print(f"Помилка отримання позицій статус: {response.status}", flush=True)
    except Exception as e:
        print(f"Виняток у fetch_open_positions: {e}", flush=True)
    return []

async def set_leverage(session, symbol, leverage, side):
    path = "/openApi/swap/v2/trade/leverage"
    timestamp = str(int(time.time() * 1000))
    position_side = "LONG" if side == "LONG" else "SHORT"
    param_str = f"leverage={leverage}&positionSide={position_side}&symbol={symbol}&timestamp={timestamp}"
    signature = get_sign(API_SECRET, param_str)
    url = f"{BINGX_BASE_URL}{path}?{param_str}&signature={signature}"
    headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/json"}
    try:
        async with session.post(url, headers=headers, timeout=5) as response:
            if response.status != 200:
                text = await response.text()
                print(f"Помилка встановлення плеча для {symbol}: {text}", flush=True)
    except Exception as e:
        print(f"Виняток у set_leverage для {symbol}: {e}", flush=True)

async def set_initial_stop_loss(session, symbol, side, level_price, kline_data):
    path = "/openApi/swap/v2/trade/stopOrder"
    timestamp = str(int(time.time() * 1000))
    atr_val = calculate_atr(kline_data, period=14)
    multiplier = 1.5 
    
    if side == "LONG":
        stop_price = level_price - (multiplier * atr_val)
        stop_side = "SELL"
        position_side_param = "LONG"
    else:
        stop_price = level_price + (multiplier * atr_val)
        stop_side = "BUY"
        position_side_param = "SHORT"
        
    stop_price = round(stop_price, 5)
    
    param_str = f"positionSide={position_side_param}&side={stop_side}&stopPrice={stop_price}&symbol={symbol}&timestamp={timestamp}&type=STOP_MARKET"
    signature = get_sign(API_SECRET, param_str)
    url = f"{BINGX_BASE_URL}{path}?{param_str}&signature={signature}"
    headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/json"}
    
    try:
        async with session.post(url, headers=headers, timeout=5) as response:
            if response.status == 200:
                res_data = await response.json()
                if res_data.get("code") == 0:
                    return stop_price
                else:
                    print(f"Біржа відхилила стоп-лос для {symbol}: {res_data}", flush=True)
            else:
                text = await response.text()
                print(f"Помилка HTTP стоп-лосу для {symbol}: {text}", flush=True)
    except Exception as e:
        print(f"Виняток у set_initial_stop_loss для {symbol}: {e}", flush=True)
    return None

async def open_bot_position(session, symbol, side, current_price, level_price, kline_data, discord_webhook_url):
    if not API_KEY or not API_SECRET:
        return
    
    await set_leverage(session, symbol, LEVERAGE, side)
    
    path = "/openApi/swap/v2/trade/order"
    timestamp = str(int(time.time() * 1000))
    
    position_value_usdt = BOT_MARGIN_USDT * LEVERAGE
    quantity = round(position_value_usdt / current_price, 4)
    if quantity <= 0:
        return

    order_side = "BUY" if side == "LONG" else "SELL"
    position_side_param = "LONG" if side == "LONG" else "SHORT"
    
    param_str = f"positionSide={position_side_param}&quantity={quantity}&side={order_side}&symbol={symbol}&timestamp={timestamp}&type=MARKET"
    signature = get_sign(API_SECRET, param_str)
    url = f"{BINGX_BASE_URL}{path}?{param_str}&signature={signature}"
    headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/json"}
    
    try:
        async with session.post(url, headers=headers, timeout=5) as response:
            if response.status == 200:
                res_data = await response.json()
                if res_data.get("code") == 0:
                    stop_price = await set_initial_stop_loss(session, symbol, side, level_price, kline_data)
                    msg = (
                        f"🤖🚀 **БОТ ВІДКРИВ УГОДУ [{side}] (З EMA)**:\n`{symbol}`\n"
                        f"• Вхід: `{current_price}` | Рівень: `{level_price}`\n"
                        f"• Маржа: `{BOT_MARGIN_USDT} USDT` (Плече: `{LEVERAGE}x`)\n"
                        f"• Кількість: `{quantity}`\n"
                        f"🛡️ ATR Стоп-лос: `{stop_price}`"
                    )
                    await send_to_discord(session, discord_webhook_url, msg)
                else:
                    err_msg = f"❌ **БІРЖА ВІДХИНИЛА ОРДЕР (`{symbol}`)**:\n`{res_data}`"
                    print(err_msg, flush=True)
                    await send_to_discord(session, discord_webhook_url, err_msg)
            else:
                text = await response.text()
                print(f"HTTP Помилка відкриття ордера {symbol}: {text}", flush=True)
    except Exception as e:
        print(f"Виняток при відкритті ордера {symbol}: {e}", flush=True)

async def close_partial_position_on_exchange(session, symbol, side, quantity_to_close, discord_webhook_url):
    if not API_KEY or not API_SECRET:
        return False
    path = "/openApi/swap/v2/trade/order"
    timestamp = str(int(time.time() * 1000))
    close_side = "SELL" if side == "LONG" else "BUY"
    position_side_param = "LONG" if side == "LONG" else "SHORT"
    param_str = f"positionSide={position_side_param}&quantity={quantity_to_close}&side={close_side}&symbol={symbol}&timestamp={timestamp}&type=MARKET"
    signature = get_sign(API_SECRET, param_str)
    url = f"{BINGX_BASE_URL}{path}?{param_str}&signature={signature}"
    headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/json"}
    try:
        async with session.post(url, headers=headers, timeout=5) as response:
            if response.status == 200:
                res_data = await response.json()
                if res_data.get("code") == 0:
                    return True
                else:
                    print(f"Помилка часткового закриття {symbol}: {res_data}", flush=True)
            else:
                text = await response.text()
                print(f"HTTP Помилка часткового закриття {symbol}: {text}", flush=True)
    except Exception as e:
        print(f"Виняток у close_partial_position_on_exchange для {symbol}: {e}", flush=True)
    return False

async def set_break_even_stop(session, symbol, side, entry_price):
    if not API_KEY or not API_SECRET:
        return False
    path = "/openApi/swap/v2/trade/stopOrder"
    timestamp = str(int(time.time() * 1000))
    stop_side = "SELL" if side == "LONG" else "BUY"
    position_side_param = "LONG" if side == "LONG" else "SHORT"
    param_str = f"positionSide={position_side_param}&side={stop_side}&stopPrice={entry_price}&symbol={symbol}&timestamp={timestamp}&type=STOP_MARKET"
    signature = get_sign(API_SECRET, param_str)
    url = f"{BINGX_BASE_URL}{path}?{param_str}&signature={signature}"
    headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/json"}
    try:
        async with session.post(url, headers=headers, timeout=5) as response:
            if response.status == 200:
                res_data = await response.json()
                if res_data.get("code") == 0:
                    return True
                else:
                    print(f"Помилка встановлення б/у стопу для {symbol}: {res_data}", flush=True)
            else:
                text = await response.text()
                print(f"HTTP Помилка б/у стопу для {symbol}: {text}", flush=True)
    except Exception as e:
        print(f"Виняток у set_break_even_stop для {symbol}: {e}", flush=True)
    return False

async def fetch_top_bingx_symbols(session):
    url = f"{BINGX_BASE_URL}/openApi/swap/v2/quote/ticker"
    try:
        async with session.get(url, timeout=5) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("code") == 0:
                    list_tickers = data.get("data", [])
                    usdt_tickers = []
                    for t in list_tickers:
                        symbol = t.get("symbol", "")
                        if symbol.startswith("NC") or "2USD" in symbol or not symbol.endswith("-USDT"):
                            continue
                        try:
                            quote_vol = float(t.get("volume", 0)) * float(t.get("lastPrice", 0))
                            if quote_vol >= MIN_24H_VOLUME_USDT:
                                usdt_tickers.append((symbol, quote_vol))
                        except Exception:
                            continue
                    usdt_tickers.sort(key=lambda x: x[1], reverse=True)
                    return [item[0] for item in usdt_tickers[:TOP_COINS_LIMIT]]
    except Exception as e:
        print(f"Помилка отримання топ тикерів: {e}", flush=True)
    return []

async def fetch_kline_data(session, symbol):
    url = f"{BINGX_BASE_URL}/openApi/swap/v2/quote/klines"
    params = {"symbol": symbol, "interval": TIMEFRAME, "limit": str(LIMIT_CANDLES)}
    try:
        async with session.get(url, params=params, timeout=4) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("code") == 0:
                    raw_list = data.get("data", [])
                    raw_list.sort(key=lambda x: int(x.get("time", x.get("openTime", 0))))
                    formatted = []
                    for item in raw_list:
                        formatted.append({
                            "open": float(item.get("open", 0)),
                            "high": float(item.get("high", 0)),
                            "low": float(item.get("low", 0)),
                            "close": float(item.get("close", 0)),
                            "volume": float(item.get("volume", 0)),
                            "time": int(item.get("time", item.get("openTime", 0)))
                        })
                    return formatted
    except Exception as e:
        print(f"Помилка отримання свічок для {symbol}: {e}", flush=True)
    return None

async def scan_and_trade_coin(session, symbol, discord_webhook_url, current_open_count):
    if current_open_count >= 2:
        return

    current_time = time.time()
    if symbol in last_alert_time and current_time - last_alert_time[symbol] < COOLDOWN_SECONDS:
        return

    kline_data = await fetch_kline_data(session, symbol)
    if not kline_data or len(kline_data) < 60:
        return

    try:
        closes = [x['close'] for x in kline_data]
        volumes = [x['volume'] for x in kline_data]
        lows = [x['low'] for x in kline_data]
        highs = [x['high'] for x in kline_data]
        
        current_volume = volumes[-1]
        if current_volume <= 0:
            return

        avg_volume = sum(volumes[:-1]) / (len(volumes) - 1)
        if avg_volume <= 0:
            return
        
        current_price = closes[-1]
        
        if current_volume < (avg_volume * VOLUME_MULTIPLIER):
            return

        ema_value = calculate_ema(closes, period=50)

        support_level = min(lows[:-1])
        if support_level > 0 and 0 <= (current_price - support_level) / support_level <= APPROACH_PERCENT:
            if current_price > ema_value:
                last_alert_time[symbol] = current_time
                await open_bot_position(session, symbol, "LONG", current_price, support_level, kline_data, discord_webhook_url)
                return

        resistance_level = max(highs[:-1])
        if resistance_level > 0 and 0 <= (resistance_level - current_price) / resistance_level <= APPROACH_PERCENT:
            if current_price < ema_value:
                last_alert_time[symbol] = current_time
                await open_bot_position(session, symbol, "SHORT", current_price, resistance_level, kline_data, discord_webhook_url)
                return
    except Exception as e:
        print(f"Помилка в scan_and_trade_coin для {symbol}: {e}", flush=True)

async def monitor_active_position(session, pos, discord_webhook_url):
    global last_position_alert_time, last_opposite_alert_time, handled_partial_positions
    sym = pos.get("symbol")
    amt_raw = float(pos.get("positionAmt", 0))
    entry_price = float(pos.get("avgPrice", 0))
    pnl = pos.get("unrealizedProfit", "0")
    side = "LONG" if amt_raw > 0 else "SHORT"
    abs_amt = abs(amt_raw)

    kline_data = await fetch_kline_data(session, sym)
    if not kline_data or len(kline_data) < 15:
        return

    k_vals, d_vals, j_vals = calculate_kdj(kline_data)
    if not j_vals or len(j_vals) < 2:
        return

    curr_j, prev_j = j_vals[-1], j_vals[-2]
    curr_k, prev_k = k_vals[-1], k_vals[-2]

    last_candle = kline_data[-1]
    current_volume = last_candle['volume']
    current_price = last_candle['close']
    open_price = last_candle['open']
    
    prev_avg_volume = sum(x['volume'] for x in kline_data[-11:-1]) / 10

    # Умови для часткового тейку (75%)
    is_kdj_take_profit = False
    # Умови для повного закриття залишку (25%), наприклад сильний рух далі або зворотний перетин/перекупленість
    is_kdj_full_close = False

    if side == "LONG":
        if prev_j > 85 and (curr_j < curr_k and prev_j >= prev_k) and current_price >= entry_price * 1.01:
            if sym in handled_partial_positions:
                is_kdj_full_close = True  # Якщо 75% вже закрили, наступний сигнал KDJ закриває решту 25%
            else:
                is_kdj_take_profit = True
    elif side == "SHORT":
        if prev_j < 15 and (curr_j > curr_k and prev_j <= prev_k) and current_price <= entry_price * 0.99:
            if sym in handled_partial_positions:
                is_kdj_full_close = True
            else:
                is_kdj_take_profit = True

    is_opposite_volume = False
    if current_volume >= (prev_avg_volume * 1.5):
        if side == "LONG" and current_price < open_price:  
            is_opposite_volume = True
        elif side == "SHORT" and current_price > open_price: 
            is_opposite_volume = True

    current_time = time.time()
    last_pos_time = last_position_alert_time.get(sym, 0)
    last_opp_time = last_opposite_alert_time.get(sym, 0)

    # 1. Частковий тейк 75%
    if is_kdj_take_profit and sym not in handled_partial_positions:
        partial_qty = round(abs_amt * 0.75, 4)
        if partial_qty > 0:
            success_close = await close_partial_position_on_exchange(session, sym, side, partial_qty, discord_webhook_url)
            if success_close:
                await set_break_even_stop(session, sym, side, entry_price)
                handled_partial_positions.add(sym)
                report_msg = (
                    f"🎯 **ЧАСТКОВИЙ ТЕЙК 75% `{sym}` ({side})**:\n"
                    f"• Вхід: `{entry_price}` | Ціна: `{current_price}`\n"
                    f"• PnL: `{pnl} USDT` | Стоп у безубитку!"
                )
                await send_to_discord(session, discord_webhook_url, report_msg)

    # 2. Закриття залишку 25% за сигналом KDJ
    elif is_kdj_full_close:
        success_close = await close_partial_position_on_exchange(session, sym, side, abs_amt, discord_webhook_url)
        if success_close:
            if sym in handled_partial_positions:
                handled_partial_positions.remove(sym)
            report_msg = (
                f"🏁 **ПОВНЕ ЗАКРИТТЯ ЗАЛИШКУ 25% `{sym}` ({side})**:\n"
                f"• Ціна виходу: `{current_price}` | PnL: `{pnl} USDT`"
            )
            await send_to_discord(session, discord_webhook_url, report_msg)

    elif is_opposite_volume and (current_time - last_opp_time >= 180):
        report_msg = (
            f"📊 **Супровід `{sym}` ({side})**:\n"
            f"⚠️ Увага: Великий об'єм у протилежному напрямку!"
        )
        await send_to_discord(session, discord_webhook_url, report_msg)
        last_opposite_alert_time[sym] = current_time

    elif current_time - last_pos_time >= 900:
        report_msg = (
            f"📊 **Супровід позиції `{sym}` ({side})**:\n"
            f"• Вхід: `{entry_price}` | Ціна: `{current_price}` | PnL: `{pnl} USDT`"
        )
        awai
