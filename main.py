import asyncio
import aiohttp
import os
import time
import hmac
import hashlib
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import traceback

API_KEY = os.environ.get("BINGX_API_KEY", "")
API_SECRET = os.environ.get("BINGX_SECRET_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
RENDER_URL = os.environ.get("RENDER_URL", "https://bingx-scanner-djbf.onrender.com")
BINGX_BASE_URL = "https://open-api.bingx.com"

LEVERAGE = 10
MARGIN_USD = 0.5  
MAX_OPEN_POSITIONS = 2  
server_time_offset = 0

tracked_positions = {}

class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive and running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    server.serve_forever()

def keep_alive():
    t = threading.Thread(target=run_server)
    t.daemon = True
    t.start()

async def self_ping(session):
    while True:
        await asyncio.sleep(420)
        if RENDER_URL:
            try:
                async with session.get(RENDER_URL) as resp:
                    print(f"🏓 Self-ping виконано, статус: {resp.status}", flush=True)
            except Exception as e:
                print(f"⚠️ Помилка self-ping: {e}", flush=True)

def get_sign(secret_key: str, payload: str) -> str:
    return hmac.new(secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()

async def send_telegram(session, message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        async with session.post(url, json=payload) as resp:
            pass
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)

async def sync_time(session):
    global server_time_offset
    try:
        async with session.get(f"{BINGX_BASE_URL}/openApi/swap/v1/server/time") as resp:
            data = await resp.json()
            server_time = data.get("serverTime", int(time.time() * 1000))
            local_time = int(time.time() * 1000)
            server_time_offset = server_time - local_time
            print(f"🕒 Час синхронізовано. Offset: {server_time_offset} ms", flush=True)
    except Exception as e:
        print(f"⚠️ Помилка синхронізації часу: {e}", flush=True)

async def get_open_positions(session):
    path = "/openApi/swap/v2/user/positions"
    ts = str(int(time.time() * 1000) + server_time_offset)
    p_str = f"timestamp={ts}"
    sig = get_sign(API_SECRET, p_str)
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        async with session.get(f"{BINGX_BASE_URL}{path}?{p_str}&signature={sig}", headers=headers) as resp:
            res = await resp.json()
            if res.get("code") == 0:
                positions = [p for p in res.get("data", []) if float(p.get("positionAmt", 0)) != 0]
                return positions
    except Exception as e:
        print(f"⚠️ Помилка отримання позицій: {e}", flush=True)
    return []

async def get_klines(session, symbol, interval="1m", limit=50):
    url = f"{BINGX_BASE_URL}/openApi/swap/v3/quote/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        async with session.get(url) as resp:
            data = await resp.json()
            if not isinstance(data, dict) or data.get("code") != 0:
                return []
            klines = data.get("data", [])
            if not isinstance(klines, list):
                return []
            return klines
    except Exception:
        return []

def calculate_ema(closes, period=50):
    if len(closes) < period:
        return sum(closes) / len(closes) if closes else 0
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

async def execute_trade(session, symbol, entry_price, low_price):
    print(f"🚀 Спроба відкриття позиції по {symbol} (Ціна: {entry_price})", flush=True)
    await send_telegram(session, f"🟢 Знайдено кандидат для входу: `{symbol}` за ціною `{entry_price}`")

async def scan_market(session):
    print("🔄 Початок нового циклу сканування ринку...", flush=True)
    try:
        open_pos = await get_open_positions(session)
        if open_pos is None:
            open_pos = []
        
        print(f"💼 Активних позицій на біржах: {len(open_pos)}/{MAX_OPEN_POSITIONS}", flush=True)
        if len(open_pos) >= MAX_OPEN_POSITIONS:
            print("⛔ Сканування зупинено: досягнуто ліміт відкритих позицій.", flush=True)
            return

        url = f"{BINGX_BASE_URL}/openApi/swap/v2/quote/ticker"
        async with session.get(url) as resp:
            if resp.status != 200:
                print(f"⚠️ Помилка запиту тікерів: статус {resp.status}", flush=True)
                return
            data = await resp.json()
            if not isinstance(data, dict) or data.get("code") != 0:
                return
            
            tickers = data.get("data", [])
            if not isinstance(tickers, list):
                return
            
            scanned_count = 0
            matched_count = 0
            passed_ema = 0
            passed_vol = 0

            for ticker in tickers:
                if not isinstance(ticker, dict):
                    continue
                symbol = ticker.get("symbol", "")
                if not symbol.endswith("USDT"):
                    continue
                
                scanned_count += 1
                try:
                    change_24h = float(ticker.get("priceChangePercent", 0))
                except (ValueError, TypeError):
                    continue

                # Фільтр зростання (1% - 35%)
                if 1.0 <= change_24h <= 35.0:
                    matched_count += 1
                    
                    klines_15m = await get_klines(session, symbol, interval="15m", limit=60)
                    if not klines_15m or len(klines_15m) < 50:
                        continue
                    
                    try:
                        closes_15m = [float(k[4]) for k in klines_15m if isinstance(k, (list, tuple)) and len(k) > 4]
                        if len(closes_15m) < 50:
                            continue
                        
                        ema_50 = calculate_ema(closes_15m, period=50)
                        current_price = float(ticker.get("lastPrice", closes_15m[-1]))
                        
                        if current_price < ema_50:
                            continue
                    except (ValueError, TypeError, IndexError):
                        continue

                    passed_ema += 1  # Пройшли EMA50

                    klines_1m = await get_klines(session, symbol, interval="1m", limit=20)
                    if not klines_1m or len(klines_1m) < 15:
                        continue
                        
                    try:
                        valid_1m = [k for k in klines_1m if isinstance(k, (list, tuple)) and len(k) > 5]
                        if len(valid_1m) < 15:
                            continue

                        volumes_1m = [float(k[5]) for k in valid_1m]
                        avg_vol_1m = sum(volumes_1m[:-1]) / len(volumes_1m[:-1]) if len(volumes_1m) > 1 else 1
                        last_vol_1m = volumes_1m[-1]
                    except (IndexError, ValueError, TypeError):
                        continue
                    
                    # Пом'якшений множник об'єму (1.4 замість 2.2)
                    if last_vol_1m > avg_vol_1m * 1.4:
                        passed_vol += 1
                        print(f"🎯 Успіх! Монета {symbol} пройшла всі фільтри!", flush=True)
                        try:
                            lows_1m = [float(k[3]) for k in valid_1m if len(k) > 3]
                            low_price = min(lows_1m[-10:]) if lows_1m else current_price * 0.99
                            entry_price = current_price
                        except (IndexError, ValueError, TypeError):
                            continue
                        
                        await execute_trade(session, symbol, entry_price, low_price)
                        await asyncio.sleep(5)
                        
            print(f"🔍 Підсумок: перевірено {scanned_count}, ріст 1-35%: {matched_count}, пройшли EMA50: {passed_ema}, пройшли об'єм: {passed_vol}", flush=True)
            
    except Exception as e:
        print(f"❌ Помилка у scan_market: {e}", flush=True)
        traceback.print_exc()

async def main():
    keep_alive()
    async with aiohttp.ClientSession() as session:
        print("🚀 Запуск головної функції бота...", flush=True)
        await sync_time(session)
        print("✅ Бот успішно запущено, переходимо до безперервного циклу!", flush=True)
        await send_telegram(session, "🟢 *Бот оновлено з розширеною деталізацією логів!*")
        
        asyncio.create_task(self_ping(session))
        
        while True:
            try:
                await scan_market(session)
            except Exception as e:
                print(f"❌ Помилка у загальному циклі: {e}", flush=True)
            print("⏳ Очікування 60 секунд до наступного сканування...\n", flush=True)
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
                        
