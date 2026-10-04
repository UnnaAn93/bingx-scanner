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
                    print(f"🏓 Self-ping выполнен, статус: {resp.status}", flush=True)
            except Exception as e:
                print(f"⚠️ Ошибка self-ping: {e}", flush=True)

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
            print(f"🕒 Время синхронизировано. Offset: {server_time_offset} ms", flush=True)
    except Exception as e:
        print(f"⚠️ Ошибка синхронизации времени: {e}", flush=True)

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
        print(f"⚠ Ошибка получения позиций: {e}", flush=True)
    return []

async def get_klines(session, symbol, interval="1m", limit=60):
    url = f"{BINGX_BASE_URL}/openApi/swap/v3/quote/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        async with session.get(url) as resp:
            data = await resp.json()
            if not isinstance(data, dict) or data.get("code") != 0:
                print(f"⚠️ Ошибка свечей для {symbol}: code={data.get('code')}, msg={data.get('msg')}", flush=True)
                return []
            klines = data.get("data", [])
            if not isinstance(klines, list):
                return []
            return klines
    except Exception as e:
        print(f"⚠️️ Исключение в get_klines для {symbol}: {e}", flush=True)
        return []

def calculate_ema(closes, period=50):
    if not closes:
        return 0
    if len(closes) < period:
        period = len(closes)
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

async def execute_trade(session, symbol, entry_price, low_price):
    print(f"🚀 Попытка открытия позиции по {symbol} (Цена: {entry_price})", flush=True)
    await send_telegram(session, f"🟢 Найден кандидат для входа: `{symbol}` по цене `{entry_price}`")

async def scan_market(session):
    print("🔄 Начало нового цикла сканирования рынка...", flush=True)
    try:
        open_pos = await get_open_positions(session)
        if open_pos is None:
            open_pos = []
        
        print(f"💼 Активных позиций на биржах: {len(open_pos)}/{MAX_OPEN_POSITIONS}", flush=True)
        if len(open_pos) >= MAX_OPEN_POSITIONS:
            print("⛔ Сканирование остановлено: достигнут лимит открытых позиций.", flush=True)
            return

        url = f"{BINGX_BASE_URL}/openApi/swap/v2/quote/ticker"
        async with session.get(url) as resp:
            if resp.status != 200:
                print(f"⚠️ Ошибка запроса тикеров: статус {resp.status}", flush=True)
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
                    current_price = float(ticker.get("lastPrice", 0))
                except (ValueError, TypeError):
                    continue

                # Фильтр роста (1% - 35%)
                if 1.0 <= change_24h <= 35.0:
                    matched_count += 1
                    
                    # Получаем 15m свечи для EMA50
                    klines_15m = await get_klines(session, symbol, interval="15m", limit=60)
                    if not klines_15m or len(klines_15m) < 10:
                        continue
                    
                    try:
                        closes_15m = []
                        for k in klines_15m:
                            if isinstance(k, (list, tuple)) and len(k) > 4:
                                closes_15m.append(float(k[4]))
                        
                        if len(closes_15m) < 10:
                            continue
                        
                        ema_50 = calculate_ema(closes_15m, period=50)
                        if current_price == 0:
                            current_price = closes_15m[-1]
                        
                        # Мягкий фильтр EMA50 (допускаем небольшой откат до 98.5%)
                        if ema_50 > 0 and current_price < ema_50 * 0.985:
                            continue
                    except Exception:
                        continue

                    passed_ema += 1

                    # Получаем 1m свечи для объема
                    klines_1m = await get_klines(session, symbol, interval="1m", limit=25)
                    if not klines_1m or len(klines_1m) < 15:
                        continue
                        
                    try:
                        valid_1m = []
                        for k in klines_1m:
                            if isinstance(k, (list, tuple)) and len(k) > 5:
                                valid_1m.append(k)
                        
                        if len(valid_1m) < 15:
                            continue

                        volumes_1m = [float(k[5]) for k in valid_1m]
                        avg_vol_1m = sum(volumes_1m[:-1]) / len(volumes_1m[:-1]) if len(volumes_1m) > 1 else 1
                        last_vol_1m = volumes_1m[-1]
                    except Exception:
                        continue
                    
                    # Множитель объема 1.4
                    if last_vol_1m > avg_vol_1m * 1.4:
                        passed_vol += 1
                        print(f"🎯 Успех! Монета {symbol} прошла все фильтры! (Цена: {current_price}, EMA50: {round(ema_50, 4)})", flush=True)
                        try:
                            lows_1m = [float(k[3]) for k in valid_1m if len(k) > 3]
                            low_price = min(lows_1m[-10:]) if lows_1m else current_price * 0.99
                            entry_price = current_price
                        except Exception:
                            entry_price = current_price
                            low_price = current_price * 0.99
                        
                        await execute_trade(session, symbol, entry_price, low_price)
                        await asyncio.sleep(5)
                        
            print(f"🔍 Итог: проверено {scanned_count}, рост 1-35%: {matched_count}, прошли EMA50: {passed_ema}, прошли объем: {passed_vol}", flush=True)
            
    except Exception as e:
        print(f"❌ Ошибка в scan_market: {e}", flush=True)
        traceback.print_exc()

async def main():
    keep_alive()
    async with aiohttp.ClientSession() as session:
        print("🚀 Запуск главной функции бота...", flush=True)
        await sync_time(session)
        print("✅ Бот успешно запущен, переходим к непрерывному циклу!", flush=True)
        await send_telegram(session, "🟢 *Бот обновлен: добавлено логирование ошибок свечей!*")
        
        asyncio.create_task(self_ping(session))
        
        while True:
            try:
                await scan_market(session)
            except Exception as e:
                print(f"❌ Ошибка в общем цикле: {e}", flush=True)
            print("⏳ Ожидание 60 секунд до следующего сканирования...\n", flush=True)
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
                            
