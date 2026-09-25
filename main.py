import asyncio
import aiohttp
import os
import time
import hmac
import hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

VOLUME_MULTIPLIER = 2.2                 
MOMENTUM_VOLUME_MULTIPLIER = 3.0        
APPROACH_PERCENT = 0.008          
TIMEFRAME = "15m"                 
LIMIT_CANDLES = 100               
TOP_COINS_LIMIT = 150             
MIN_24H_VOLUME_USDT = 5_000_000   
COOLDOWN_SECONDS = 900            
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
position_extremes = {}

def get_sign(secret, payload):
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), digestmod=hashlib.sha256).hexdigest()

async def send_to_telegram(session, msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        async with session.post(url, json=payload, timeout=5) as resp:
            pass
    except: pass

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

def calculate_kdj(kdata, n=9, m1=3, m2=3):
    if len(kdata) < n: return None, None, None
    k_l, d_l, j_l = [], [], []
    k, d = 50.0, 50.0
    for i in range(len(kdata)):
        if i < n - 1:
            k_l.append(50.0); d_l.append(50.0); j_l.append(50.0)
            continue
        win = kdata[i - n + 1 : i + 1]
        hh, ll = max(x['high'] for x in win), min(x['low'] for x in win)
        c = kdata[i]['close']
        rsv = 50.0 if hh == ll else (c - ll) / (hh - ll) * 100
        k = ((m1 - 1) * k + rsv) / m1
        d = ((m2 - 1) * d + k) / m2
        j = 3 * k - 2 * d
        k_l.append(k); d_l.append(d); j_l.append(j)
    return k_l, d_l, j_l

async def fetch_open_positions(session):
    if not API_KEY or not API_SECRET: return []
    path = "/openApi/swap/v2/user/positions"
    ts = str(int(time.time() * 1000))
    sig = get_sign(API_SECRET, f"timestamp={ts}")
    url = f"{BINGX_BASE_URL}{path}?timestamp={ts}&signature={sig}"
    try:
        async with session.get(url, headers={"X-BX-APIKEY": API_KEY}, timeout=5) as r:
            if r.status == 200:
                data = await r.json()
                if data.get("code") == 0:
                    return [p for p in data.get("data", []) if float(p.get("positionAmt", 0)) != 0]
    except: pass
    return []

async def set_leverage(session, symbol, lev, side):
    path = "/openApi/swap/v2/trade/leverage"
    ts = str(int(time.time() * 1000))
    p_side = "LONG" if side == "LONG" else "SHORT"
    p_str = f"leverage={lev}&positionSide={p_side}&symbol={symbol}&timestamp={ts}"
    sig = get_sign(API_SECRET, p_str)
    try:
        async with session.post(f"{BINGX_BASE_URL}{path}?{p_str}&signature={sig}", headers={"X-BX-APIKEY": API_KEY}, timeout=5) as r:
            pass
    except: pass

async def set_initial_stop_loss(session, symbol, side, level, kdata):
    path = "/openApi/swap/v2/trade/stopOrder"
    ts = str(int(time.time() * 1000))
    atr = calculate_atr(kdata, 14)
    if side == "LONG":
        stop_p = round(min(level, kdata[-1]['low']) - (1.2 * atr), 5)
        stop_s, p_side = "SELL", "LONG"
    else:
        stop_p = round(max(level, kdata[-1]['high']) + (1.2 * atr), 5)
        stop_s, p_side = "BUY", "SHORT"
    
    p_str = f"positionSide={p_side}&side={stop_s}&stopPrice={stop_p}&symbol={symbol}&timestamp={ts}&type=STOP_MARKET"
    sig = get_sign(API_SECRET, p_str)
    try:
        async with session.post(f"{BINGX_BASE_URL}{path}?{p_str}&signature={sig}", headers={"X-BX-APIKEY": API_KEY}, timeout=5) as r:
            if r.status == 200:
                res = await r.json()
                if res.get("code") == 0: return stop_p
    except: pass
    return None

async def update_trailing_stop(session, symbol, side, new_stop_price):
    path = "/openApi/swap/v2/trade/stopOrder"
    ts = str(int(time.time() * 1000))
    c_side = "SELL" if side == "LONG" else "BUY"
    p_side = "LONG" if side == "LONG" else "SHORT"
    p_str = f"positionSide={p_side}&price=0&side={c_side}&stopPrice={round(new_stop_price, 5)}&symbol={symbol}&timestamp={ts}&type=STOP_MARKET"
    sig = get_sign(API_SECRET, p_str)
    try:
        async with session.post(f"{BINGX_BASE_URL}{path}?{p_str}&signature={sig}", headers={"X-BX-APIKEY": API_KEY}, timeout=5) as r:
            if r.status == 200:
                res = await r.json()
                return res.get("code") == 0
    except: pass
    return False

async def open_bot_position(session, symbol, side, price, level, kdata):
    if not API_KEY or not API_SECRET: return
    current_positions = await fetch_open_positions(session)
    if len(current_positions) >= 2: return

    await set_leverage(session, symbol, LEVERAGE, side)
    path = "/openApi/swap/v2/trade/order"
    ts = str(int(time.time() * 1000))
    qty = round((BOT_MARGIN_USDT * LEVERAGE) / price, 4)
    if qty <= 0: return
    
    o_side = "BUY" if side == "LONG" else "SELL"
    p_side = "LONG" if side == "LONG" else "SHORT"
    p_str = f"positionSide={p_side}&quantity={qty}&side={o_side}&symbol={symbol}&timestamp={ts}&type=MARKET"
    sig = get_sign(API_SECRET, p_str)
    try:
        async with session.post(f"{BINGX_BASE_URL}{path}?{p_str}&signature={sig}", headers={"X-BX-APIKEY": API_KEY}, timeout=5) as r:
            if r.status == 200:
                res = await r.json()
                if res.get("code") == 0:
                    msg = f"🤖🚀 БОТ ВІДКРИВ УГОДУ *[{side}]*: `{symbol}` | Вхід: `{price}`"
                    print(msg, flush=True)
                    await send_to_telegram(session, msg)
                    await set_initial_stop_loss(session, symbol, side, level, kdata)
    except: pass

async def close_partial(session, symbol, side, qty):
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
                return res.get("code") == 0
    except: pass
    return False

async def set_break_even(session, symbol, side, entry):
    path = "/openApi/swap/v2/trade/stopOrder"
    ts = str(int(time.time() * 1000))
    c_side = "SELL" if side == "LONG" else "BUY"
    p_side = "LONG" if side == "LONG" else "SHORT"
    p_str = f"positionSide={p_side}&price=0&side={c_side}&stopPrice={entry}&symbol={symbol}&timestamp={ts}&type=STOP_MARKET"
    sig = get_sign(API_SECRET, p_str)
    try:
        async with session.post(f"{BINGX_BASE_URL}{path}?{p_str}&signature={sig}", headers={"X-BX-APIKEY": API_KEY}, timeout=5) as r:
            if r.status == 200:
                res = await r.json()
                if res.get("code") == 0: return True
    except: pass
    return False

async def fetch_top_symbols(session):
    try:
        async with session.get(f"{BINGX_BASE_URL}/openApi/swap/v2/quote/ticker", timeout=5) as r:
            if r.status == 200:
                data = await r.json()
                if data.get("code") == 0:
                    res = []
                    for t in data.get("data", []):
                        sym = t.get("symbol", "")
                        if sym.startswith("NC") or "2USD" in sym or not sym.endswith("-USDT"): continue
                        try:
                            vol = float(t.get("volume", 0)) * float(t.get("lastPrice", 0))
                            if vol >= MIN_24H_VOLUME_USDT: res.append((sym, vol))
                        except: pass
                    res.sort(key=lambda x: x[1], reverse=True)
                    return [x[0] for x in res[:TOP_COINS_LIMIT]]
    except: pass
    return []

async def fetch_kline(session, symbol):
    try:
        async with session.get(f"{BINGX_BASE_URL}/openApi/swap/v2/quote/klines", params={"symbol": symbol, "interval": TIMEFRAME, "limit": str(LIMIT_CANDLES)}, timeout=4) as r:
            if r.status == 200:
                data = await r.json()
                if data.get("code") == 0:
                    raw = data.get("data", [])
                    raw.sort(key=lambda x: int(x.get("time", x.get("openTime", 0))))
                    return [{"time": int(x.get("time", x.get("openTime", 0))), "open": float(x['open']), "close": float(x['close']), "high": float(x['high']), "low": float(x['low']), "volume": float(x['volume'])} for x in raw]
    except: pass
    return None

async def scan_coin(session, symbol, open_count):
    if open_count >= 2: return
    now = time.time()
    if symbol in last_alert_time and now - last_alert_time[symbol] < COOLDOWN_SECONDS: return
    
    kdata = await fetch_kline(session, symbol)
    if not kdata or len(kdata) < 60: return
    
    try:
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
        
        has_volume_spike = (cur_vol > 0 and avg_vol > 0 and cur_vol >= avg_vol * VOLUME_MULTIPLIER)
        has_momentum_volume_spike = (cur_vol > 0 and avg_vol > 0 and cur_vol >= avg_vol * MOMENTUM_VOLUME_MULTIPLIER)
        
        ema = calculate_ema(closes, 50)
        sup, res = min(lows[:-1]), max(highs[:-1])
        
        near_support = (sup > 0 and 0 <= (cur_price - sup) / sup <= APPROACH_PERCENT)
        near_resistance = (res > 0 and 0 <= (res - cur_price) / res <= APPROACH_PERCENT)
        
        candle_body = abs(cur_price - cur_open)
        candle_range = cur_high - cur_low
        is_solid_candle = candle_range > 0 and (candle_body / candle_range) >= 0.40
        
        momentum_long = (
            has_momentum_volume_spike and is_solid_candle and
            cur_price > ema and closes[-2] <= ema and closes[-3] <= ema
        )
        momentum_short = (
            has_momentum_volume_spike and is_solid_candle and
            cur_price < ema and closes[-2] >= ema and closes[-3] >= ema
        )
        
        if near_support and has_volume_spike and is_solid_candle:
            last_alert_time[symbol] = now
            await open_bot_position(session, symbol, "LONG", cur_price, sup, kdata)
            return
        elif near_resistance and has_volume_spike and is_solid_candle:
            last_alert_time[symbol] = now
            await open_bot_position(session, symbol, "SHORT", cur_price, res, kdata)
            return
        elif momentum_long:
            last_alert_time[symbol] = now
            await open_bot_position(session, symbol, "LONG", cur_price, ema, kdata)
            return
        elif momentum_short:
            last_alert_time[symbol] = now
            await open_bot_position(session, symbol, "SHORT", cur_price, ema, kdata)
            return
    except: pass

async def monitor_pos(session, pos):
    global last_position_alert_time, handled_partial_positions, partial_exit_prices, position_extremes
    
    sym, amt = pos.get("symbol"), float(pos.get("positionAmt", 0))
    entry, pnl = float(pos.get("avgPrice", 0)), pos.get("unrealizedProfit", "0")
    
    p_side = pos.get("positionSide", "LONG")
    side = "LONG" if p_side == "LONG" else "SHORT"
    abs_amt = abs(amt)
    
    kdata = await fetch_kline(session, sym)
    if not kdata or len(kdata) < 15: return
    
    cur_c = kdata[-1]['close']
    cur_high = kdata[-1]['high']
    cur_low = kdata[-1]['low']
    cur_vol = kdata[-1]['volume']
    vols = [x['volume'] for x in kdata[:-1]]
    avg_vol = sum(vols) / len(vols) if vols else cur_vol
    
    ema = calculate_ema([x['close'] for x in kdata], 50)
    atr = calculate_atr(kdata, 14)
    
    lows = [x['low'] for x in kdata[:-1]]
    highs = [x['high'] for x in kdata[:-1]]
    support_level = min(lows) if lows else entry
    resistance_level = max(highs) if highs else entry

    if sym not in position_extremes:
        position_extremes[sym] = cur_high if side == "LONG" else cur_low

    tp_75 = False
    full_close = False
    current_time = time.time()
    last_pos_time = last_position_alert_time.get(sym, 0)

    if side == "LONG":
        if cur_high > position_extremes[sym]:
            position_extremes[sym] = cur_high
            if position_extremes[sym] > entry + (1 * atr):
                new_sl = position_extremes[sym] - (1.5 * atr)
                if new_sl > entry:
                    await update_trailing_stop(session, sym, side, new_sl)

        is_volume_breakout = (cur_vol >= avg_vol * 2.0) and (cur_c > resistance_level)
        near_res = (resistance_level > entry) and (cur_high >= resistance_level * 0.997)
        price_take = cur_c >= entry * 1.015

        if (near_res or price_take) and not is_volume_breakout:
            tp_75 = True

        ema_break = (cur_c < ema * 0.995) and (kdata[-2]['close'] < ema) and (cur_c < entry)
        if ema_break:
            full_close = True

    elif side == "SHORT":
        if cur_low < position_extremes[sym]:
            position_extremes[sym] = cur_low
            if position_extremes[sym] < entry - (1 * atr):
                new_sl = position_extremes[sym] + (1.5 * atr)
                if new_sl < entry:
                    await update_trailing_stop(session, sym, side, new_sl)

        is_volume_breakout = (cur_vol >= avg_vol * 2.0) and (cur_c < support_level)
        near_sup = (support_level < entry) and (cur_low <= support_level * 1.003)
        price_take = cur_c <= entry * 0.985

        if (near_sup or price_take) and not is_volume_breakout:
            tp_75 = True

        ema_break = (cur_c > ema * 1.005) and (kdata[-2]['close'] > ema) and (cur_c > entry)
        if ema_break:
            full_close = True

    if tp_75 and sym not in handled_partial_positions:
        part_q = round(abs_amt * 0.75, 4)
        if part_q > 0 and await close_partial(session, sym, side, part_q):
            await set_break_even(session, sym, side, entry)
            handled_partial_positions.add(sym)
            msg = f"🎯 ЧАСТКОВИЙ ТЕЙК 75% `{sym}` *({side})* біля рівня! | Ціна: `{cur_c}` | PnL: `{pnl} USDT`"
            await send_to_telegram(session, msg)
    elif full_close:
        if await close_partial(session, sym, side, abs_amt):
            handled_partial_positions.discard(sym)
            position_extremes.pop(sym, None)
            last_alert_time[sym] = current_time
            msg = f"🏁 ПОВНЕ ЗАКРИТТЯ `{sym}` *({side})* (Трейлінг/EMA) | Ціна: `{cur_c}` | PnL: `{pnl} USDT`"
            await send_to_telegram(session, msg)
    elif current_time - last_pos_time >= 900:
        k_v, d_v, j_v = calculate_kdj(kdata)
        cur_j = j_v[-1] if j_v else 0
        msg = f"📊 Супровід позиції `{sym}` *({side})*:\n• Вхід: `{entry}` | Ціна: `{cur_c}` | PnL: `{pnl} USDT`\n• J: `{cur_j:.1f}`\n✅ Позиція в роботі."
        await send_to_telegram(session, msg)
        last_position_alert_time[sym] = current_time

async def self_ping():
    while True:
        await asyncio.sleep(60)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(RENDER_URL, timeout=5) as r: await r.text()
        except: pass

async def main():
    async with aiohttp.ClientSession() as session:
        await send_to_telegram(session, "🔄 *Скрипт оновлено та помилки виправлено!*")
        asyncio.create_task(self_ping())
        while True:
            try:
                start = asyncio.get_event_loop().time()
                positions = await fetch_open_positions(session)
                open_syms = [p.get("symbol") for p in positions]
                if not positions: 
                    handled_partial_positions.clear()
                    partial_exit_prices.clear()
                    position_extremes.clear()
                
                for p in positions:
                    await monitor_pos(session, p)
                
                if len(positions) < 2:
                    syms = await fetch_top_symbols(session)
                    if syms:
                        tasks = [scan_coin(session, s, len(positions)) for s in syms if s not in open_syms]
                        await asyncio.gather(*tasks)
                        
                elapsed = asyncio.get_event_loop().time() - start
                await asyncio.sleep(max(1, 60 - elapsed))
            except: await asyncio.sleep(10)

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"Bot is active!")
    def log_message(self, format, *args): pass

if __name__ == "__main__":
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), SimpleHandler).serve_forever(), daemon=True).start()
    asyncio.run(main())
    
