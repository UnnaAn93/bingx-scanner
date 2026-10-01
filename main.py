import asyncio
import aiohttp
import os
import time
import hmac
import hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

VOLUME_MULTIPLIER = 1.6
MOMENTUM_VOLUME_MULTIPLIER = 2.5
APPROACH_PERCENT = 0.015
TIMEFRAME = "15m"
LIMIT_CANDLES = 100
TOP_COINS_LIMIT = 400
MIN_24H_VOLUME_USDT = 500_000
COOLDOWN_SECONDS = 300  # Змінено на 5 хвилин
LEVERAGE = 10
BOT_MARGIN_USDT = 1.0

API_KEY = os.environ.get("BINGX_API_KEY", "")
API_SECRET = os.environ.get("BINGX_SECRET_KEY", "")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

RENDER_URL = os.environ.get("RENDER_URL", "https://bingx-scanner-djbf.onrender.com")
BINGX_BASE_URL = "https://open-api.bingx.com"

last_alert_time = {}
last_position_alert_time = {}
handled_partial_positions = set()
partial_exit_prices = {}
position_open_time = {}
support_touches_count = {}
resistance_touches_count = {}
last_touch_candle_time = {}

def get_sign(secret, payload):
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()

async def send_to_telegram(session, msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ПОМИЛКА: TELEGRAM_BOT_TOKEN або TELEGRAM_CHAT_ID не вказані")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown"
        }
        headers = {"User-Agent": "Mozilla/5.0 (Compatible; TelegramBot/1.0)"}
        async with session.post(url, json=payload, headers=headers, timeout=5) as r:
            resp_text = await r.text()
            if r.status >= 400:
                print(f"ПОМИЛКА Telegram API [{r.status}]: {resp_text}")
            else:
                print("Повідомлення успішно надіслано в Telegram")
    except Exception as e:
        print(f"Виняток при відправці в Telegram: {e}", flush=True)

def calculate_ema(closes, period=50):
    if len(closes) < period: return closes[-1] if closes else 0.0
    mult = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = (p - ema) * mult + ema
    return ema

def calculate_atr(kdata, period=14):
    if len(kdata) < 2: return 0.0
    tr = []
    for i in range(1, len(kdata)):
        h, l, pc = float(kdata[i]['high']), float(kdata[i]['low']), float(kdata[i-1]['close'])
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(tr[-period:]) / period if len(tr) >= period else (tr[-1] if tr else 0.0)

def calculate_kd(kdata, n=9, m1=3, m2=3):
    try:
        if not kdata or len(kdata) < n: 
            return [50.0], [50.0], [50.0]
        
        closes = [float(x['close']) for x in kdata]
        lows = [float(x['low']) for x in kdata]
        highs = [float(x['high']) for x in kdata]
        
        k_list = []
        d_list = []
        j_list = []
        
        rsv_list = []
        for i in range(len(kdata)):
            if i < n - 1:
                rsv_list.append(50.0)
                continue
            sub_lows = lows[i - n + 1 : i + 1]
            sub_highs = highs[i - n + 1 : i + 1]
            l_val = min(sub_lows)
            h_val = max(sub_highs)
            c_val = closes[i]
            
            if h_val - l_val == 0:
                rsv = 50.0
            else:
                rsv = (c_val - l_val) / (h_val - l_val) * 100
            rsv_list.append(rsv)
            
        k = 50.0
        d = 50.0
        for rsv in rsv_list:
            k = (2 / 3) * k + (1 / 3) * rsv
            d = (2 / 3) * d + (1 / 3) * k
            j = 3 * k - 2 * d
            k_list.append(k)
            d_list.append(d)
            j_list.append(j)
            
        return k_list, d_list, j_list
    except Exception:
        return [50.0], [50.0], [50.0]

