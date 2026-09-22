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
LIMIT_CANDLES = 40                
TOP_COINS_LIMIT = 150             
MIN_24H_VOLUME_USDT = 5_000_000   
COOLDOWN_SECONDS = 300            

# Налаштування для авто-торгівлі ботом
LEVERAGE = 10                     # 10x плече
BOT_MARGIN_USDT = 0.06            # Чиста маржа 0.06 USDT (загальна вартість позиції з плечем 10x буде 0.6 USDT)

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
    except Exception:
        pass
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
            pass
    except Exception:
        pass

async def open_bot_position(session, symbol, side, current_price, discord_webhook_url):
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
                    msg = (
                        f"🤖🚀 **БОТ ВІДКРИВ УГОДУ [{side}]**:\n`{symbol}`\n"
                        f"• Ціна входу: `{current_price}` | Маржа: `{BOT_MARGIN_USDT} USDT` (Плече: `{LEVERAGE}x`, Вартість: `{position_value_usdt} USDT`)\n"
                        f"• Кількість: `{quantity}`"
                    )
                    await send_to_discord(session, discord_webhook_url, msg)
    except Exception as e:
        print(f"Помилка відкриття позиції ботом: {e}", flush=True)

async def close_partial_position_on_exchange(session, symbol, side, quantity_to_close):
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
    except Exception:
        pass
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
    except Exception:
        pass
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
    except Exception:
        pass
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
    except Exception:
        pass
    return None

async def scan_and_trade_coin(session, symbol, discord_webhook_url, current_open_count):
    if current_open_count >= 2:
        return

    current_time = time.time()
    if symbol in last_alert_time and current_time - last_alert_time[symbol] < COOLDOWN_SECONDS:
        return

    kline_data = await fetch_kline_data(session, symbol)
    if not kline_data or len(kline_data) < 30:
        return

    try:
        volumes = [x['volume'] for x in kline_data]
        lows = [x['low'] for x in kline_data]
        highs = [x['high'] for x in kline_data]
        closes = [x['close'] for x in kline_data]
        
        current_volume = volumes[-1]
        if current_volume <= 0:
            return

        avg_volume = sum(volumes[:-1]) / (len(volumes) - 1)
        if avg_volume <= 0:
            return
        
        current_price = closes[-1]
        
        if current_volume < (avg_volume * VOLUME_MULTIPLIER):
            return

        support_level = min(lows[:-1])
        if support_level > 0 and 0 <= (current_price - support_level) / support_level <= APPROACH_PERCENT:
            last_alert_time[symbol] = current_time
            print(f"[SCANNER] Знайдено сигнал LONG для {symbol} за ціною {current_price}!", flush=True)
            await open_bot_position(session, symbol, "LONG", current_price, discord_webhook_url)
            return

        resistance_level = max(highs[:-1])
        if resistance_level > 0 and 0 <= (resistance_level - current_price) / resistance_level <= APPROACH_PERCENT:
            last_alert_time[symbol] = current_time
            print(f"[SCANNER] Знайдено сигнал SHORT для {symbol} за ціною {current_price}!", flush=True)
            await open_bot_position(session, symbol, "SHORT", current_price, discord_webhook_url)
            return

    except Exception as e:
        print(f"Помилка сканування {symbol}: {e}", flush=True)
        pass

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

    is_kdj_take_profit = False
    if side == "LONG":
        if prev_j > 85 and (curr_j < curr_k and prev_j >= prev_k) and current_price >= entry_price * 1.01:
            is_kdj_take_profit = True
    elif side == "SHORT":
        if prev_j < 15 and (curr_j > curr_k and prev_j <= prev_k) and current_price <= entry_price * 0.99:
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

    if is_kdj_take_profit and sym not in handled_partial_positions:
        partial_qty = round(abs_amt * 0.75, 4)
        if partial_qty > 0:
            success_close = await close_partial_position_on_exchange(session, sym, side, partial_qty)
            if success_close:
                await set_break_even_stop(session, sym, side, entry_price)
                handled_partial_positions.add(sym)
                
                report_msg = (
                    f"🎯 **ЧАСТКОВИЙ ТЕЙК 75% [KDJ + ПЛЮС > 1%] `{sym}` ({side})**:\n"
                    f"• Вхід: `{entry_price}` | Поточна ціна: `{current_price}`\n"
                    f"• Закрито: `75%` | PnL: `{pnl} USDT`\n"
                    f"🛡️ **Стоп перенесено в безубиток!**"
                )
                await send_to_discord(session, discord_webhook_url, report_msg)

    elif is_opposite_volume and (current_time - last_opp_time >= 180):
        report_msg = (
            f"📊 **Супровід позиції `{sym}` ({side})**:\n"
            f"• Вхід: `{entry_price}` | Поточна ціна: `{current_price}`\n"
            f"• PnL: `{pnl} USDT`\n"
            f"⚠️ **УВАГА: Великий об'єм у ПРОТИЛЕЖНОМУ напрямку!**"
        )
        await send_to_discord(session, discord_webhook_url, report_msg)
        last_opposite_alert_time[sym] = current_time

    elif current_time - last_pos_time >= 900:
        report_msg = (
            f"📊 **Супровід позиції `{sym}` ({side})**:\n"
            f"• Вхід: `{entry_price}` | Поточна ціна: `{current_price}`\n"
            f"• PnL: `{pnl} USDT`\n"
            f"• KDJ -> J: `{curr_j:.1f}`, K: `{curr_k:.1f}`\n"
            f"✅ Позиція в роботі."
        )
        await send_to_discord(session, discord_webhook_url, report_msg)
        last_position_alert_time[sym] = current_time

async def self_ping_loop(session):
    while True:
        await asyncio.sleep(240)
        try:
            async with session.get(RENDER_URL, timeout=5) as response:
                pass
        except Exception:
            pass

async def main():
    print("Бот мультипозиційного авто-ведення (0.06 маржа) запущено...", flush=True)
    async with aiohttp.ClientSession() as session:
        asyncio.create_task(self_ping_loop(session))
        
        while True:
            start_time = asyncio.get_event_loop().time()
            
            open_positions = await fetch_open_positions(session)
            open_symbols = [p.get("symbol") for p in open_positions]
            
            if not open_positions:
                handled_partial_positions.clear()

            if open_positions:
                for pos in open_positions:
                    await monitor_active_position(session, pos, DISCORD_WEBHOOK_URL)

            if len(open_positions) < 2:
                symbols_list = await fetch_top_bingx_symbols(session)
                if symbols_list:
                    filtered_symbols = [s for s in symbols_list if s not in open_symbols]
                    print(f"[SCANNER] Перевіряю топ монет, в роботі відкритих: {len(open_positions)}. Сканую активів: {len(filtered_symbols)}", flush=True)
                    tasks = [scan_and_trade_coin(session, symbol, DISCORD_WEBHOOK_URL, len(open_positions)) for symbol in filtered_symbols]
                    await asyncio.gather(*tasks)

            elapsed = asyncio.get_event_loop().time() - start_time
            sleep_time = max(1, 60 - elapsed)
            await asyncio.sleep(sleep_time)

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"BingX Bot (0.06 Margin) is running!")
    def log_message(self, format, *args):
        return

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот зупинений.")
    
