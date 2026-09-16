import asyncio
import aiohttp
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# Налаштування (можеш підлаштувати під свої параметри)
VOLUME_MULTIPLIER = 2.5       # У скільки разів поточний об'єм має перевищувати середній
APPROACH_PERCENT = 0.003      # Як близько ціна має бути до рівня (0.3%)
TIMEFRAME = "1m"              # Робота на хвилинках для максимальної швидкості
LIMIT_CANDLES = 20            # Кількість свічок для розрахунку середнього об'єму

# Отримуємо вебхук з змінної середовища Render
DISCORD_WEBHOOK_URL = os.environ.get("BINGX_API_KEY")

async def send_to_discord(webhook_url, message):
    """Асинхронна відправка сповіщення у Discord"""
    if not webhook_url:
        print("Помилка: Не задано URL вебхука Discord!")
        return
        
    async with aiohttp.ClientSession() as session:
        payload = {"content": message}
        try:
            async with session.post(webhook_url, json=payload) as response:
                if response.status != 200:
                    print(f"Помилка відправки у Discord: {response.status}")
        except Exception as e:
            print(f"Виняток при відправці у Discord: {e}")

async def check_single_coin(client, symbol, discord_webhook_url):
    """Функція перевірки однієї монети (працює паралельно для кожної пари)"""
    try:
        kline_data = await client.get_kline(symbol=symbol, interval=TIMEFRAME, limit=LIMIT_CANDLES)
        
        if not kline_data or len(kline_data) < 10:
            return

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
            await send_to_discord(discord_webhook_url, alert_message)

    except Exception as e:
        pass

async def scan_market_cycle(client, symbols_list, discord_webhook_url):
    """Головний цикл, який запускає перевірку всіх монет одночасно"""
    tasks = [check_single_coin(client, symbol, discord_webhook_url) for symbol in symbols_list]
    await asyncio.gather(*tasks)

async def main():
    print("Бот запущено. Початок асинхронного сканування ринку...")
    
    # Приклад: якщо у тебе поки немає ініціалізованого клієнта, 
    # тут можна підключити твій SDK BingX та список монет:
    # client = BingXAsyncClient(...) 
    # symbols = ["BTC-USDT", "ETH-USDT"]
    
    client = None # Тимчасова заглушка, заміни на свій клієнт
    symbols = []  # Твій список монет
    
    while True:
        start_time = asyncio.get_event_loop().time()
        
        if client and symbols and DISCORD_WEBHOOK_URL:
            await scan_market_cycle(client, symbols, DISCORD_WEBHOOK_URL)
        else:
            print("Очікування налаштування клієнта або списку монет у main()...")
        
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
        # Вимикаємо зайві логи перевірок сервером у консолі
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
        
