import asyncio
import aiohttp
import os
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# Налаштування параметрів для пошуку точок знизу (підтримка / відскок)
VOLUME_MULTIPLIER = 2.2           # Сплеск об'єму у 2.2 рази
APPROACH_PERCENT = 0.007          # 0.7% до рівня підтримки знизу
TIMEFRAME = "5m"                  # Таймфрейм 5 хвилин
LIMIT_CANDLES = 30                # Історія для пошуку локального мінімуму
TOP_COINS_LIMIT = 150             # Кількість найактивніших пар
MIN_24H_VOLUME_USDT = 5_000_000   # Мінімальний добовий об'єм у USDT
COOLDOWN_SECONDS = 300            # Кулдаун 5 хвилин на монету

DISCORD_WEBHOOK_URL = os.environ.get("BINGX_API_KEY")
RENDER_URL = "https://bingx-scanner-djbf.onrender.com"

last_alert_time = {}

async def fetch_top_bingx_symbols(session):
    url = "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"
    try:
        async with session.get(url, timeout=5) as response:
            if response.status == 200:
                data = await response.json()
                tickers = data.get("data", [])
                
                usdt_tickers = []
                for t in tickers:
                    symbol = t.get("symbol", "")
                    if symbol.endswith("-USDT") and len(symbol) <= 12 and "USD" not in symbol[:-5]:
                        quote_vol = float(t.get("quoteVolume", 0))
                        if quote_vol >= MIN_24H_VOLUME_USDT:
                            usdt_tickers.append((symbol, quote_vol))
                
                usdt_tickers.sort(key=lambda x: x[1], reverse=True)
                top_symbols = [item[0] for item in usdt_tickers[:TOP_COINS_LIMIT]]
                return top_symbols
    except Exception as e:
        print(f"Помилка при отриманні списку монет від BingX: {e}")
    return []

async def fetch_kline_data(session, symbol):
    url = "https://open-api.bingx.com/openApi/swap/v2/quote/klines"
    params = {
        "symbol": symbol,
        "interval": TIMEFRAME,
        "limit": LIMIT_CANDLES
    }
    try:
        async with session.get(url, params=params, timeout=4) as response:
            if response.status == 200:
                data = await response.json()
                return data.get("data", [])
    except Exception:
        pass
    return None

async def send_to_discord(session, webhook_url, message):
    if not webhook_url:
        return
    payload = {"content": message}
    try:
        async with session.post(webhook_url, json=payload) as response:
            if response.status != 200:
                print(f"Помилка відправки у Discord: {response.status}")
    except Exception as e:
        print(f"Виняток при відправці у Discord: {e}")

async def check_single_coin(session, symbol, discord_webhook_url):
    current_time = time.time()
    if symbol in last_alert_time and current_time - last_alert_time[symbol] < COOLDOWN_SECONDS:
        return

    kline_data = await fetch_kline_data(session, symbol)
    
    if not kline_data or len(kline_data) < 20:
        return

    try:
        volumes = [float(x['volume']) for x in kline_data]
        lows = [float(x['low']) for x in kline_data]
        closes = [float(x['close']) for x in kline_data]
        opens = [float(x['open']) for x in kline_data]
        
        current_volume = volumes[-1]
        if current_volume <= 0:
            return

        avg_volume = sum(volumes[:-1]) / (len(volumes) - 1)
        if avg_volume <= 0:
            return
        
        current_price = closes[-1]
        
        # Шукаємо локальний рівень підтримки знизу (мінімум по лоу попередніх свічок)
        support_level = min(lows[:-1])
        if support_level <= 0:
            return
            
        # Відстань від ціни до підтримки зверху вниз (скільки відсотків до дна)
        distance_to_support = (current_price - support_level) / support_level
            
        is_volume_spike = current_volume >= (avg_volume * VOLUME_MULTIPLIER)
        
        # Умова: ціна підходить дуже близько до підтримки (в межах 0.7% над рівнем дна)
        is_approaching_support = (0 <= distance_to_support <= APPROACH_PERCENT)

        if is_volume_spike and is_approaching_support:
            surge_percent = int((current_volume / avg_volume - 1) * 100)
            
            alert_message = (
                f"🟢🎯 **УВАГА [Кульмінація на дні / Підтримка 5m]**: `{symbol}`\n"
                f"• Напрямок: 🚀 **Підхід до локального дна / Збір ліквідності**\n"
                f"• Ціна: `{current_price}` (Підтримка: `{support_level}`)\n"
                f"• Об'єм свічки: `+{surge_percent}%` від середнього!\n"
                f"⏳ Готуйся до можливого відскоку вгору!"
            )
            
            last_alert_time[symbol] = current_time
            await send_to_discord(session, discord_webhook_url, alert_message)
            print(f"Сигнал дна 5m відправлено для {symbol}")

    except Exception as e:
        pass

async def self_ping_loop(session):
    while True:
        await asyncio.sleep(240)
        try:
            async with session.get(RENDER_URL, timeout=5) as response:
                pass
        except Exception:
            pass

async def main():
    print("Бот переорієнтований на пошук локального дна та підтримки (5m)...")
    
    async with aiohttp.ClientSession() as session:
        asyncio.create_task(self_ping_loop(session))
        
        while True:
            start_time = asyncio.get_event_loop().time()
            
            symbols_list = await fetch_top_bingx_symbols(session)
            
            if symbols_list and DISCORD_WEBHOOK_URL:
                tasks = [check_single_coin(session, symbol, DISCORD_WEBHOOK_URL) for symbol in symbols_list]
                await asyncio.gather(*tasks)
            
            elapsed = asyncio.get_event_loop().time() - start_time
            sleep_time = max(1, 15 - elapsed)
            await asyncio.sleep(sleep_time)

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"BingX Support Scanner Bot is running!")
    
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
        print("Бот зупинений користувачем.")
        
