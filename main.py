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

# --- Налаштування середовища ---
API_KEY = os.environ.get("BINGX_API_KEY", "")
API_SECRET = os.environ.get("BINGX_SECRET_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
RENDER_URL = os.environ.get("RENDER_URL", "https://bingx-scanner-djbf.onrender.com")
BINGX_BASE_URL = "https://open-api.bingx.com"

# Торгові параметри
LEVERAGE = 10
MARGIN_USD = 0.5  
MAX_OPEN_POSITIONS = 2  
server_time_offset = 0

tracked_positions = {}

# --- Keep-alive сервер для Render ---
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

# --- Допоміжні функції ---
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
            print(f"Час синхронізовано. Offset: {server_time_offset} ms", flush=True)
    except Exception as e:
        print(f"Помилка синхронізації часу: {e}", flush=True)

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
        print(f"Помилка отримання позицій: {e}", flush=True)
    return []

async def get_open_orders(session, symbol):
    path = "/openApi/swap/v2/trade/openOrders"
    ts = str(int(time.time() * 1000) + server_time_offset)
    p_str = f"symbol={symbol}&timestamp={ts}"
    sig = get_sign(API_SECRET, p_str)
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        async with session.get(f"{BINGX_BASE_URL}{path}?{p_str}&signature={sig}", headers=headers) as resp:
            res = await resp.json()
            if res.get("code") == 0:
                return res.get("data", {}).get("orders", [])
    except Exception as e:
        print(f"Помилка отримання відкритих ордерів для {symbol}: {e}", flush=True)
    return []

async def cancel_all_symbol_orders(session, symbol):
    path = "/openApi/swap/v2/trade/allOpenOrders"
    ts = str(int(time.time() * 1000) + server_time_offset)
    p_str = f"symbol={symbol}&timestamp={ts}"
    sig = get_sign(API_SECRET, p_str)
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        async with session.delete(f"{BINGX_BASE_URL}{path}?{p_str}&signature={sig}", headers=headers) as resp:
            pass
    except Exception as e:
        print(f"Помилка скасування ордерів для {symbol}: {e}", flush=True)

async def set_leverage(session, symbol):
    path = "/openApi/swap/v2/trade/leverage"
    ts = str(int(time.time() * 1000) + server_time_offset)
    p_str = f"leverage={LEVERAGE}&side=LONG&symbol={symbol}&timestamp={ts}"
    sig = get_sign(API_SECRET, p_str)
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        async with session.post(f"{BINGX_BASE_URL}{path}?{p_str}&signature={sig}", headers=headers) as resp:
            res = await resp.json()
            if res.get("code") == 0:
                return True
            return False
    except Exception:
        return False

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

def calculate_exit_levels(entry_price, low_price, total_qty):
    stop_loss = low_price * 0.995
    risk = entry_price - stop_loss
    if risk <= 0:
        return None
    tp1 = entry_price + (risk * 1.0)
    tp2 = entry_price + (risk * 2.0)
    tp3 = entry_price + (risk * 3.0)

    qty1 = round(total_qty * 0.4, 4)
    qty2 = round(total_qty * 0.3, 4)
    qty3 = round(total_qty - qty1 - qty2, 4)

    return {
        "stop_loss": round(stop_loss, 5),
        "tps": [
            {"price": round(tp1, 5), "qty": qty1},
            {"price": round(tp2, 5), "qty": qty2},
            {"price": round(tp3, 5), "qty": qty3}
        ]
    }

async def execute_trade(session, symbol, entry_price, low_price):
    open_pos = await get_open_positions(session)
    if open_pos is None:
        open_pos = []
    if len(open_pos) >= MAX_OPEN_POSITIONS:
        return

    if not await set_leverage(session, symbol):
        return

    notional_value = MARGIN_USD * LEVERAGE
    qty = round(notional_value / entry_price, 2)
    if qty <= 0:
        return

    path = "/openApi/swap/v2/trade/order"
    ts = str(int(time.time() * 1000) + server_time_offset)
    p_str = f"positionSide=LONG&side=BUY&symbol={symbol}&type=MARKET&quantity={qty}&timestamp={ts}"
    sig = get_sign(API_SECRET, p_str)
    headers = {"X-BX-APIKEY": API_KEY}
    
    try:
        async with session.post(f"{BINGX_BASE_URL}{path}?{p_str}&signature={sig}", headers=headers) as resp:
            res = await resp.json()
            if res.get("code") != 0:
                return
    except Exception:
        return

    levels = calculate_exit_levels(entry_price, low_price, qty)
    if not levels:
        return

    msg = f"🚀 *Вхід на відкаті (EMA50 + 1m імпульс)*\nМонета: `{symbol}`\nЦіна входу: `{entry_price}`\nСтоп-лос: `{levels['stop_loss']}`"
    await send_telegram(session, msg)

    await set_stop_loss(session, symbol, levels["stop_loss"], qty)
    for i, tp in enumerate(levels["tps"], 1):
        await set_take_profit(session, symbol, tp["price"], tp["qty"], i)

    tracked_positions[symbol] = {
        "entry_price": entry_price,
        "tp1_hit": False,
        "total_qty": qty
    }

async def set_stop_loss(session, symbol, stop_price, qty):
    path = "/openApi/swap/v2/trade/order"
    ts = str(int(time.time() * 1000) + server_time_offset)
    p_str = f"positionSide=LONG&price={stop_price}&side=SELL&stopPrice={stop_price}&symbol={symbol}&type=STOP_MARKET&quantity={qty}&timestamp={ts}"
    sig = get_sign(API_SECRET, p_str)
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        async with session.post(f"{BINGX_BASE_URL}{path}?{p_str}&signature={sig}", headers=headers) as resp:
            pass
    except Exception:
        pass

async def set_take_profit(session, symbol, tp_price, qty, tp_num):
    path = "/openApi/swap/v2/trade/order"
    ts = str(int(time.time() * 1000) + server_time_offset)
    p_str = f"positionSide=LONG&price={tp_price}&side=SELL&stopPrice={tp_price}&symbol={symbol}&type=TAKE_PROFIT_MARKET&quantity={qty}&timestamp={ts}"
    sig = get_sign(API_SECRET, p_str)
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        async with session.post(f"{BINGX_BASE_URL}{path}?{p_str}&signature={sig}", headers=headers) as resp:
            pass
    except Exception:
        pass

async def manage_positions(session):
    while True:
        await asyncio.sleep(10)
        try:
            positions = await get_open_positions(session)
            active_symbols = {p.get("symbol") for p in positions}

            for sym in list(tracked_positions.keys()):
                if sym not in active_symbols:
                    tracked_positions.pop(sym, None)
                    continue

                info = tracked_positions[sym]
                if not info["tp1_hit"]:
                    orders = await get_open_orders(session, sym)
                    tp_orders = [o for o in orders if o.get("type") == "TAKE_PROFIT_MARKET"]
                    
                    if len(tp_orders) < 3:
                        info["tp1_hit"] = True
                        await cancel_all_symbol_orders(session, sym)
                        
                        current_amt = 0
                        for p in positions:
                            if p.get("symbol") == sym:
                                current_amt = float(p.get("positionAmt", 0))
                                break
                        
                        if current_amt > 0:
                            entry_p = info["entry_price"]
                            await set_stop_loss(session, sym, entry_p, current_amt)
                            
                            qty2 = round(current_amt * 0.6, 4)
                            qty3 = round(current_amt - qty2, 4)
                            tp2_price = entry_p + ((entry_p - (entry_p * 0.995)) * 2.0)
                            tp3_price = entry_p + ((entry_p - (entry_p * 0.995)) * 3.0)
                            
                            await set_take_profit(session, sym, tp2_price, qty2, 2)
                            await set_take_profit(session, sym, tp3_price, qty3, 3)
                            await send_telegram(session, f"🛡 *Стоп перенесено в безубиток*\nМонета: `{sym}`")
        except Exception:
            pass

async def status_reporter(session):
    while True:
        await asyncio.sleep(900)  # Виправлено відсутній await
        try:
            positions = await get_open_positions(session)
            if not positions:
                report = "📊 *ЗВІТ БОТА (15 хв)*\nАктивних позицій немає."
            else:
                report = f"📊 *ЗВІТ БОТА (15 хв)*\nАктивні позиції ({len(positions)}/{MAX_OPEN_POSITIONS}):\n"
                for p in positions:
                    sym = p.get("symbol")
                    amt = p.get("positionAmt")
                    pnl = p.get("unrealizedProfit", "0")
                    report += f"• `{sym}` | Об'єм: `{amt}` | PnL: `{pnl}$`\n"
            await send_telegram(session, report)
        except Exception:
            pass

async def scan_market(session):
    try:
        open_pos = await get_open_positions(session)
        if open_pos is None:
            open_pos = []
        if len(open_pos) >= MAX_OPEN_POSITIONS:
            return

        url = f"{BINGX_BASE_URL}/openApi/swap/v2/quote/ticker"
        async with session.get(url) as resp:
            if resp.status != 200:
                return
            data = await resp.json()
            if not isinstance(data, dict) or data.get("code") != 0:
                return
            
            tickers = data.get("data", [])
            if not isinstance(tickers, list):
                return
            
            for ticker in tickers:
                if not isinstance(ticker, dict):
                    continue
                symbol = ticker.get("symbol", "")
                if not symbol.endswith("USDT"):
                    continue
                
                try:
                    change_24h = float(ticker.get("priceChangePercent", 0))
                except (ValueError, TypeError):
                    continue

                if 3.0 <= change_24h <= 25.0:
                    klines_15m = await get_klines(session, symbol, interval="15m", limit=60)
                    if not klines_15m or len(klines_15m) < 50:
                        continue
                    
                    try:
                        # Безпечний парсинг свічок (перевірка чи елемент є списком/кортежем)
                        closes_15m = [float(k[4]) for k in klines_15m if isinstance(k, (list, tuple)) and len(k) > 4]
                        if len(closes_15m) < 50:
                            continue
                        
                        ema_50 = calculate_ema(closes_15m, period=50)
                        current_price = float(ticker.get("lastPrice", closes_15m[-1]))
                        
                        if current_price < ema_50:
                            continue
                        
                        lows_15m = [float(k[3]) for k in klines_15m if isinstance(k, (list, tuple)) and len(k) > 3]
                        if not lows_15m:
                            continue
                    except (ValueError, TypeError, IndexError):
                        continue

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
                    
                    if last_vol_1m > avg_vol_1m * 3.0:
                        try:
                            lows_1m = [float(k[3]) for k in valid_1m if len(k) > 3]
                            low_price = min(lows_1m[-10:]) if lows_1m else current_price * 0.99
                            entry_price = current_price
                        except (IndexError, ValueError, TypeError):
                            continue
                        
                        await execute_trade(session, symbol, entry_price, low_price)
                        await asyncio.sleep(5)
                        
    except Exception as e:
        print(f"Помилка сканування: {e}", flush=True)

async def main():
    keep_alive()
    async with aiohttp.ClientSession() as session:
        await sync_time(session)
        print("Бот запущено за оновленою стратегією...", flush=True)
        
        asyncio.create_task(status_reporter(session))
        asyncio.create_task(manage_positions(session))
        
        while True:
            await sync_time(session)
            await scan_market(session)
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
                            
