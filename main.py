import asyncio
import aiohttp
import os
import time
import hmac
import hashlib
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# --- Налаштування середовища ---
API_KEY = os.environ.get("BINGX_API_KEY", "")
API_SECRET = os.environ.get("BINGX_SECRET_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
RENDER_URL = os.environ.get("RENDER_URL", "https://bingx-scanner-djbf.onrender.com")
BINGX_BASE_URL = "https://open-api.bingx.com"

# Торгові параметри
LEVERAGE = 10
MARGIN_USD = 0.5  # Маржа на позицію
MAX_OPEN_POSITIONS = 2  # Максимум одночасних позицій
server_time_offset = 0

# Словник для відстеження стану позицій (для перенесення стопа в б/у після TP1)
# Формат: {symbol: {"entry_price": float, "tp1_hit": bool, "stop_order_id": str}}
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
            else:
                err_msg = f"❌ *Помилка встановлення плеча* `{symbol}`\nПричина: `{res}`"
                print(err_msg, flush=True)
                await send_telegram(session, err_msg)
                return False
    except Exception as e:
        err_msg = f"❌ *Виняток встановлення плеча* `{symbol}`\nПричина: `{e}`"
        print(err_msg, flush=True)
        await send_telegram(session, err_msg)
        return False

async def get_klines(session, symbol, interval="1m", limit=20):
    url = f"{BINGX_BASE_URL}/openApi/swap/v3/quote/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        async with session.get(url) as resp:
            data = await resp.json()
            if data.get("code") == 0:
                return data.get("data", [])
    except Exception as e:
        print(f"Помилка отримання свічок для {symbol}: {e}", flush=True)
    return []

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

    msg = f"🚀 *Вхід у позицію (Малокапіталка)*\nМонета: `{symbol}`\nЦіна входу: `{entry_price}`\nСтоп-лос: `{levels['stop_loss']}`"
    await send_telegram(session, msg)
    print(msg, flush=True)

    await set_stop_loss(session, symbol, levels["stop_loss"], qty)
    for i, tp in enumerate(levels["tps"], 1):
        await set_take_profit(session, symbol, tp["price"], tp["qty"], i)

    # Зберігаємо для трекінгу перенесення стопа в безубиток
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

# --- Фоновий менеджер позицій (Перенесення стопа в б/у після TP1) ---
async def manage_positions(session):
    while True:
        await asyncio.sleep(10)
        try:
            positions = await get_open_positions(session)
            active_symbols = {p.get("symbol") for p in positions}

            # Видаляємо з трекінгу закриті позиції
            for sym in list(tracked_positions.keys()):
                if sym not in active_symbols:
                    tracked_positions.pop(sym, None)
                    continue

                info = tracked_positions[sym]
                if not info["tp1_hit"]:
                    # Перевіряємо відкриті ордери: якщо TP1 зник (спрацював), переносимо стоп на точку входу
                    orders = await get_open_orders(session, sym)
                    tp_orders = [o for o in orders if o.get("type") == "TAKE_PROFIT_MARKET"]
                    
                    # Якщо залишилось менше Тейк-Профітів (значить перший спрацював)
                    if len(tp_orders) < 3:
                        info["tp1_hit"] = True
                        print(f"Спрацював TP1 для {sym}! Переносимо стоп-лос у безубиток (ТВХ)...", flush=True)
                        
                        # Скасовуємо старий стоп-лос та інші лімітки/тейки перед перенесенням
                        await cancel_all_symbol_orders(session, sym)
                        
                        # Знаходимо поточний об'єм позиції, що залишився
                        current_amt = 0
                        for p in positions:
                            if p.get("symbol") == sym:
                                current_amt = float(p.get("positionAmt", 0))
                                break
                        
                        if current_amt > 0:
                            # Встановлюємо новий стоп-лос на ціну входу (безубиток)
                            entry_p = info["entry_price"]
                            await set_stop_loss(session, sym, entry_p, current_amt)
                            
                            # Переставляємо решту Тейк-Профітів (TP2 та TP3)
                            risk = entry_p * 0.005 # Приблизний крок ризику для перерахунку, або залишаємо початкові пропорції
                            # Або просто залишаємо старий TP2/TP3 якщо вони не скасовувались, або виставляємо заново
                            # Оскільки cancel_all скасував усе, відновимо TP2 та TP3 на основі залишку об'єму
                            qty2 = round(current_amt * 0.6, 4)
                            qty3 = round(current_amt - qty2, 4)
                            tp2_price = entry_p + ((entry_p - (entry_p * 0.995)) * 2.0)
                            tp3_price = entry_p + ((entry_p - (entry_p * 0.995)) * 3.0)
                            
                            await set_take_profit(session, sym, tp2_price, qty2, 2)
                            await set_take_profit(session, sym, tp3_price, qty3, 3)

                            await send_telegram(session, f"🛡 *Стоп перенесено в безубиток (ТВХ)*\nМонета: `{sym}`\nЦіна входу: `{entry_p}`")

        except Exception as e:
            print(f"Помилка в manage_positions: {e}", flush=True)

async def status_reporter(session):
    while True:
        await asyncio.sleep(900)
        try:
            positions = await get_open_positions(session)
            if positions is None:
                positions = []
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
        except Exception as e:
            print(f"Помилка відправки звіту: {e}", flush=True)

# --- Сканер ринку ---
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

                if 3.0 <= change_24h <= 20.0:
                    klines = await get_klines(session, symbol, interval="1m", limit=15)
                    if not klines or len(klines) < 15:
                        continue
                        
                    try:
                        volumes = [float(k[5]) for k in klines]
                        avg_volume = sum(volumes[:-1]) / len(volumes[:-1]) if len(volumes) > 1 else 1
                        last_volume = volumes[-1]
                    except (IndexError, ValueError, TypeError):
                        continue
                    
                    if last_volume > avg_volume * 3.0:
                        try:
                            lows = [float(k[3]) for k in klines]
                            low_price = min(lows[-10:])
                            entry_price = float(ticker.get("lastPrice", klines[-1][4]))
                        except (IndexError, ValueError, TypeError):
                            continue
                        
                        print(f"Знайдено сплеск об'єму для {symbol}! Входимо...", flush=True)
                        await execute_trade(session, symbol, entry_price, low_price)
                        await asyncio.sleep(5)
                        
    except Exception as e:
        print(f"Помилка сканування (виняток): {e}", flush=True)

async def main():
    keep_alive()
    async with aiohttp.ClientSession() as session:
        await sync_time(session)
        print("Бот запущено та сканує ринок...", flush=True)
        
        await send_telegram(session, "🟢 *Бот успішно запущено та оновлено!*\nДодано перенесення стопа в безубиток після TP1, інтервал сканування — 1 хв.")
        
        asyncio.create_task(status_reporter(session))
        asyncio.create_task(manage_positions(session))
        
        while True:
            await sync_time(session)
            await scan_market(session)
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
    
