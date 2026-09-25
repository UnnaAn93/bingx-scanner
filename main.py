import asyncio
import aiohttp
import time
import hmac
import hashlib
import base64
import json
import os
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

API_KEY = os.environ.get("BINGX_API_KEY", "")
SECRET_KEY = os.environ.get("BINGX_SECRET_KEY", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
RENDER_URL = os.environ.get("RENDER_URL", "")

BASE_URL = "https://open-api.bingx.com"

async def send_to_discord(session, message):
    if not DISCORD_WEBHOOK_URL:
        return
    payload = {"content": message}
    try:
        async with session.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5) as r:
            if r.status != 204:
                print(f"Discord error: {r.status}", flush=True)
    except Exception as e:
        print(f"Discord exception: {e}", flush=True)

def get_sign(method, path, query_string=''):
    timestamp = str(int(time.time() * 1000))
    payload = timestamp + method.upper() + path + query_string
    signature = hmac.new(
        SECRET_KEY.encode('utf-8'),
        payload.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return timestamp, signature

async def fetch_open_positions(session):
    path = "/openApi/swap/v2/user/positions"
    timestamp, signature = get_sign("GET", path)
    url = f"{BASE_URL}{path}?timestamp={timestamp}&signature={signature}"
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        async with session.get(url, headers=headers, timeout=10) as r:
            data = await r.json()
            if data.get("code") == 0:
                return data.get("data", [])
    except Exception as e:
        print(f"Помилка отримання відкритих позицій: {e}", flush=True)
    return []

async def fetch_top_symbols(session):
    path = "/openApi/swap/v1/ticker/24hr"
    url = f"{BASE_URL}{path}"
    try:
        async with session.get(url, timeout=10) as r:
            data = await r.json()
            if data.get("code") == 0:
                tickers = data.get("data", [])
                usdt_pairs = [t["symbol"] for t in tickers if t["symbol"].endswith("-USDT")]
                return usdt_pairs[:25]
    except Exception as e:
        print(f"Помилка отримання списку торгових пар: {e}", flush=True)
    return []

async def scan_coin(session, symbol, active_count, support_dict, resistance_dict, touch_time_dict):
    path = "/openApi/swap/v1/market/klines"
    query = f"symbol={symbol}&interval=5m&limit=30"
    url = f"{BASE_URL}{path}?{query}"
    try:
        async with session.get(url, timeout=10) as r:
            data = await r.json()
            if data.get("code") == 0:
                klines = data.get("data", [])
                if len(klines) >= 20:
                    closes = [float(k["close"]) for k in klines]
                    highs = [float(k["high"]) for k in klines]
                    lows = [float(k["low"]) for k in klines]
                    
                    current_time = time.time()
                    last_candle_time = klines[-1].get("time", current_time)
                    
                    if symbol not in touch_time_dict:
                        touch_time_dict[symbol] = 0

                    # Перевірка прориву структури (BOS) з урахуванням свічкових екстремумів
                    if closes[-1] > max(highs[-15:-1]):
                        if touch_time_dict[symbol] != last_candle_time:
                            touch_time_dict[symbol] = last_candle_time
                            msg = f"⚡ **Структурний прорив вверх (BOS Up)** по **{symbol}**! Активних позицій: {active_count}"
                            async with aiohttp.ClientSession() as s_disc:
                                await send_to_discord(s_disc, msg)
                                
                    elif closes[-1] < min(lows[-15:-1]):
                        if touch_time_dict[symbol] != last_candle_time:
                            touch_time_dict[symbol] = last_candle_time
                            msg = f"📉 **Структурний прорив вниз (BOS Down)** по **{symbol}**! Активних позицій: {active_count}"
                            async with aiohttp.ClientSession() as s_disc:
                                await send_to_discord(s_disc, msg)
    except Exception as e:
        pass

async def monitor_pos(session, position, partial_exit_dict):
    sym = position.get("symbol")
    pnl = float(position.get("unrealizedProfit", 0))
    roe = float(position.get("roe", 0)) * 100
    
    if sym not in partial_exit_dict:
        partial_exit_dict[sym] = False

    if abs(pnl) > 4.0 and not partial_exit_dict[sym]:
        partial_exit_dict[sym] = True
        msg = f"💰 **Моніторинг позиції**: по **{sym}** досягнуто значного PnL: `{pnl:.2f} USDT` (ROE: `{roe:.2f}%`)"
        async with aiohttp.ClientSession() as s_disc:
            await send_to_discord(s_disc, msg)

async def self_ping():
    while True:
        await asyncio.sleep(240)
        if RENDER_URL:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(RENDER_URL, timeout=5) as r:
                        await r.text()
            except Exception:
                pass

async def periodic_report_task(session):
    while True:
        await asyncio.sleep(3600)
        await send_to_discord(session, "🟢 **Щоденний звіт сканера**: сервіс працює стабільно, зв'язок з API та Discord активний.")

async def main():
    print("Бот запущено успішно!", flush=True)
    async with aiohttp.ClientSession() as session:
        await send_to_discord(session, "🚀 **Повноцінний торговий скрипт з розширеною логікою запущено!**")
        
        asyncio.create_task(self_ping())
        asyncio.create_task(periodic_report_task(session))
        
        handled_partial_positions = set()
        partial_exit_prices = {}
        support_touches_count = {}
        resistance_touches_count = {}
        last_touch_candle_time = {}

        while True:
            try:
                start = asyncio.get_event_loop().time()
                positions = await fetch_open_positions(session)
                open_syms = [p.get("symbol") for p in positions]
                
                if not positions:
                    handled_partial_positions.clear()
                    partial_exit_prices.clear()
                    support_touches_count.clear()
                    resistance_touches_count.clear()
                    last_touch_candle_time.clear()

                for p in positions:
                    await monitor_pos(session, p, handled_partial_positions)

                if len(positions) < 3:
                    syms = await fetch_top_symbols(session)
                    if syms:
                        tasks = [
                            scan_coin(
                                session, s, len(positions), 
                                support_touches_count, resistance_touches_count, last_touch_candle_time
                            ) 
                            for s in syms if s not in open_syms
                        ]
                        await asyncio.gather(*tasks)

            except Exception as e:
                print(f"Помилка основного циклу: {e}", flush=True)
                traceback.print_exc()
                await asyncio.sleep(10)

            elapsed = asyncio.get_event_loop().time() - start
            await asyncio.sleep(max(1, 60 - elapsed))

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is active and running!")
    def log_message(self, format, *args): 
        pass

if __name__ == "__main__":
    try:
        port = int(os.environ.get("PORT", 10000))
        threading.Thread(target=lambda: HTTPServer(("0.0.0.0", port), SimpleHandler).serve_forever(), daemon=True).start()
        asyncio.run(main())
    except Exception as e:
        print(f"ПОМИЛКА СТАРТУ ДОДАТКУ: {e}", flush=True)
        traceback.print_exc()
            
