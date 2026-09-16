import asyncio
import aiohttp
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# Налаштування параметрів сканування
VOLUME_MULTIPLIER = 2.5       # У скільки разів поточний об'єм має перевищувати середній
APPROACH_PERCENT = 0.003      # Як близько ціна має бути до рівня (0.3%)
TIMEFRAME = "1m"              # Робота на хвилинках для максимальної швидкості
LIMIT_CANDLES = 20            # Кількість свічок для розрахунку середнього об'єму
TOP_COINS_LIMIT = 150         # Кількість найактивніших пар для сканування

# Отримуємо вебхук із змінної середовища Render
DISCORD_WEBHOOK_URL = os.environ.get("BINGX_API_KEY")

async def fetch_top_bingx_symbols():
    """Автоматично завантажує список найактивніших USDT-пар з ф'ючерсів BingX за об'ємом"""
    url = "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    tickers = data.get("data", [])
                    # Фільтруємо лише USDT-пари та сортуємо за об'ємом (volume)
                    usdt_tickers = [
                        t for t in tickers 
                        if t.get("symbol", "").endswith("-USDT")
                    ]
                    # Сортуємо за спаданням об'єму
                    usdt_tickers.sort(key=lambda x: float(x.get("volume", 0)), reverse=True)
                    
                    # Беремо топ-150
                    top_symbols = [t["symbol"] for t in usdt_tickers[:TOP_COINS_LIMIT]]
                    return top_symbols
        except Exception as e:
            print(f"Помилка при отриманні списку монет від BingX: {e}")
        return []

async def fetch_kline_data(session, symbol):
    """Отримує свічки (kline) для конкретної пари через публічне API BingX"""
    url = "https://open-api.bingx.com/openApi/swap/v2/quote/klines"
    params = {
        "symbol": symbol,
        "interval": TIMEFRAME,
        "limit": LIMIT_CANDLES
    }
    try:
        async with session.get(url, params=params, timeout=5) as response:
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
        # У BingX API дані свічок приходять списком словників або масивом (залежно від версії, перевіряємо ключі)
        # Зазвичай публічне API v2/quote/klines повертає поля: volume, high, close
        volumes = [float(x['volume']) for x in kline_data]
        highs = [float(x['high']) for x in kline_data]
        closes = [float(x['close']) for x in kline_data]
        
        avg_volume = sum(volumes[-(LIMIT_CANDLES - 1):-1]) / (LIMIT_CANDLES - 2)
        current_volume = volumes[-1]
        
        resistance_level = max(highs[:-1])
        current_price = closes[-1]
        
        if resistance_level > 0:
            distance_to_resistance = (resistance_level - current_price) / resistance_level
        else:
            return
            
        is_volume_spike = current_volume >= (avg_volume * VOLUME_MULTIPLIER)
        is_approaching = 0 <= distance_to_resistance <= APPROACH_PERCENT

        if is_volume_spike and is_approaching:
            surge_percent = int((current_volume / avg_volume - 1) * 100)
            alert_message = (
                f"🎯⚡ **УВАГА [Зона інтересу / Спалах об'єму]**: `{symbol}` (1m)\n"
                f"• Ціна біля опору: `{current_price}` (Рівень: `{resistance_level}`)\n"
                f"• Об'єм активної свічки: `+{surge_percent}%` до середнього!\n"
                f"⏳ Лови шпильку / готуйся до розвороту!"
            )
            await send_to_discord(session, discord_webhook_url, alert_message)

    except Exception:
        pass

async def main():
    print("Бот запущено. Початок сканування 150 найактивніших пар BingX...")
    
    async with aiohttp.ClientSession() as session:
        while True:
            start_time = asyncio.get_event_loop().time()
            
            # Оновлюємо список топ-150 пар на випадок зміни ліквідності на ринку
            symbols_list = await fetch_top_bingx_symbols()
            
            if symbols_list and DISCORD_WEBHOOK_URL:
                print(f"Сканування ринку для {len(symbols_list)} активних пар...")
                # Запускаємо паралельні перевірки для всіх пар одразу
                tasks = [check_single_coin(session, symbol, DISCORD_WEBHOOK_URL) for symbol in symbols_list]
                await asyncio.gather(*tasks)
            else:
                print("Не вдалося отримати список пар або відсутній вебхук Discord.")
            
            # Пауза між циклами сканування (наприклад, 10 секунд)
            elapsed = asyncio.get_event_loop().time() - start_time
            sleep_time = max(1, 10 - elapsed)
            await asyncio.sleep(sleep_time)


# --- Веб-сервер для утримання порта на Render ---
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
    # Запускаємо веб-сервер у фоновому потоці
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

    # Запускаємо асинхронного бота
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот зупинений користувачем.")
        