async def fetch_open_positions(session):
    if not API_KEY or not API_SECRET: return []
    path = "/openApi/swap/v2/user/positions"
    ts = str(int(time.time() * 1000)) - 5000
    sig = get_sign(API_SECRET, f"timestamp={ts}")
    url = f"{BINGX_BASE_URL}{path}?timestamp={ts}&signature={sig}"
    try:
        async with session.get(url, headers={"X-BX-APIKEY": API_KEY}, timeout=5) as r:
            if r.status == 200:
                data = await r.json()
                if data.get("code") == 0:
                    d = data.get("data")
                    if isinstance(d, list):
                        return [p for p in d if isinstance(p, dict) and float(p.get("positionAmt", 0)) != 0]
    except: pass
    return []

async def set_leverage(session, symbol, lev, side):
    path = "/openApi/swap/v2/trade/leverage"
    ts = str(int(time.time() * 1000)) - 5000
    p_side = "LONG" if side == "LONG" else "SHORT"
    p_str = f"leverage={lev}&positionSide={p_side}&symbol={symbol}&timestamp={ts}"
    sig = get_sign(API_SECRET, p_str)
    try:
        async with session.post(f"{BINGX_BASE_URL}{path}?{p_str}&signature={sig}", headers={"X-BX-APIKEY": API_KEY}, timeout=5) as r:
            
            pass
    except: pass

async def cancel_existing_stop_orders(session, symbol):
    try:
        path = "/openApi/swap/v2/trade/openOrders"
        ts = str(int(time.time() * 1000)) - 5000
        p_str = f"symbol={symbol}&timestamp={ts}"
        sig = get_sign(API_SECRET, p_str)
        async with session.get(f"{BINGX_BASE_URL}{path}?{p_str}&signature={sig}", headers={"X-BX-APIKEY": API_KEY}) as r:
            if r.status == 200:
                data = await r.json()
                orders = data.get("data", {}).get("orders", [])
                for ord in orders:
                    if ord.get("type") in ["STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT"]:
                        order_id = ord.get("orderId")
                        del_path = "/openApi/swap/v2/trade/order"
                        del_ts = str(int(time.time() * 1000)) - 5000
                        del_p_str = f"orderId={order_id}&symbol={symbol}&timestamp={del_ts}"
                        del_sig = get_sign(API_SECRET, del_p_str)
                        await session.delete(f"{BINGX_BASE_URL}{del_path}?{del_p_str}&signature={del_sig}", headers={"X-BX-APIKEY": API_KEY})
    except Exception as e:
        print(f"Помилка при скасуванні старих ордерів для {symbol}: {e}", flush=True)

async def set_initial_stop_loss(session, symbol, side, level, kdata, qty):
    await cancel_existing_stop_orders(session, symbol)
    
    path = "/openApi/swap/v2/trade/order"
    ts = str(int(time.time() * 1000)) - 5000
    atr = calculate_atr(kdata, 14)
    if not atr or atr <= 0:
        atr = float(kdata[-1]['close']) * 0.01
    
    if side == "LONG":
        stop_p = round(level - (1.5 * atr), 3)
        stop_s = f"stopPrice={stop_p}&positionSide=LONG&quantity={qty}&side=SELL&symbol={symbol}&type=STOP_MARKET"
    else:
        stop_p = round(level + (1.5 * atr), 3)
        stop_s = f"stopPrice={stop_p}&positionSide=SHORT&quantity={qty}&side=BUY&symbol={symbol}&type=STOP_MARKET"
        
    p_str = stop_s
    sig = get_sign(API_SECRET, p_str)
    try:
        async with session.post(f"{BINGX_BASE_URL}{path}?{p_str}&signature={sig}", headers={"X-BX-APIKEY": API_KEY}) as r:
            if r.status == 200:
                res = await r.json()
                if res.get("code") == 0:
                    print(f"{symbol} Початковий стоп успішно встановлено: {stop_p}", flush=True)
                    return stop_p
                else:
                    print(f"Помилка стопу при відкритті {symbol}: {res}", flush=True)
            else:
                print(f"HTTP помилка стопу {symbol}: {r.status}", flush=True)
    except Exception as e:
        print(f"Виняток при встановленні стопу {symbol}: {e}", flush=True)
    return None
    
