import asyncio
import aiohttp
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# Налаштування параметрів сканування
VOLUME_MULTIPLIER = 2.5           # У скільки разів поточний об'єм має перевищувати середній
APPROACH_PERCENT = 0.5            # 0.5% до рівня
TIMEFRAME = "1m"                  # Робота на хвилинках
LIMIT_CANDLES = 20                # Кількість свічок для середнього об'єму
TOP_COINS_LIMIT = 150             # Кількість найактивніших пар
MIN_24H_VOLUME_USDT = 5_000_000   # Мінімальний добовий об'єм у USDT (5 мільйонів), щоб відсіяти шлам

# Отримуємо вебхук із змінної середовища Render
DISCORD_WEBHOOK_URL = os.environ.get("BINGX_API_KEY")
RENDER_URL = "https://bingx-scanner-djbf.onrender.com"

async def fetch_top_bingx_symbols(session):
    """Автоматично завантажує список найактивніших USDT-пар з ф'ючерсів BingX з фільтром ліквідності"""
    url = "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"
    try:
        async with session.get(url, timeout=5) as response:
            if response.status == 200:
                data = await response.json()
                tickers = data.get("data", [])
                
                usdt_tickers = []
                for t in tickers:
                    symbol = t.get("symbol", "")
                    if symbol.endswith("-USDT"):
                        # Отримуємо добовий об'єм у USDT (у тікері BingX це поле quoteVolume або volume в USDT)
                        quote_vol = float(t.get("quoteVolume", 0))
                        if quote_vol >= MIN_24H_VOLUME_USDT:
                            usdt_tickers.append((symbol, quote_vol))
                
                # Сортуємо за спаданням добового об'єму
                usdt_tickers.sort(key=lambda x: x[1], reverse=True)
                
                # Беремо топ
                top_symbols = [item[0] for item in usdt_tickers[:TOP_COINS_LIMIT]]
                return top_symbols
    except Exception as e:
        print(f"Помилка при отриманні списку монет від BingX: {e}")
    return []

async def fetch_kline_data(session, symbol):
    """Отримує свічки (kline) через публічне API BingX"""
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
    """Асинхронна відправка сповіщення у Discord"""
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
    """Функція перевірки однієї монети"""
    kline_data = await fetch_kline_data(session, symbol)
    
    if not kline_data or len(kline_data) < 10:
        return

    try:
        volumes = [float(x['volume']) for x in kline_data]
        highs = [float(x['high']) for x in kline_data]
        closes = [float(x['close']) for x in kline_data]
        opens = [float(x['open']) for x in kline_data]
        
        # Перевірка, чи свічки взагалі мають об'єм (відсікаємо повністю мертві свічки)
        current_volume = volumes[-1]
        if current_volume <= 0:
            return

        avg_volume = sum(volumes[-(LIMIT_CANDLES - 1):-1]) / (LIMIT_CANDLES - 2)
        if avg_volume <= 0:
            return
        
        resistance_level = max(highs[:-1])
        current_price = closes[-1]
        current_open = opens[-1]
        
        if resistance_level > 0:
            distance_to_resistance = (resistance_level - current_price) / resistance_level
        else:
            return
            
        is_volume_spike = current_volume >= (avg_volume * VOLUME_MULTIPLIER)
        is_approaching = 0 <= distance_to_resistance <= APPROACH_PERCENT

        if is_volume_spike and is_approaching:
            surge_percent = int((current_volume / avg_volume - 1) * 100)
            
            if current_price >= current_open:
                side_emoji = "🟢"
                side_text = "Кульмінація покупців (Лонг / Зелена свічка)"
            else:
                side_emoji = "🔴"
                side_text = "Кульмінація продавців (Шорт / Червона свічка)"

            alert_message = (
                f"🎯⚡ **УВАГА [Спалах об'єму]**: `{symbol}` (1m)\n"
                f"• Напрямок: {side_emoji} **{side_text}**\n"
                f"• Ціна біля опору: `{current_price}` (Рівень: `{resistance_level}`)\n"
                f"• Об'єм активної свічки: `+{surge_percent}%` до середнього!\n"
                f"⏳ Лови шпильку / готуйся до розвороту!"
            )
            await send_to_discord(session, discord_webhook_url, alert_message)

    except Exception:
        pass

async def self_ping_loop(session):
    """Фонова задача для запобігання засинанню на Render"""
    while True:
        await asyncio.sleep(240)
        try:
            async with session.get(RENDER_URL, timeout=5) as response:
                pass
        except Exception:
            pass

async def main():
    print("Бот запущено. Початок сканування ліквідних пар з фільтром об'ємів...")
    
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


# --- Веб-сервер для Render ---
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"BingX Scanner Bot is running!")
    
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
                                
