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
active_position = None  # {'symbol': ..., 'side': 'LONG'/'SHORT', 'entryPrice': ...}

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
                        
                        # ФІЛЬТР: Відсікаємо індекси NCFX та все, що не є стандартною криптою з -USDT
                        if "NCFX" in symbol or not symbol.endswith("-USDT"):
                            continue
                            
                        if symbol.endswith("-USDT") or symbol.endswith("USDT"):
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
                            "high": float(item.get("high", 0)),
                            "low": float(item.get("low", 0)),
                            "close": float(item.get("close", 0)),
                            "volume": float(item.get("volume", 0))
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

        # 1. Сигнал на ЛОНГ
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

        # 2. Сигнал на ШОРТ
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
    sym = pos.get("symbol")
    amt = float(pos.get("positionAmt", 0))
    entry_price = float(pos.get("avgPrice", 0))
    pnl = pos.get("unrealizedProfit", "0")
    side = "LONG" if amt > 0 else "SHORT"

    kline_data = await fetch_kline_data(session, sym)
    if not kline_data or len(kline_data) < 10:
        return

    current_volume = kline_data[-1]['volume']
    prev_avg_volume = sum(x['volume'] for x in kline_data[-11:-1]) / 10
    current_price = kline_data[-1]['close']

    is_volume_dropped = current_volume < (prev_avg_volume * 0.6)
    is_reversal = False
    if side == "LONG" and current_price < entry_price * 0.992:
        is_reversal = True
    elif side == "SHORT" and current_price > entry_price * 1.008:
        is_reversal = True

    report_msg = (
        f"📊 **Супровід позиції `{sym}` ({side})**:\n"
        f"• Вхід: `{entry_price}` | Поточна ціна: `{current_price}`\n"
        f"• PnL: `{pnl} USDT`"
    )

    if is_volume_dropped:
        report_msg += f"\n⚠️ **УВАГА: Об'єми впали! Рекомендується закрити угоду (згасання імпульсу).**"
    elif is_reversal:
        report_msg += f"\n🚨 **УВАГА: Зміна напрямку ринку! Рекомендується терміново закрити угоду.**"

    await send_to_discord(session, discord_webhook_url, report_msg)

async def self_ping_loop(session):
    while True:
        await asyncio.sleep(240)
        try:
            async with session.get(RENDER_URL, timeout=5) as response:
                pass
        except Exception:
            pass

async def main():
    print("Бот супроводу та аналізу сигналів BingX запущено...")
    async with aiohttp.ClientSession() as session:
        asyncio.create_task(self_ping_loop(session))
        
        while True:
            start_time = asyncio.get_event_loop().time()
            
            open_positions = await fetch_open_positions(session)
            
            if open_positions:
                pos = open_positions[0]
                await monitor_active_position(session, pos, DISCORD_WEBHOOK_URL)
            else:
                print("Активних позицій немає. Скануємо ринок...")
                symbols_list = await fetch_top_bingx_symbols(session)
                if symbols_list:
                    tasks = [check_single_coin(session, symbol, DISCORD_WEBHOOK_URL) for symbol in symbols_list]
                    await asyncio.gather(*tasks)

            elapsed = asyncio.get_event_loop().time() - start_time
            sleep_time = max(1, 30 - elapsed)
            await asyncio.sleep(sleep_time)

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"BingX Smart Position Bot is running!")
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
                    