async def open_bot_position(session, symbol, side, price, level, kdata):
    if not API_KEY or not API_SECRET: return
    await set_leverage(session, symbol, LEVERAGE, side)
    path = "/openApi/swap/v2/trade/order"
    ts = str(int(time.time() * 1000)) - 5000
    qty = round(BOT_MARGIN_USDT * LEVERAGE / price, 4)
    if qty == 0: return
    p_side = "LONG" if side == "LONG" else "SHORT"
    c_side = "BUY" if side == "LONG" else "SELL"
    p_str = f"positionSide={p_side}&quantity={qty}&side={c_side}&symbol={symbol}&timestamp={ts}&type=MARKET"
    sig = get_sign(API_SECRET, p_str)
    try:
        async with session.post(f"{BINGX_BASE_URL}{path}?{p_str}&signature={sig}", headers={"X-BX-APIKEY": API_KEY}, timeout=5) as r:
            res = await r.json()
            if res.get("code") == 0:
                position_open_time[symbol] = time.time()
                msg = f"🟢 ВІДКРИТЬ УГОДУ ({side})\n• Монета: {symbol}\n• Ціна: {price}"
                print(msg, flush=True)
                await send_to_telegram(session, msg)
                await asyncio.sleep(1)
                await set_initial_stop_loss(session, symbol, side, level, kdata, qty)
            else:
                print(f"ПОМИЛКА БІРЖІ (код {res.get('code')}): {res.get('msg')}", flush=True)
    except Exception as e:
        print(f"Помилка запиту open_bot_position: {e}", flush=True)

async def set_break_even(session, symbol, side, entry, qty):
    if not API_KEY or not API_SECRET: return False
    await cancel_existing_stop_orders(session, symbol)
    path = "/openApi/swap/v2/trade/order"
    ts = str(int(time.time() * 1000)) - 5000
    p_side = "LONG" if side == "LONG" else "SHORT"
    c_side = "SELL" if side == "LONG" else "BUY"
    p_str = f"positionSide={p_side}&price={entry}&quantity={qty}&side={c_side}&stopPrice={entry}&symbol={symbol}&timestamp={ts}&type=STOP"
    sig = get_sign(API_SECRET, p_str)
    try:
        async with session.post(f"{BINGX_BASE_URL}{path}?{p_str}&signature={sig}", headers={"X-BX-APIKEY": API_KEY}, timeout=5) as r:
            if r.status == 200:
                res = await r.json()
                if res.get("code") == 0:
                    print(f"{symbol} Стоп успішно перенесено в Беззбиток", flush=True)
                    return True
                else:
                    print(f"Помилка БУ для {symbol}: {res}", flush=True)
            else:
                print(f"HTTP помилка БУ {symbol}: {r.status}", flush=True)
    except Exception as e:
        print(f"Виняток при встановленні БУ {symbol}: {e}", flush=True)
    return False


async def fetch_top_symbols(session):
    try:
        async with session.get(f"{BINGX_BASE_URL}/openApi/swap/v2/quote/ticker") as r:
            if r.status == 200:
                data = await r.json()
                d = data.get("data")
                if isinstance(d, list) and len(d) > 0:
                    print(f"FIRST COIN: {d[0]}", flush=True)
                    res = []
                    for t in d:
                        if not isinstance(t, dict): continue
                        sym = t.get("symbol")
                        if not sym: continue
                        try:
                            vol_val = t.get("quoteVolume") or t.get("volume") or 0
                            val = float(str(vol_val).replace(',', '.'))
                            if "USD-USD" in sym or "2USDT-USDT" in sym:
                                continue
                            if val >= 500 and "USDT" in sym:
                                res.append((sym, val))
                        except Exception as e:
                            pass
                    res.sort(key=lambda x: x[1], reverse=True)
                    top_symbols = [x[0] for x in res[:TOP_COINS_LIMIT]]
                    print(f"Успішно відібрано монет за об'ємом: {len(top_symbols)}")
                    return top_symbols
                else:
                    print("Помилка: список порожній", flush=True)
            else:
                print(f"Помилка HTTP: {r.status}", flush=True)
    except Exception as e:
        print(f"Виняток у fetch_top_symbols: {e}", flush=True)
    return []

