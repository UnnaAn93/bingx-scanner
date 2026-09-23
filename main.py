import asyncio
import aiohttp
import os
import time
import hmac
import hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

VOLUME_MULTIPLIER = 1.6           
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
partial_exit_prices = {}          # Зберігаємо ціну першого часткового виходу для кожної монети

def get_sign(secret, payload):
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), digestmod=hashlib.sha256).hexdigest()

async def send_to_discord(session, url, msg):
    if not url: return
    try:
        async with session.post(url, json={"content": msg}) as resp:
            await resp.text()
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
        stop_p = round(level - (1.5 * atr), 5)
        stop_s, p_side = "SELL", "LONG"
    else:
        stop_p = round(level + (1.5 * atr), 5)
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

async def open_bot_position(session, symbol, side, price, level, kdata, webhook):
    if not API_KEY or not API_SECRET: return
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
                    stop_p = await set_initial_stop_loss(session, symbol, side, level, kdata)
                    msg = f"🤖🚀 БОТ ВІДКРИВ УГОДУ [{side}]: `{symbol}` | Вхід: `{price}` | Стоп: `{stop_p}`"
                    print(msg, flush=True)
                    await send_to_discord(session, webhook, msg)
    except: pass

async def close_partial(session, symbol, side, qty, webhook):
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
    p_str = f"positionSide={p_side}&side={c_side}&stopPrice={entry}&symbol={symbol}&timestamp={ts}&type=STOP_MARKET"
    sig = get_sign(API_SECRET, p_str)
    try:
        async with session.post(f"{BINGX_BASE_URL}{path}?{p_str}&signature={sig}", headers={"X-BX-APIKEY": API_KEY}, timeout=5) as r:
            pass
    except: pass

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
                    top_list = [x[0] for x in res[:TOP_COINS_LIMIT]]
                    return top_list
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
                    return [{"close": float(x['close']), "high": float(x['high']), "low": float(x['low']), "volume": float(x['volume'])} for x in raw]
    except: pass
    return None

async def scan_coin(session, symbol, webhook, open_count):
    if open_count >= 2: return
    now = time.time()
    if symbol in last_alert_time and now - last_alert_time[symbol] < COOLDOWN_SECONDS: return
    
    kdata = await fetch_kline(session, symbol)
    if not kdata or len(kdata) < 60: return
    
    try:
        closes, vols, lows, highs = [x['close'] for x in kdata], [x['volume'] for x in kdata], [x['low'] for x in kdata], [x['high'] for x in kdata]
        cur_vol, cur_price = vols[-1], closes[-1]
        avg_vol = sum(vols[:-1]) / (len(vols) - 1)
        if cur_vol <= 0 or avg_vol <= 0 or cur_vol < avg_vol * VOLUME_MULTIPLIER: return
        
        ema = calculate_ema(closes, 50)
        sup, res = min(lows[:-1]), max(highs[:-1])
        
        if sup > 0 and 0 <= (cur_price - sup) / sup <= APPROACH_PERCENT and cur_price > ema:
            last_alert_time[symbol] = now
            print(f"Знайдено сигнал LONG для {symbol} біля підтримки {sup}!", flush=True)
            await open_bot_position(session, symbol, "LONG", cur_price, sup, kdata, webhook)
        elif res > 0 and 0 <= (res - cur_price) / res <= APPROACH_PERCENT and cur_price < ema:
            last_alert_time[symbol] = now
            print(f"Знайдено сигнал SHORT для {symbol} біля опору {res}!", flush=True)
            await open_bot_position(session, symbol, "SHORT", cur_price, res, kdata, webhook)
    except: pass

