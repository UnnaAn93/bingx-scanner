import asyncio
import aiohttp
# Тут підключи свої імпорти для клієнта BingX, якщо використовуєш офіційну чи кастомну бібліотеку

# Налаштування (можеш підлаштувати під свої параметри)
VOLUME_MULTIPLIER = 2.5       # У скільки разів поточний об'єм має перевищувати середній
APPROACH_PERCENT = 0.003      # Як близько ціна має бути до рівня (0.3%)
TIMEFRAME = "1m"              # Робота на хвилинках для максимальної швидкості
LIMIT_CANDLES = 20            # Кількість свічок для розрахунку середнього об'єму

async def send_to_discord(webhook_url, message):
    """Асинхронна відправка сповіщення у Discord"""
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
        # Робимо асинхронний запит свічок (приклад виклику залежить від твого SDK BingX)
        # Головне — отримати дані по 1m таймфрейму
        kline_data = await client.get_kline(symbol=symbol, interval=TIMEFRAME, limit=LIMIT_CANDLES)
        
        if not kline_data or len(kline_data) < 10:
            return

        # Витягуємо об'єми, максимуми та ціни закриття
        # (Формат даних залежить від того, чи повертає BingX словники чи списки, нижче універсальний приклад)
        volumes = [float(x['volume']) for x in kline_data]
        highs = [float(x['high']) for x in kline_data]
        closes = [float(x['close']) for x in kline_data]
        
        # Середній об'єм за попередні свічки (виключаючи саму поточну відкриту свічку [-1])
        avg_volume = sum(volumes[-(LIMIT_CANDLES - 1):-1]) / (LIMIT_CANDLES - 2)
        current_volume = volumes[-1] # Об'єм свічки, що зараз формується
        
        # Рівень опору (наприклад, локальний максимум за попередній період)
        resistance_level = max(highs[:-1])
        current_price = closes[-1]
        
        # Розраховуємо відстань до рівня та сплеск об'єму
        if resistance_level > 0:
            distance_to_resistance = (resistance_level - current_price) / resistance_level
        else:
            return
            
        is_volume_spike = current_volume >= (avg_volume * VOLUME_MULTIPLIER)
        is_approaching = 0 <= distance_to_resistance <= APPROACH_PERCENT

        # Якщо об'єм різко стріляє, а ціна треться об рівень — кидаємо сигнал
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
        # Логуємо помилку по конкретній монеті, щоб бот не падав
        # print(f"Помилка по {symbol}: {e}")
        pass

async def scan_market_cycle(client, symbols_list, discord_webhook_url):
    """Головний цикл, який запускає перевірку всіх монет одночасно"""
    # Створюємо список асинхронних задач для кожної монети з твого списку
    tasks = [check_single_coin(client, symbol, discord_webhook_url) for symbol in symbols_list]
    
    # ОСЬ ТЕ САМЕ gather: пускає все паралельно і чекає завершення пакету
    await asyncio.gather(*tasks)

async def main():
    # Ініціалізація твого клієнта BingX та посилання на вебхук
    # client = BingXAsyncClient(...) 
    # symbols = ["BTC-USDT", "ETH-USDT", "HYPE-USDT", ...] # Твій список сканування
    # webhook = "ТВОЄ_ПОСИЛАННЯ_НА_DISCORD_WEBHOOK"

    print("Бот запущено. Початок асинхронного сканування ринку...")
    
    while True:
        start_time = asyncio.get_event_loop().time()
        
        # Запуск сканування всього списку
        # await scan_market_cycle(client, symbols, webhook)
        
        # Робимо невелику паузу між циклами (наприклад, кожні 5-10 секунд)
        elapsed = asyncio.get_event_loop().time() - start_time
        sleep_time = max(1, 10 - elapsed) # Час паузи з урахуванням тривалості запитів
        await asyncio.sleep(sleep_time)

# Запуск програми локально або на Render
# if __name__ == "__main__":
#     asyncio.run(main())