async def fetch_kline(session, symbol):
    try:
        async with session.get(f"{BINGX_BASE_URL}/openApi/swap/v2/quote/klines?symbol={symbol}&interval={TIMEFRAME}&limit={LIMIT_CANDLES}", timeout=5) as r:
            if r.status == 200:
                raw = await r.json()
                if raw.get("code") == 0:
                    data = raw.get("data", [])
                    data.sort(key=lambda x: int(x.get("time", x.get("openTime", 0))))
                    return [{"time": int(x.get("time", x.get("openTime"))), "open": float(x["open"]), "high": float(x["high"]), "low": float(x["low"]), "close": float(x["close"]), "volume": float(x["volume"])} for x in data]
    except: pass
    return None

async def scan_coin(session, symbol, open_count, open_symbols):
    if symbol in open_symbols:
        return
    if open_count >= 2: return
    now = time.time()
    if symbol in last_alert_time and now - last_alert_time[symbol] < COOLDOWN_SECONDS: return
    kdata = await fetch_kline(session, symbol)
    if not kdata or len(kdata) < 60: return

    closes = [x['close'] for x in kdata]
    opens = [x['open'] for x in kdata]
    vols = [x['volume'] for x in kdata]
    lows = [x['low'] for x in kdata]
    highs = [x['high'] for x in kdata]
        
    cur_vol = vols[-1]
    cur_price = closes[-1]
    cur_open = opens[-1]
    cur_high = highs[-1]
    cur_low = lows[-1]
        
    avg_vol = sum(vols[:-1]) / (len(vols) - 1)
    has_volume_spike = (cur_vol > avg_vol * VOLUME_MULTIPLIER) and (cur_vol > 0)
    has_momentum_volume_spike = (cur_vol > avg_vol * MOMENTUM_VOLUME_MULTIPLIER) and (cur_vol > 0)
        
    ema = calculate_ema(closes, 50)
    sup, res = lows[-1], highs[-1]
    
        # Надійний виклик KD у сканері
    try:
        kd_res = calculate_kd(kdata)
    except Exception:
        kd_res = None

    if isinstance(kd_res, (list, tuple)) and len(kd_res) >= 3:
        k_vals, d_vals, j_vals = kd_res
        current_k = float(k_vals[-1]) if k_vals else 0.0
        current_d = float(d_vals[-1]) if d_vals else 0.0
        prev_k = float(k_vals[-2]) if len(k_vals) >= 2 else 0.0
        prev_d = float(d_vals[-2]) if len(d_vals) >= 2 else 0.0
        current_j = 3 * current_k - 2 * current_d
    else:
        current_k, current_d, prev_k, prev_d = 0.0, 0.0, 0.0, 0.0


    near_support = (sup != 0) and ((cur_price - sup) / sup <= APPROACH_PERCENT)
    near_resistance = (res != 0) and ((res - cur_price) / res <= APPROACH_PERCENT)

        
    near_support = (sup > 0) and ((cur_price - sup) / sup <= APPROACH_PERCENT)
    near_resistance = (res > 0) and ((res - cur_price) / res <= APPROACH_PERCENT)
        
    candle_body = abs(cur_price - cur_open)
    candle_range = cur_high - cur_low
    is_solid_candle = candle_range > 0 and (candle_body / candle_range >= 0.4)
        
    # Суворі умови з урахуванням EMA за закриттям (тілом свічки)
    
    momentum_long = (
        has_momentum_volume_spike and
        is_solid_candle and
        cur_price >= ema and
        current_k > current_d and prev_k <= prev_d and
        current_k < 20
    )

    momentum_short = (
        has_momentum_volume_spike and
        is_solid_candle and
        cur_price < ema and
        current_k < current_d and
        current_j > 80
    )

    if near_support and has_volume_spike and is_solid_candle and cur_price >= ema and (current_k >= current_d and prev_k <= prev_d) and current_k < 85:
        last_alert_time[symbol] = now
        level = sup
        print(f"Знайдено сигнал LONG (Підтримка + KDJ) для {symbol}", flush=True)
        await open_bot_position(session, symbol, "LONG", cur_price, level, kdata)
        return

    elif near_resistance and has_volume_spike and is_solid_candle and cur_price <= ema and (current_k <= current_d and prev_k >= prev_d) and current_j > 20:
        last_alert_time[symbol] = now
        level = res
        print(f"Знайдено сигнал SHORT (Опір + KDJ) для {symbol}", flush=True)
        await open_bot_position(session, symbol, "SHORT", cur_price, level, kdata)
        return

    elif momentum_long:
        last_alert_time[symbol] = now
        level = ema
        print(f"Знайдено сигнал LONG (Імпульсний пробій EMA 50) для {symbol}", flush=True)
        await open_bot_position(session, symbol, "LONG", cur_price, level, kdata)
        return

    elif momentum_short:
        last_alert_time[symbol] = now
        level = ema
        print(f"Знайдено сигнал SHORT (Імпульсний пробій EMA 50) для {symbol}", flush=True)
        await open_bot_position(session, symbol, "SHORT", cur_price, level, kdata)
        return

        