async def monitor_pos(session, pos, webhook):
    global last_position_alert_time, last_opposite_alert_time, handled_partial_positions, partial_exit_prices
    sym, amt = pos.get("symbol"), float(pos.get("positionAmt", 0))
    entry, pnl = float(pos.get("avgPrice", 0)), pos.get("unrealizedProfit", "0")
    
    p_side = pos.get("positionSide", "LONG")
    side = "LONG" if p_side == "LONG" else "SHORT"
    abs_amt = abs(amt)
    
    kdata = await fetch_kline(session, sym)
    if not kdata or len(kdata) < 15: return
    
    k_v, d_v, j_v = calculate_kdj(kdata)
    if not j_v or len(j_v) < 2: return
    
    cur_j, prev_j, cur_k, prev_k = j_v[-1], j_v[-2], k_v[-1], k_v[-2]
    cur_c, cur_v = kdata[-1]['close'], kdata[-1]['volume']
    ema = calculate_ema([x['close'] for x in kdata], 50)
    
    current_time = time.time()
    last_pos_time = last_position_alert_time.get(sym, 0)
    
    tp, full_close = False, False
    
    if side == "LONG":
        if prev_j > 85 and cur_j < cur_k and cur_c >= entry * 1.01:
            if sym in handled_partial_positions:
                # Повне закриття залишку: якщо ціна пішла вище мінімум на 1% від ціни першого тейку АБО пробила EMA вниз
                prev_exit_p = partial_exit_prices.get(sym, entry)
                if cur_c >= prev_exit_p * 1.01 or cur_c < ema:
                    full_close = True
            else:
                tp = True
    elif side == "SHORT":
        if prev_j < 15 and cur_j > cur_k and cur_c <= entry * 0.99:
            if sym in handled_partial_positions:
                # Повне закриття залишку: якщо ціна нижче мінімум на 1% від ціни першого тейку АБО пробила EMA вгору
                prev_exit_p = partial_exit_prices.get(sym, entry)
                if cur_c <= prev_exit_p * 0.99 or cur_c > ema:
                    full_close = True
            else:
                tp = True
        
    if tp and sym not in handled_partial_positions:
        part_q = round(abs_amt * 0.75, 4)
        if part_q > 0 and await close_partial(session, sym, side, part_q, webhook):
            await set_break_even(session, sym, side, entry)
            handled_partial_positions.add(sym)
            partial_exit_prices[sym] = cur_c  # Зберігаємо ціну першого виходу
            msg = f"🎯 ЧАСТКОВИЙ ТЕЙК 75% `{sym}` ({side}) | Ціна: `{cur_c}` | PnL: `{pnl} USDT`"
            print(msg, flush=True)
            await send_to_discord(session, webhook, msg)
    elif full_close:
        if await close_partial(session, sym, side, abs_amt, webhook):
            handled_partial_positions.discard(sym)
            partial_exit_prices.pop(sym, None)
            msg = f"🏁 ПОВНЕ ЗАКРИТТЯ `{sym}` ({side}) | Ціна: `{cur_c}` | PnL: `{pnl} USDT`"
            print(msg, flush=True)
            await send_to_discord(session, webhook, msg)
    elif current_time - last_pos_time >= 900:
        msg = f"📊 Супровід позиції `{sym}` ({side}):\n• Вхід: `{entry}` | Ціна: `{cur_c}` | PnL: `{pnl} USDT`\n• KDJ -> J: `{cur_j:.1f}`, K: `{cur_k:.1f}`\n✅ Позиція в роботі."
        print(f"Супровід активної позиції {sym}", flush=True)
        await send_to_discord(session, webhook, msg)
        last_position_alert_time[sym] = current_time

async def self_ping():
    while True:
        await asyncio.sleep(60)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(RENDER_URL, timeout=5) as r:
                    await r.text()
        except: pass

async def main():
    print("Бот запущено успішно та сканує ринок (з покращеним виходом для залишку)!", flush=True)
    async with aiohttp.ClientSession() as session:
        asyncio.create_task(self_ping())
        while True:
            try:
                start = asyncio.get_event_loop().time()
                print("--- Початок нового циклу сканування ринку ---", flush=True)
                positions = await fetch_open_positions(session)
                open_syms = [p.get("symbol") for p in positions]
                if not positions: 
                    handled_partial_positions.clear()
                    partial_exit_prices.clear()
                
                for p in positions:
                    await monitor_pos(session, p, DISCORD_WEBHOOK_URL)
                
                if len(positions) < 2:
                    syms = await fetch_top_symbols(session)
                    print(f"Отримано топ монет для перевірки: {len(syms)}", flush=True)
                    if syms:
                        tasks = [scan_coin(session, s, DISCORD_WEBHOOK_URL, len(positions)) for s in syms if s not in open_syms]
                        await asyncio.gather(*tasks)
                        
                elapsed = asyncio.get_event_loop().time() - start
                print(f"Цикл завершено за {elapsed:.2f} сек. Очікування...", flush=True)
                await asyncio.sleep(max(1, 60 - elapsed))
            except Exception as e:
                print(f"Помилка циклу: {e}", flush=True)
                await asyncio.sleep(10)

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"Bot is active!")
    def log_message(self, format, *args): pass

if __name__ == "__main__":
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), SimpleHandler).serve_forever(), daemon=True).start()
    asyncio.run(main())
    
