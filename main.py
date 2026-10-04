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
import urllib.parse

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
    params = {"timestamp": ts}
    query_str = urllib.parse.urlencode(sorted(params.items()))
    sig = get_sign(API_SECRET, query_str)
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        async with session.get(f"{BINGX_BASE_URL}{path}?{query_str}&signature={sig}", headers=headers) as resp:
            res = await resp.json()
            if res.get("code") == 0:
                positions = [p for p in res.get("data", []) if float(p.get("positionAmt", 0)) != 0]
                return positions
    except Exception as e:
        print(f"⚠️ Помилка отримання позицій: {e}", flush=True)
    return []

async def set_leverage(session, symbol):
    path = "/openApi/swap/v2/trade/leverage"
    ts = str(int(time.time() * 1000) + server_time_offset)
    
    params = {
        "leverage": str(LEVERAGE),
        "side": "LONG",
        "symbol": symbol,
        "timestamp": ts
    }
    query_str = urllib.parse.urlencode(sorted(params.items()))
    sig = get_sign(API_SECRET, query_str)
    
    url = f"{BINGX_BASE_URL}{path}?{query_str}&signature={sig}"
    headers = {
        "X-BX-APIKEY": API_KEY,
        "Content-Type": "application/x-www-form-urlencoded"
    }
    try:
        async with session.post(url, headers=headers) as resp:
            res = await resp.json()
            print(f"⚙️ Встановлення плеча для {symbol}: {res}", flush=True)
    except Exception as e:
        print(f"⚠️ Помилка встановлення плеча: {e}", flush=True)

async def execute_trade(session, symbol, entry_price):
    print(f"🚀 Спроба реального відкриття позиції по {symbol} (Ціна: {entry_price})", flush=True)
    
    await set_leverage(session, symbol)

    try:
        target_usd = MARGIN_USD * LEVERAGE
        quantity = f"{target_usd / entry_price:.4f}"
        if float(quantity) <= 0:
            print(f"⚠️ Занадто мала кількість для ордера {symbol}", flush=True)
            return
    except Exception as e:
        print(f"⚠️ Помилка розрахунку кількості: {e}", flush=True)
        return

    path = "/openApi/swap/v2/trade/order"
    ts = str(int(time.time() * 1000) + server_time_offset)
    
    params = {
        "positionSide": "LONG",
        "quantity": str(quantity),
        "side": "BUY",
        "symbol": symbol,
        "timestamp": ts,
        "type": "MARKET"
    }
    
    query_str = urllib.parse.urlencode(sorted(params.items()))
    sig = get_sign(API_SECRET, query_str)
    
    url = f"{BINGX_BASE_URL}{path}?{query_str}&signature={sig}"
    headers = {
        "X-BX-APIKEY": API_KEY,
        "Content-Type": "application/x-www-form-urlencoded"
    }

    try:
        async with session.post(url, headers=headers) as resp:
            res = await resp.json()
            print(f"📦 Відповідь біржі на відкриття ордера {symbol}: {res}", flush=True)
            if res.get("code") == 0:
                await send_telegram(session, f"🟢 *Успішно відкрито LONG по `{symbol}`*!\nЦіна: `{entry_price}`\nКількість: `{quantity}`")
            else:
                await send_telegram(session, f"🔴 Помилка відкриття `{symbol}`: {res.get('msg')}")
    except Exception as e:
        print(f"⚠️ Виняток при відправці ордера для {symbol}: {e}", flush=True)

async def get_klines(session, symbol, interval="1m", limit=60):
    url = f"{BINGX_BASE_URL}/openApi/swap/v3/quote/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        async with session.get(url) as resp:
            data = await resp.json()
            if not isinstance(data, dict) or data.get("code") != 0:
                return []
            klines = data.get("data", [])
            if not isinstance(klines, list) or len(klines) == 0:
                return []
            return klines
    except Exception as e:
        print(f"⚠️ Виняток у get_klines для {symbol}: {e}", flush=True)
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
                
                if not symbol.endswith("USDT") or "-" in symbol[:-5] or "USD" in symbol[:-4]:
                    continue
                
                # Відсікаємо топ-монети за назвою
                if "BNB" in symbol or "BTC" in symbol or "ETH" in symbol or "SOL" in symbol or "XRP" in symbol:
                    continue

                scanned_count += 1
                try:
                    change_24h = float(ticker.get("priceChangePercent", 0))
                    current_price = float(ticker.get("lastPrice", 0))
                    volume_24h = float(ticker.get("volume", 0)) * current_price
                except (ValueError, TypeError):
                    continue

                # Фільтр об'єму: від 300k до 20 млн доларів
                if volume_24h < 300_000 or volume_24h > 20_000_000:
                    continue

                # Діапазон росту від 5% до 35%
                if 5.0 <= change_24h <= 35.0:
                    matched_count += 1
                    
                    klines_15m = await get_klines(session, symbol, interval="15m", limit=60)
                    if not klines_15m or len(klines_15m) < 10:
                        continue
                    
                    try:
                        closes_15m = []
                        for k in klines_15m:
                            if isinstance(k, dict) and "close" in k:
                                closes_15m.append(float(k["close"]))
                        
                        if len(closes_15m) < 10:
                            continue
                        
                        ema_50 = calculate_ema(closes_15m, period=50)
                        if current_price == 0:
                            current_price = closes_15m[-1]
                        
                        if ema_50 > 0 and current_price < ema_50 * 0.985:
                            continue
                    except Exception:
                        continue

                    passed_ema += 1

                    klines_1m = await get_klines(session, symbol, interval="1m", limit=25)
                    if not klines_1m or len(klines_1m) < 15:
                        continue
                        
                    try:
                        valid_1m = []
                        for k in klines_1m:
                            if isinstance(k, dict) and "volume" in k and "close" in k:
                                valid_1m.append(k)
                        
                        if len(valid_1m) < 15:
                            continue

                        volumes_1m = [float(k["volume"]) for k in valid_1m]
                        avg_vol_1m = sum(volumes_1m[:-1]) / len(volumes_1m[:-1]) if len(volumes_1m) > 1 else 1
                        last_vol_1m = volumes_1m[-1]
                    except Exception:
                        continue
                    
                    if last_vol_1m > avg_vol_1m * 1.4:
                        passed_vol += 1
                        print(f"🎯 Успіх! Малокап {symbol} пройшов усі фільтри! (Ціна: {current_price}, Об'єм 24h: ${int(volume_24h)}, Ріст: {change_24h}%)", flush=True)
                        
                        await execute_trade(session, symbol, current_price)
                        await asyncio.sleep(5)
                        
            print(f"🔍 Підсумок: перевірено {scanned_count}, ріст 5-35%: {matched_count}, пройшли EMA50: {passed_ema}, пройшли об'єм: {passed_vol}", flush=True)
            
    except Exception as e:
        print(f"❌ Помилка у scan_market: {e}", flush=True)
        traceback.print_exc()

async def main():
    keep_alive()
    async with aiohttp.ClientSession() as session:
        print("🚀 Запуск головної функції бота...", flush=True)
        await sync_time(session)
        print("✅ Бот успішно запущено, переходимо до безперервного циклу!", flush=True)
        await send_telegram(session, "🟢 *Бот оновлено: виправлено підпис API, об'єм $300k-$20M, ріст 5-35%!*")
        
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
        