async def close_partial(session, symbol, side, qty):
    if not API_KEY or not API_SECRET: return False
    path = "/openApi/swap/v2/trade/order"
    ts = str(int(time.time() * 1000))
    c_side = "SELL" if side == "LONG" else "BUY"
    p_side = "LONG" if side == "LONG" else "SHORT"
    p_str = f"positionSide={p_side}&quantity={qty}&side={c_side}&symbol={symbol}&timestamp={ts}&type=MARKET"
    sig = get_sign(API_SECRET, p_str)
    try:
        async with session.post(f"{BINGX_BASE_URL}{path}?{p_str}&signature={sig}", headers={"X-BX-APIKEY": API_KEY}, timeout=5) as r:
            if r.status == 200:
                res = await r.json()
                if res.get("code") == 0:
                    return True
                else:
                    print(f"Помилка закриття {symbol}: {res}", flush=True)
            else:
                print(f"HTTP помилка закриття {symbol}: {r.status}", flush=True)
    except Exception as e:
        print(f"Виняток при закритті {symbol}: {e}", flush=True)
    return False

async def monitor_pos(session, pos):
    global last_position_alert_time, handled_partial_positions, partial_exit_prices
    global position_open_time, support_touches_count, resistance_touches_count, last_touch_candle_time
    
    sym, amt = pos.get("symbol"), float(pos.get("positionAmt", 0))
    entry, pnl = float(pos.get("avgPrice", 0)), pos.get("unrealizedProfit", 0)
    
    p_side = pos.get("positionSide")
    side = "LONG" if p_side == "LONG" else "SHORT"
    abs_amt = abs(amt)
    
    kdata = await fetch_kline(session, sym)
    if not kdata or len(kdata) < 15: return
    
    kd_res = calculate_kd(kdata)
    if not isinstance(kd_res, (list, tuple)) or len(kd_res) < 3:
        return
    k_v, d_v, j_v = kd_res
    if not j_v or len(j_v) < 2:
        return

    cur_j, prev_j, cur_k, prev_k = j_v[-1], j_v[-2], k_v[-1], k_v[-2]
    cur_c = float(kdata[-1]['close'])
    prev_c = float(kdata[-2]['close']) if len(kdata) >= 2 else cur_c
    cur_low, cur_high = float(kdata[-1]['low']), float(kdata[-1]['high'])

    current_candle_time = kdata[-1]['time']
    
    atr = calculate_atr(kdata, 14)
    current_time = time.time()
    last_pos_time = last_position_alert_time.get(sym, 0)
    open_t = position_open_time.get(sym, current_time)
    candles_passed_in_pos = (current_time - open_t) / 900
    
    lows = [float(x['low']) for x in kdata]
    highs = [float(x['high']) for x in kdata]

    support_level = min(lows) if lows else entry
    resistance_level = max(highs) if highs else entry
    
    if sym not in support_touches_count: support_touches_count[sym] = 0
    if sym not in resistance_touches_count: resistance_touches_count[sym] = 0
    if sym not in last_touch_candle_time: last_touch_candle_time[sym] = 0
    
    candles_passed = len(kdata) - 1 - next((i for i, x in enumerate(kdata[:-1]) if x['time'] == last_touch_candle_time[sym]), 0)
    
    if side == "SHORT" and support_level > 0 and 0 <= (cur_low - support_level) / support_level <= APPROACH_PERCENT:
        if last_touch_candle_time[sym] == 0 or candles_passed >= 3:
            support_touches_count[sym] += 1
            last_touch_candle_time[sym] = current_candle_time
    elif side == "LONG" and resistance_level > 0 and 0 <= (resistance_level - cur_high) / resistance_level <= APPROACH_PERCENT:
        if last_touch_candle_time[sym] == 0 or candles_passed >= 3:
            resistance_touches_count[sym] += 1
            last_touch_candle_time[sym] = current_candle_time
            
    stop_trigger_long = support_level - (1.5 * atr)
    stop_trigger_short = resistance_level + (1.5 * atr)

    full_close = False
    Tp = False

    if side == "LONG":
        if cur_c <= stop_trigger_long:
            full_close = True
        elif sym in handled_partial_positions:
            if cur_c >= entry + (4 * atr):
                full_close = True
        else:
            if cur_c >= entry + (2 * atr):
                Tp = True

    elif side == "SHORT":
        if cur_c >= stop_trigger_short:
            full_close = True
        elif sym in handled_partial_positions:
            if cur_c <= entry - (4 * atr):
                full_close = True
        else:
            if cur_c <= entry - (2 * atr):
                Tp = True

    if Tp and sym not in handled_partial_positions:
        part_q = round(abs_amt * 0.75, 4)
        if part_q > 0:
            if await close_partial(session, sym, side, part_q):
                await set_break_even(session, sym, side, entry, round(abs_amt - part_q, 4))
                handled_partial_positions.add(sym)
                partial_exit_prices[sym] = cur_c
                msg = f"🟡 ЧАСТКОВИЙ ТЕЙК 75% (+1%)\n• Монета: {sym} ({side})\n• Ціна: {cur_c}\n• PnL: {pnl} USDT"
                print(msg, flush=True)
                await send_to_telegram(session, msg)
            else:
                err_msg = f"⚠️ ПОМИЛКА: Не вдалося виконати частковий тейк для {sym} ({side})"
                print(err_msg, flush=True)
                await send_to_telegram(session, err_msg)
    elif full_close:
        if await close_partial(session, sym, side, abs_amt):
            handled_partial_positions.discard(sym)
            partial_exit_prices.pop(sym, None)
            position_open_time.pop(sym, None)
            support_touches_count.pop(sym, None)
            resistance_touches_count.pop(sym, None)
            last_touch_candle_time.pop(sym, None)
            last_alert_time.pop(sym, None)
            msg = f"🔴 ПОВНЕ ЗАКРИТТЯ\n• Монета: {sym} ({side})\n• Ціна: {cur_c}\n• PnL: {pnl} USDT"
            print(msg, flush=True)
            await send_to_telegram(session, msg)
        else:
            err_msg = f"⚠️ ПОМИЛКА: Не вдалося виконати повне закриття для {sym} ({side})"
            print(err_msg, flush=True)
            await send_to_telegram(session, err_msg)
            
    elif current_time - last_pos_time >= 900:
        msg = f"ℹ️ Супровід позиції {sym} ({side})\n• Вхід: {entry}\n• Ціна: {cur_c}\n• PnL: {pnl} USDT"
        print(f"Супровід активної позиції {sym} відправлено в Телеграм", flush=True)
        await send_to_telegram(session, msg)
        last_position_alert_time[sym] = current_time
        
