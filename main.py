import os
import asyncio
import aiohttp
import threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

# --- НАЛАШТУВАННЯ ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
BINGX_BASE_URL = "https://open-api.bingx.com"

# --- HTTP-СЕРВЕР ДЛЯ RENDER (Health Check) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Daily Sweep Bot is alive and running!")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()

def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

async def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[ERROR] Не задано TELEGRAM_BOT_TOKEN або TELEGRAM_CHAT_ID")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload, timeout=10) as resp:
                if resp.status != 200:
                    print(f"[ERROR] Помилка відправки в Telegram: {resp.status}")
        except Exception as e:
            print(f"[ERROR] Виняток при відправці в Telegram: {e}")

async def bingx_request(method, endpoint, params=None):
    url = f"{BINGX_BASE_URL}{endpoint}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.request(method, url, params=params, timeout=15) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            print(f"[ERROR] Помилка запиту до BingX: {e}")
    return None

def is_blacklisted(symbol):
    return "USDC" in symbol or "EUR" in symbol

# --- СКАНЕР ДЕННИХ СВІПІВ (1D) ---
async def scan_daily_sweeps():
    try:
        print("[INFO] Розпочато сканування даних свіпів (1D) по свічках 1 і 2...", flush=True)
        res = await bingx_request("GET", "/openApi/swap/v2/quote/contracts")
        if not res:
            await send_telegram("⚠️ *Помилка денного сканера:* не вдалося отримати список монет від BingX.")
            return

        contracts_data = res if isinstance(res, list) else res.get("data", [])
        symbols = [item["symbol"] for item in contracts_data if isinstance(item, dict) and "symbol" in item]

        bullish_sweeps = []
        bearish_sweeps = []

        for symbol in symbols:
            if is_blacklisted(symbol):
                continue

            url = f"{BINGX_BASE_URL}/openApi/swap/v1/market/klines"
            params = {
                "symbol": symbol,
                "interval": "1D",
                "limit": 5
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        klines_1d = data.get("data", [])
                        
                        if len(klines_1d) >= 3:
                            candle_2 = klines_1d[-3]  # Свічка 2 (орієнтир)
                            candle_1 = klines_1d[-2]  # Свічка 1 (остання закрита, що робить свіп)

                            high_2 = float(candle_2['high'])
                            low_2 = float(candle_2['low'])

                            high_1 = float(candle_1['high'])
                            low_1 = float(candle_1['low'])
                            close_1 = float(candle_1['close'])

                            # Ведмежий свіп: тінь свічки 1 вища за хай свічки 2, але закриття нижче хаю свічки 2
                            if high_1 > high_2 and close_1 < high_2:
                                bearish_sweeps.append(f"`{symbol}` (High Sweep: High {high_1} > High2 {high_2} -> Close {close_1})")

                            # Бичачий свіп: тінь свічки 1 нижча за лой свічки 2, але закриття вище лою свічки 2
                            elif low_1 < low_2 and close_1 > low_2:
                                bullish_sweeps.append(f"`{symbol}` (Low Sweep: Low {low_1} < Low2 {low_2} -> Close {close_1})")

            await asyncio.sleep(0.03)

        # Формування звіту
        report = "🔔 *Щоденний звіт по свіпах (1D)*\n\n"
        if bullish_sweeps:
            report += "🟢 *Бичачі свіпи (Знято лой свічки 2 -> Лонг):*\n"
            report += "\n".join(bullish_sweeps) + "\n\n"

        if bearish_sweeps:
            report += "🔴 *Ведмежі свіпи (Знято хай свічки 2 -> Шорт):*\n"
            report += "\n".join(bearish_sweeps) + "\n\n"

        if not bullish_sweeps and not bearish_sweeps:
            report += "ℹ️ За минулу добу чітких свіпів за заданими умовами не виявлено."

        await send_telegram(report)
        print("[INFO] Звіт по денних свіпах успішно надіслано!", flush=True)

    except Exception as e:
        print(f"[ERROR] Помилка сканування даних свіпів: {e}", flush=True)
        await send_telegram(f"⚠️ *Помилка сканера свіпів:* {e}")

async def daily_sweep_loop():
    print("[INFO] Виконується позаплановий звіт при стартові...", flush=True)
    await scan_daily_sweeps()

    while True:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=0, minute=5, second=0, microsecond=0)

        if now >= target:
            target += timedelta(days=1)

        sleep_seconds = (target - now).total_seconds()
        print(f"[INFO] Наступний денний звіт о 00:05 UTC (через {int(sleep_seconds // 3600)} год. {int((sleep_seconds % 3600) // 60)} хв.)", flush=True)

        await asyncio.sleep(sleep_seconds)
        await scan_daily_sweeps()

async def main():
    server_thread = threading.Thread(target=run_http_server, daemon=True)
    server_thread.start()
    print("[INFO] HTTP-сервер для Health Check запущено.")

    print("[🚀 Окремий бот звітів по свіпах успішно запущено!]", flush=True)
    await send_telegram("🚀 Окремий бот звітів по денних свіпах успішно запущено!")

    await daily_sweep_loop()

if __name__ == "__main__":
    asyncio.run(main())
            
