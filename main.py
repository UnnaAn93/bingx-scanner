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

async def scan_daily_sweeps():
    try:
        print("[INFO] Початок стабільного сканування ринку...", flush=True)
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BINGX_BASE_URL}/openApi/swap/v2/quote/contracts", timeout=10) as resp:
                if resp.status != 200:
                    print("[ERROR] Не вдалося отримати список контрактів", flush=True)
                    return
                res = await resp.json()

        contracts_data = res if isinstance(res, list) else res.get("data", [])
        symbols = [item["symbol"] for item in contracts_data if isinstance(item, dict) and "symbol" in item]
        print(f"[INFO] Завантажено контрактів: {len(symbols)}", flush=True)

        bullish_sweeps = []
        bearish_sweeps = []

        async with aiohttp.ClientSession() as session:
            for symbol in symbols:
                url = f"{BINGX_BASE_URL}/openApi/swap/v1/market/klines"
                params = {"symbol": symbol, "interval": "1D", "limit": 5}
                try:
                    async with session.get(url, params=params, timeout=5) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            klines_1d = data.get("data", [])
                            
                            if len(klines_1d) >= 3:
                                candle_2 = klines_1d[-3]  # Орієнтир
                                candle_1 = klines_1d[-2]  # Закрита свічка

                                high_2 = float(candle_2['high'])
                                low_2 = float(candle_2['low'])
                                high_1 = float(candle_1['high'])
                                low_1 = float(candle_1['low'])
                                close_1 = float(candle_1['close'])

                                if "LSK" in symbol or "BCH" in symbol:
                                    print(f"[CHECK] {symbol} | C2 High:{high_2} Low:{low_2} || C1 High:{high_1} Low:{low_1} Close:{close_1}", flush=True)

                                if high_1 > high_2 and close_1 < high_2:
                                    bearish_sweeps.append(f"`{symbol}` (High Sweep)")
                                elif low_1 < low_2 and close_1 > low_2:
                                    bullish_sweeps.append(f"`{symbol}` (Low Sweep)")
                except:
                    pass
                
                await asyncio.sleep(0.02)

        print(f"[INFO] Сканування завершено. Бичачих: {len(bullish_sweeps)}, Ведмежих: {len(bearish_sweeps)}", flush=True)

        # Формування повного звіту в Telegram (зберігаємо звичний формат)
        report = "🔔 *Щоденний звіт по свіпах (1D)*\n\n"
        if bullish_sweeps:
            report += "🟢 *Бичачі свіпи (Знято лой свічки 2 -> Лонг):*\n" + "\n".join(bullish_sweeps) + "\n\n"
        if bearish_sweeps:
            report += "🔴 *Ведмежі свіпи (Знято хай свічки 2 -> Шорт):*\n" + "\n".join(bearish_sweeps) + "\n\n"
        
        if not bullish_sweeps and not bearish_sweeps:
            report += "ℹ️ За минулу добу чітких свіпів за заданими умовами не виявлено."

        await send_telegram(report)
        print("[INFO] Звіт успішно надіслано в Telegram!", flush=True)

    except Exception as e:
        print(f"[ERROR] Помилка у сканері: {e}", flush=True)

async def daily_sweep_loop():
    print("[INFO] Виконується позаплановий звіт при стартові...", flush=True)
    await scan_daily_sweeps()
    while True:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=0, minute=5, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        
        sleep_seconds = (target - now).total_seconds()
        print(f"[INFO] Наступний звіт о 00:05 UTC (через {int(sleep_seconds // 3600)} год.)", flush=True)
        await asyncio.sleep(sleep_seconds)
        await scan_daily_sweeps()

async def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    await send_telegram("🚀 Сканер денних свіпів успішно запущено!")
    await daily_sweep_loop()

if __name__ == "__main__":
    asyncio.run(main())
    