async def self_ping():
    while True:
        await asyncio.sleep(60)
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Compatible; RenderPingBot/1.0)"}
            async with aiohttp.ClientSession() as s:
                async with s.get(RENDER_URL, headers=headers, timeout=5) as r:
                    await r.text()
        except:
            pass

async def main():
    print("Бот запущено успішно!", flush=True)
    async with aiohttp.ClientSession() as session:
        await send_to_telegram(session, "🟢 Скрипт успішно оновлено та запущено!")

        asyncio.create_task(self_ping())
        previous_open_syms = set()
        last_hourly_report = time.time()

        while True:
            try:
                start = asyncio.get_event_loop().time()
                print("🔄 Початок нового циклу сканування ринку...", flush=True)

                if time.time() - last_hourly_report >= 3600:
                    if not positions:
                        report_msg = "ℹ️ Статус бота: Відкритих позицій немає, нових сигналів за останню годину не знайдено."
                        print(report_msg, flush=True)
                        await send_to_telegram(session, report_msg)
                    last_hourly_report = time.time()

                positions = await fetch_open_positions(session)
                print(f"📊 Знайдено відкритих позицій на біржі: {len(positions)}", flush=True)

                current_open_syms = (
                    {p.get("symbol") for p in positions}
                    if isinstance(positions, list)
                    else set()
                )

                closed_by_exchange = previous_open_syms - current_open_syms
                for sym in closed_by_exchange:
                    msg = f"❌ УГОДУ ЗАКРИТО (СТОП БІРЖІ)\n• Монета: {sym}"
                    print(msg, flush=True)
                    await send_to_telegram(session, msg)
                    
                    handled_partial_positions.discard(sym)
                    partial_exit_prices.pop(sym, None)
                    position_open_time.pop(sym, None)
                    support_touches_count.pop(sym, None)
                    resistance_touches_count.pop(sym, None)
                    last_touch_candle_time.pop(sym, None)
                    last_alert_time.pop(sym, None)

                previous_open_syms = current_open_syms

                if not positions:
                    handled_partial_positions.clear()
                    partial_exit_prices.clear()
                    position_open_time.clear()
                    support_touches_count.clear()
                    resistance_touches_count.clear()
                    last_touch_candle_time.clear()
                    last_alert_time.clear()
                else:
                    for p in positions:
                        await monitor_pos(session, p)

                if len(positions) < 2:
                    syms = await fetch_top_symbols(session)
                    print(f"📈 Отримано тикерів для сканування: {len(syms) if syms else 0}", flush=True)
                    
                    if syms:
                        tasks = [
                            scan_coin(
                                session, symbol, len(positions), current_open_syms
                            )
                            for symbol in syms
                        ]
                        await asyncio.gather(*tasks)
                else:
                    print("⏸ Досягнуто ліміт відкритих позицій (>= 2), сканування монет пропущено.", flush=True)

                elapsed = asyncio.get_event_loop().time() - start
                print(f"💤 Цикл завершено за {elapsed:.2f} сек. Чекаємо на наступний...", flush=True)
                
                await asyncio.sleep(max(1, 300 - elapsed))
            except Exception as e:
                print(f"⚠️ Помилка у головному циклі: {e}", flush=True)
                await asyncio.sleep(10)
                    

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is running successfully!")
    def log_message(self, format, *args):
        pass

if __name__ == "__main__":
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), SimpleHandler).serve_forever(), daemon=True).start()
    asyncio.run(main())
