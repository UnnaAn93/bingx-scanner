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

API_KEY = os.environ.get("BINGX_API_KEY", "")
API_SECRET = os.environ.get("BINGX_SECRET_KEY", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
RENDER_URL = os.environ.get("RENDER_URL", "https://bingx-scanner-djbf.onrender.com")

BINGX_BASE_URL = "https://open-api.bingx.com"

last_alert_time = {}
last_position_alert_time = 0      
last_opposite_alert_time = 0      
handled_partial_position = None   # Запам'ятовуємо позицію, по якій вже зробили частковий тейк

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

    k_list = []
    d_list = []
    j_list = []

    k = 50.0
    d = 50.0

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
                    print(f"Частково закрито 75% по {symbol} ({side})", flush=True)
                    return True
    except Exception as e:
        print(f"Помилка часткового закриття: {e}", flush=True)
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
                    print(f"Стоп перенесено в безубиток на {entry_price} для {symbol}", flush=True)
                    return True
    except Exception as e:
        print(f"Помилка встановлення безубитку: {e}", flush=True)
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

async def check_single_coin(session, symbol, discord_webhook_url):
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
        is_volume_spike = current_volume >= (avg_volume * VOLUME_MULTIPLIER)
        if not is_volume_spike:
            return

        surge_percent = int((current_volume / avg_volume - 1) * 100)

        support_level = min(lows[:-1])
        if support_level > 0 and 0 <= (current_price - support_level) / support_level <= APPROACH_PERCENT:
            alert_message = (
                f"🟢🎯 **ВІДКРИТИ ЛОНГ [Підтримка 15m]**:\n`{symbol}`\n"
                f"• Ціна: `{current_price}` (Підтримка: `{support_level}`)\n"
                f"• Об'єм свічки: `+{surge_percent}%` від середнього!"
            )
            last_alert_time[symbol] = current_time
            await send_to_discord(session, discord_webhook_url, alert_message)
            return

        resistance_level = max(highs[:-1])
        if resistance_level > 0 and 0 <= (resistance_level - current_price) / resistance_level <= APPROACH_PERCENT:
            alert_message = (
                f"🔴🎯 **ВІДКРИТИ ШОРТ [Опір 15m]**:\n`{symbol}`\n"
                f"• Ціна: `{current_price}` (Опір: `{resistance_level}`)\n"
                f"• Об'єм свічки: `+{surge_percent}%` від середнього!"
            )
            last_alert_time[symbol] = current_time
            await send_to_discord(session, discord_webhook_url, alert_message)
            return

    except Exception:
        pass

async def monitor_active_position(session, pos, discord_webhook_url):
    global last_position_alert_time, last_opposite_alert_time, handled_partial_position
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

    curr_j = j_vals[-1]
    prev_j = j_vals[-2]
    curr_k = k_vals[-1]
    prev_k = k_vals[-2]

    last_candle = kline_data[-1]
    current_volume = last_candle['volume']
    current_price = last_candle['close']
    open_price = last_candle['open']
    
    prev_avg_volume = sum(x['volume'] for x in kline_data[-11:-1]) / 10

    # 1. Перевірка умов для часткового тейку (75%) + безубиток
    is_kdj_take_profit = False
    if side == "LONG":
        # Лонг: фіксуємо зверху на перекупленості (J > 85) при зростанні ціни від входу на +1%
        if prev_j > 85 and (curr_j < curr_k and prev_j >= prev_k) and current_price >= entry_price * 1.01:
            is_kdj_take_profit = True
    elif side == "SHORT":
        # Шорт: фіксуємо знизу на перепроданості (J < 15) при падінні ціни від входу на +1% (нижче входу)
        if prev_j < 15 and (curr_j > curr_k and prev_j <= prev_k) and current_price <= entry_price * 0.99:
            is_kdj_take_profit = True

    # 2. Перевірка на протилежний великий об'єм
    is_opposite_volume = False
    if current_volume >= (prev_avg_volume * 1.5):
        if side == "LONG" and current_price < open_price:  
            is_opposite_volume = True
        elif side == "SHORT" and current_price > open_price: 
            is_opposite_volume = True

    current_time = time.time()

    # Виконання часткового тейку (75%) + безубиток
    if is_kdj_take_profit and handled_partial_position != sym:
        partial_qty = round(abs_amt * 0.75, 4)
        if partial_qty > 0:
            success_close = await close_partial_position_on_exchange(session, sym, side, partial_qty)
            if success_close:
                await set_break_even_stop(session, sym, side, entry_price)
                handled_partial_position = sym
                
                report_msg = (
                    f"🎯 **ЧАСТКОВИЙ ТЕЙК 75% [KDJ СИГНАЛ + ПЛЮС > 1%] `{sym}` ({side})**:\n"
                    f"• Вхід: `{entry_price}` | Поточна ціна: `{current_price}`\n"
                    f"• Закрито: `75%` об'єму | PnL: `{pnl} USDT`\n"
                    f"🛡️ **Стоп залишку перенесено в безубиток (`{entry_price}`)!**"
                )
                await send_to_discord(session, discord_webhook_url, report_msg)

    # Сповіщення про протилежні об'єми
    elif is_opposite_volume and (current_time - last_opposite_alert_time >= 180):
        report_msg = (
            f"📊 **Супровід позиції `{sym}` ({side})**:\n"
            f"• Вхід: `{entry_price}` | Поточна ціна: `{current_price}`\n"
            f"• PnL: `{pnl} USDT`\n"
            f"⚠️ **УВАГА: Зайшов великий об'єм у ПРОТИЛЕЖНОМУ напрямку!**"
        )
        await send_to_discord(session, discord_webhook_url, report_msg)
        last_opposite_alert_time = current_time

    # Звичайний звіт раз на 15 хвилин
    elif current_time - last_position_alert_time >= 900:
        report_msg = (
            f"📊 **Супровід позиції `{sym}` ({side})**:\n"
            f"• Вхід: `{entry_price}` | Поточна ціна: `{current_price}`\n"
            f"• PnL: `{pnl} USDT`\n"
            f"• KDJ Status -> J: `{curr_j:.1f}`, K: `{curr_k:.1f}`\n"
            f"✅ Позиція в роботі, очікуємо імпульс."
        )
        await send_to_discord(session, discord_webhook_url, report_msg)
        last_position_alert_time = current_time

async def self_ping_loop(session):
    while True:
        await asyncio.sleep(240)
        try:
            async with session.get(RENDER_URL, timeout=5) as response:
                pass
        except Exception:
            pass

async def main():
    global handled_partial_position
    print("Бот повного циклу (Фінальна логіка Long/Short KDJ + 1% профіт) запущено...")
    async with aiohttp.ClientSession() as session:
        asyncio.create_task(self_ping_loop(session))
        
        while True:
            start_time = asyncio.get_event_loop().time()
            
            open_positions = await fetch_open_positions(session)
            
            if open_positions:
                pos = open_positions[0]
                handled_partial_position = pos.get("symbol")
                await monitor_active_position(session, pos, DISCORD_WEBHOOK_URL)
            else:
                handled_partial_position = None
                print("Активних позицій немає. Скануємо ринок...")
                symbols_list = await fetch_top_bingx_symbols(session)
                if symbols_list:
                    tasks = [check_single_coin(session, symbol, DISCORD_WEBHOOK_URL) for symbol in symbols_list]
                    await asyncio.gather(*tasks)

            elapsed = asyncio.get_event_loop().time() - start_time
            sleep_time = max(1, 60 - elapsed)
            await asyncio.sleep(sleep_time)

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"BingX Smart Bot is running!")
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
    
