import asyncio
import aiohttp
import os
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# Налаштування параметрів сканування для Bitget (15m)
VOLUME_MULTIPLIER = 2.2           # Сплеск об'єму у 2.2 рази
APPROACH_PERCENT = 0.008          # 0.8% до рівня (підтримки або опору)
TIMEFRAME = "15m"                 # Таймфрейм 15 хвилин
LIMIT_CANDLES = 40                # Історія свічок
TOP_COINS_LIMIT = 150             # Кількість найактивніших пар
MIN_24H_VOLUME_USDT = 5_000_000   # Мінімальний добовий об'єм у USDT
COOLDOWN_SECONDS = 300            # Кулдаун 5 хвилин на одну монету

# Вебхук та налаштування сервера беруться з Environment Variables на Render
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
RENDER_URL = os.environ.get("RENDER_URL", "https://bitget-scanner-djbf.onrender.com")

BITGET_BASE_URL = "https://api.bitget.com"

last_alert_time = {}

async def fetch_top_bitget_symbols(session):
    url = f"{BITGET_BASE_URL}/api/v2/mix/market/tickers?productType=USDT-FUTURES"
    try:
        async with session.get(url, timeout=5) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("code") == "00000":
                    list_tickers = data.get("data", [])
                    usdt_tickers = []
                    for t in list_tickers:
                        symbol = t.get("symbol", "")
                        if symbol.endswith("USDT"):
                            try:
                                quote_vol = float(t.get("usdtVolume", 0))
                                if quote_vol >= MIN_24H_VOLUME_USDT:
                                    usdt_tickers.append((symbol, quote_vol))
                            except Exception:
                                continue
                    usdt_tickers.sort(key=lambda x: x[1], reverse=True)
                    return [item[0] for item in usdt_tickers[:TOP_COINS_LIMIT]]
    except Exception as e:
        print(f"Помилка при отриманні списку монет від Bitget: {e}")
    return []

async def fetch_kline_data(session, symbol):
    url = f"{BITGET_BASE_URL}/api/v2/mix/market/candles"
    params = {
        "symbol": symbol,
        "productType": "USDT-FUTURES",
        "granularity": TIMEFRAME,
        "limit": str(LIMIT_CANDLES)
    }
    try:
        async with session.get(url, params=params, timeout=4) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("code") == "00000":
                    raw_list = data.get("data", [])
                    raw_list.sort(key=lambda x: int(x[0]))
                    formatted = []
                    for item in raw_list:
                        formatted.append({
                            "high": float(item[2]),
                            "low": float(item[3]),
                            "close": float(item[4]),
                            "volume": float(item[5])
                        })
                    return formatted
    except Exception:
        pass
    return None

async def send_to_discord(session, webhook_url, message):
    if not webhook_url:
        print("Попередження: DISCORD_WEBHOOK_URL не задано в середовищі!")
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

        # 1. Перевірка на ЛОНГ (Підтримка знизу)
        support_level = min(lows[:-1])
        if support_level > 0:
            distance_to_support = (current_price - support_level) / support_level
            if 0 <= distance_to_support <= APPROACH_PERCENT:
                alert_message = (
                    f"🟢🎯 **УВАГА [ЛОНГ / Підтримка 15m]**:\n`{symbol}`\n"
                    f"• Напрямок: 🚀 **Підхід до локального дна / Збір ліквідності**\n"
                    f"• Ціна: `{current_price}` (Підтримка: `{support_level}`)\n"
                    f"• Об'єм свічки: `+{surge_percent}%` від середнього!\n"
                    f"⏳ Готуйся до можливого відскоку вгору!"
                )
                last_alert_time[symbol] = current_time
                await send_to_discord(session, discord_webhook_url, alert_message)
                print(f"Лонг сигнал 15m для {symbol}")
                return

        # 2. Перевірка на ШОРТ (Опір зверху)
        resistance_level = max(highs[:-1])
        if resistance_level > 0:
            distance_to_resistance = (resistance_level - current_price) / resistance_level
            if 0 <= distance_to_resistance <= APPROACH_PERCENT:
                alert_message = (
                    f"🔴🎯 **УВАГА [ШОРТ / Опір 15m]**:\n`{symbol}`\n"
                    f"• Напрямок: 📉 **Підхід до локального хаю / Зона опору**\n"
                    f"• Ціна: `{current_price}` (Опір: `{resistance_level}`)\n"
                    f"• Об'єм свічки: `+{surge_percent}%` від середнього!\n"
                    f"⏳ Готуйся до можливого відбою вниз!"
                )
                last_alert_time[symbol] = current_time
                await send_to_discord(session, discord_webhook_url, alert_message)
                print(f"Шорт сигнал 15m для {symbol}")
                return

    except Exception as e:
        pass

# Механізм захисту від засинання (Self-Ping)
async def self_ping_loop(session):
    while True:
        await asyncio.sleep(240)
        try:
            async with session.get(RENDER_URL, timeout=5) as response:
                pass
        except Exception:
            pass

async def main():
    print("Бот Captain Hook сканує ринок Bitget на 15m...")
    if not DISCORD_WEBHOOK_URL:
        print("УВАГА: Змінна середовища DISCORD_WEBHOOK_URL не налаштована!")
    
    async with aiohttp.ClientSession() as session:
        asyncio.create_task(self_ping_loop(session))
        
        while True:
            start_time = asyncio.get_event_loop().time()
            
            symbols_list = await fetch_top_bitget_symbols(session)
            
            if symbols_list:
                tasks = [check_single_coin(session, symbol, DISCORD_WEBHOOK_URL) for symbol in symbols_list]
                await asyncio.gather(*tasks)
            
            elapsed = asyncio.get_event_loop().time() - start_time
            sleep_time = max(1, 15 - elapsed)
            await asyncio.sleep(sleep_time)

# Локальний вебсервер для підтримки життєздатності сервісу на Render
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Captain Hook Bitget Scanner Bot is running!")
    
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
        
