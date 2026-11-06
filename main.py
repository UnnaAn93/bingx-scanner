import os
import asyncio
import aiohttp
import threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
BINGX_BASE_URL = "https://open-api.bingx.com"

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is running!")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()

async def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload, timeout=10) as resp:
                pass
        except:
            pass

async def fetch_klines(session, symbol):
    url = f"{BINGX_BASE_URL}/openApi/swap/v1/market/klines"
    params = {"symbol": symbol, "interval": "1D", "limit": 6}
    try:
        async with session.get(url, params=params, timeout=5) as resp:
            if resp.status == 200:
                data = await resp.json()
                return symbol, data.get("data", [])
    except:
        pass
    return symbol, []

async def scan_daily_sweeps():
    try:
        print("[INFO] Початок сканування ринку по останніх свічках...", flush=True)
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BINGX_BASE_URL}/openApi/swap/v2/quote/contracts", timeout=10) as resp:
                if resp.status != 200:
                    print("[ERROR] Не вдалося отримати список контрактів", flush=True)
                    return
                res = await resp.json()

        contracts_data = res if isinstance(res, list) else res.get("data", [])
        symbols = [item["symbol"] for item in contracts_data if isinstance(item, dict) and "symbol" in item]
        print(f"[INFO] Завантажено контрактів: {len(symbols)}. Аналізуємо...", flush=True)

        bullish_sweeps = []
        bearish_sweeps = []

        batch_size = 40
        async with aiohttp.ClientSession() as session:
            for i in range(0, len(symbols), batch_size):
                batch = symbols[i:i + batch_size]
                tasks = [fetch_klines(session, sym) for sym in batch]
                results = await asyncio.gather(*tasks)

                for symbol, klines_1d in results:
                    if len(klines_1d) >= 4:
                        # Перевіряємо останні три закриті свічки (-4, -3, -2) відносно їх попередників
                        for idx in [-2, -3]:
                            candle_1 = klines_1d[idx]      фокусна свічка
                            candle_2 = klines_1d[idx-1]  # орієнтир

                            try:
                                high_2 = float(candle_2['high'])
                                low_2 = float(candle_2['low'])
                                high_1 = float(candle_1['high'])
                                low_1 = float(candle_1['low'])
                                close_1 = float(candle_1['close'])

                                # Ведмежий свіп (хай вище, закриття нижче)
                                if high_1 > high_2 and close_1 < high_2:
                                    bearish_sweeps.append(f"`{symbol}` (High Sweep)")
                                    break  # знайшли свіп для цієї монети, переходимо до наступної
                                
                                # Бичачий свіп (лоу нижче, закриття вище)
                                elif low_1 < low_2 and close_1 > low_2:
                                    bullish_sweeps.append(f"`{symbol}` (Low Sweep)")
                                    break
                            except:
                                continue

                await asyncio.sleep(0.02)

        print(f"[INFO] Знайдено біт-сигналів -> Бичачих: {len(bullish_sweeps)}, Ведмежих: {len(bearish_sweeps)}", flush=True)

        # Формування та обмеження звіту (топ-5 кожного типу для збереження читабельності)
        report = "🔔 *Щоденний звіт по свіпах (Топ за останні свічки)*\n\n"
        
        if bullish_sweeps:
            report += "🟢 *Бичачі свіпи (Лонг):*\n" + "\n".join(bullish_sweeps[:5]) + "\n\n"
        if bearish_sweeps:
            report += "🔴 *Ведмежі свіпи (Шорт):*\n" + "\n".join(bearish_sweeps[:5]) + "\n\n"
        
        if not bullish_sweeps and not bearish_sweeps:
            report += "ℹ️ За останні три свічки чітких свіпів за заданими умовами не виявлено."

        await send_telegram(report)
        print("[INFO] Звіт успішно надіслано в Telegram!", flush=True)

    except Exception as e:
        print(f"[ERROR] Помилка у сканері: {e}", flush=True)

async def daily_sweep_loop():
    print("[INFO] Виконується стартовий звіт...", flush=True)
    await scan_daily_sweeps()
    while True:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=0, minute=5, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        
        sleep_seconds = (target - now).total_seconds()
        await asyncio.sleep(sleep_seconds)
        await scan_daily_sweeps()

async def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    await send_telegram("🚀 Оновлений сканер свіпів запущено!")
    await daily_sweep_loop()

if __name__ == "__main__":
    asyncio.run(main())
    
