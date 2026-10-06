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
import math

API_KEY = os.environ.get("BINGX_API_KEY", "")
API_SECRET = os.environ.get("BINGX_SECRET_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
RENDER_URL = os.environ.get("RENDER_URL", "https://bingx-scanner-djbf.onrender.com")
BINGX_BASE_URL = "https://open-api.bingx.com"

LEVERAGE = 10
MARGIN_USD = 0.5
MAX_RISK_POSITIONS = 2
server_time_offset = 0

active_trade_monitors = {}

class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive and running")

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
                    print(f"🔄 Self-ping виконано, статус: {resp.status}", flush=True)
            except Exception as e:
                print(f"⚠️ Помилка self-ping: {e}", flush=True)

def get_sign(secret_key: str, payload: str) -> str:
    return hmac.new(secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()

async def send_telegram(session, message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        async with session.post(url, json=payload) as resp:
            res_text = await resp.text()
            if resp.status != 200:
                print(f"⚠️️ Telegram API помилка: {resp.status} - {res_text}", flush=True)
    except Exception as e:
        print(f"⚠️ Telegram error: {e}", flush=True)

async def sync_time(session):
    global server_time_offset
    try:
        async with session.post(f"{BINGX_BASE_URL}/openApi/swap/v1/server/time") as resp:
            data = await resp.json()
            server_time = data.get("serverTime", int(time.time() * 1000))
            local_time = int(time.time() * 1000)
            server_time_offset = server_time - local_time
            print(f"⏱ Час синхронізовано. Offset: {server_time_offset} ms", flush=True)
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
    for pos_side in ["LONG", "SHORT"]:
        ts = str(int(time.time() * 1000) + server_time_offset)
        params = {
            "leverage": str(LEVERAGE),
            "positionSide": pos_side,
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
                print(f"⚙️ Встановлення плеча {LEVERAGE}x ({pos_side}) для {symbol}: {res}", flush=True)
        except Exception as e:
            print(f"⚠️ Помилка встановлення плеча: {e}", flush=True)

async def get_klines(session, symbol, interval="15m", limit=60):
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

async def place_stop_loss_order(session, symbol, quantity_str, stop_price, position_side="LONG"):
    await asyncio.sleep(1.5)
    path = "/openApi/swap/v2/trade/order"
    ts = str(int(time.time() * 1000) + server_time_offset)
    
    # Кількість обов'язково додатна (для шортів positionAmt буває з мінусом)
    clean_qty = str(abs(float(quantity_str)))
    
    # Протилежний бік для закриття позиції ринковим стопом
    side_to_close = "SELL" if position_side == "LONG" else "BUY"

    params = {
        "positionSide": position_side,
        "quantity": clean_qty,
        "side": side_to_close,
        "symbol": symbol,
        "timestamp": ts,
        "type": "STOP_MARKET",
        "stopPrice": str(stop_price),
        "workingType": "MARK_PRICE"
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
            print(f"🛡 Відповідь біржі на встановлення Stop-Loss для {symbol}: {res}", flush=True)
            return res.get("code") == 0
    except Exception as e:
        print(f"⚠️ Помилка створення стоп-лосу для {symbol}: {e}", flush=True)
        return False

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
            print(f"🛡 Відповідь біржі на встановлення Stop-Loss для {symbol}: {res}", flush=True)
            return res.get("code") == 0
    except Exception as e:
        print(f"⚠️ Помилка створення стоп-лосу для {symbol}: {e}", flush=True)
        return False

async def monitor_open_trades(session):
    open_pos = await get_open_positions(session)
    if not open_pos:
        active_trade_monitors.clear()
        return

    url = f"{BINGX_BASE_URL}/openApi/swap/v2/quote/ticker"
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return
            data = await resp.json()
            if not isinstance(data, dict) or data.get("code") != 0:
                return

            tickers = {t.get("symbol"): float(t.get("lastPrice", 0)) for t in data.get("data", []) if isinstance(t, dict)}

            for p in open_pos:
                symbol = p.get("symbol")
                entry_price = float(p.get("avgPrice", 0))
                if entry_price == 0:
                    continue

                current_price = tickers.get(symbol, 0)
                if current_price == 0:
                    continue

                if symbol not in active_trade_monitors:
                    risk = entry_price * 0.02
                    p_amt = float(p.get("positionAmt", 0))
                    pos_side = p.get("positionSide", "LONG" if p_amt > 0 else "SHORT")
                    tp1 = entry_price + risk if pos_side == "LONG" else entry_price - risk

                    active_trade_monitors[symbol] = {
                        'entry_price': entry_price,
                        'tp1': tp1,
                        'side': pos_side,
                        'sl_moved': False
                }

                info = active_trade_monitors[symbol]
                tp1 = info['tp1']
                sl_moved = info['sl_moved']
                trade_side = info.get('side', 'LONG')

                # Перевіряємо досягнення TP1 лише тоді, коли ціна реально відійшла від входу
                if trade_side == 'LONG':
                    tp_reached = current_price >= tp1
                else:
                    tp_reached = current_price <= tp1

                if not sl_moved and tp_reached:
                    print(f"🎯 TP1 досягнуто по {symbol}! Переносимо стоп в безубиток.", flush=True)

                    # 1. Отримуємо відкриті ордери, щоб знайти ID старого стоп-лоса
                    orders_path = "/openApi/swap/v2/trade/openOrders"
                    ts = str(int(time.time() * 1000) + server_time_offset)
                    params = {"symbol": symbol, "timestamp": ts}
                    query_str = urllib.parse.urlencode(sorted(params.items()))
                    sig = get_sign(API_SECRET, query_str)
                    orders_url = f"{BINGX_BASE_URL}{orders_path}?{query_str}&signature={sig}"
                    headers = {
                        "X-BX-APIKEY": API_KEY,
                        "Content-Type": "application/x-www-form-urlencoded"
                    }

                    async with session.get(orders_url, headers=headers) as o_resp:
                        o_data = await o_resp.json()
                        if o_data.get("code") == 0:
                            orders_list = o_data.get("data", {}).get("orders", [])
                            for ord_item in orders_list:
                                if ord_item.get("type") in ["STOP", "STOP_MARKET"]:
                                    old_order_id = ord_item.get("orderId")
                                    
                                    # Скасовуємо старий стоп
                                    del_path = "/openApi/swap/v2/trade/order"
                                    del_params = {"symbol": symbol, "orderId": str(old_order_id), "timestamp": str(int(time.time() * 1000) + server_time_offset)}
                                    del_query = urllib.parse.urlencode(sorted(del_params.items()))
                                    del_sig = get_sign(API_SECRET, del_query)
                                    del_url = f"{BINGX_BASE_URL}{del_path}?{del_query}&signature={del_sig}"
                                    async with session.delete(del_url, headers=headers) as d_resp:
                                        d_res = await d_resp.json()
                                        print(f"🗑 Скасовано старий стоп для {symbol}: {d_res}", flush=True)

                    # 2. Виставляємо новий стоп-лос на ціну входу (безубиток)
                    quantity_str = str(p.get('positionAmt', '0'))
                    pos_side = info.get('side', 'LONG')

                    success = await place_stop_loss_order(session, symbol, quantity_str, entry_price, pos_side)
                    
                    if success:
                        active_trade_monitors[symbol]['sl_moved'] = True
                        asyncio.create_task(send_telegram(session, f"🛡 TP1 досягнуто по {symbol}! Стоп перенесено в безубиток."))
                    else:
                        print(f"⚠️ Помилка встановлення стопу в безубиток для {symbol}", flush=True)

            open_symbols = [p.get("symbol") for p in open_pos]
            for monitored_sym in list(active_trade_monitors.keys()):
                if monitored_sym not in open_symbols:
                    del active_trade_monitors[monitored_sym]
                    print(f"❌ Позиція {monitored_sym} закрита, видалено з моніторингу.", flush=True)
                    asyncio.create_task(send_telegram(session, f"❌ Позиція {monitored_sym} закрита (спрацював стоп або тейк)."))

    except Exception as e:
        print(f"⚠️ Помилка у monitor_open_trades: {e}", flush=True)
        

async def send_periodic_report(session):
    while True:
        await asyncio.sleep(900)
        try:
            positions = await get_open_positions(session)
            if not positions:
                report = "📊 Періодичний звіт: наразі немає відкритих позицій."
            else:
                report = "📊 Періодичний звіт по активних позиціях:\n"
                for p in positions:
                    sym = p.get("symbol")
                    amt = p.get("positionAmt")
                    entry = p.get("avgPrice")
                    pnl = p.get("unrealizedProfit", "0")
                    status = "🛡 Безубиток" if sym in active_trade_monitors and active_trade_monitors[sym]['sl_moved'] else "⏳ В роботі"
                    report += f"• {sym} ({status}) | Об'єм: {amt} | ТВХ: {entry} | PnL: {pnl} USDT\n"
            await send_telegram(session, report)
        except Exception as e:
            print(f"⚠️ Помилка відправки періодичного звіту: {e}", flush=True)

async def execute_trade(session, symbol, entry_price, side="LONG"):
    print(f"🚀 Спроба реального відкриття позиції ({side}) по {symbol} (Ціна: {entry_price})", flush=True)
    await set_leverage(session, symbol)

    klines = await get_klines(session, symbol, interval="15m", limit=40)
    
    if side == "LONG":
        if klines and len(klines) >= 40:
            lows = [float(k['low']) for k in klines if isinstance(k, dict) and 'low' in k]
            highs = [float(k['high']) for k in klines if isinstance(k, dict) and 'high' in k]
            stop_loss_price = min(lows) * 0.997 if lows else entry_price * 0.98
            
            # Фильтр ATR для Лонга: вход только если цена близко к EMA (в пределах 1 * ATR)
            atr_val = calculate_atr(klines, period=14) if klines else 0
            closes = [float(k['close']) for k in klines if isinstance(k, dict) and 'close' in k] if klines else []
            ema_val = calculate_ema(closes, period=50) if closes else 0
        
            if ema_val and atr_val:
                distance_to_ema = abs(entry_price - ema_val)
                max_allowed = atr_val * 1.0
                if distance_to_ema > max_allowed:
                    print(f"⚠️ Лонг по {symbol} отменен: цена слишком далеко от EMA (расстояние {distance_to_ema:.4f} > {max_allowed:.4f})", flush=True)
                    return
        else:
            stop_loss_price = entry_price * 0.98

        if stop_loss_price >= entry_price:
            stop_loss_price = entry_price * 0.98

        risk = entry_price - stop_loss_price
        tp1_raw = entry_price + (risk * 1.0)
        tp1 = math.ceil(tp1_raw * 100000) / 100000
        tp2 = entry_price + (risk * 2.0)
        tp3 = entry_price + (risk * 3.0)
        order_side = "BUY"
        tp_side = "SELL"
    else: # SHORT
        if klines and len(klines) >= 40:
            highs = [float(k['high']) for k in klines if isinstance(k, dict) and 'high' in k]
            lows = [float(k['low']) for k in klines if isinstance(k, dict) and 'low' in k]
            stop_loss_price = max(highs) * 1.003 if highs else entry_price * 1.01
            
            # Фильтр ATR для Шорта: вход только если цена близко к EMA (в пределах 1 * ATR)
            atr_val = calculate_atr(klines, period=14) if klines else 0
            closes = [float(k['close']) for k in klines if isinstance(k, dict) and 'close' in k] if klines else []
            ema_val = calculate_ema(closes, period=50) if closes else 0
        
            if ema_val and atr_val:
                distance_to_ema = abs(entry_price - ema_val)
                max_allowed = atr_val * 1.0
                if distance_to_ema > max_allowed:
                    print(f"⚠️ Шорт по {symbol} отменен: цена слишком далеко от EMA (расстояние {distance_to_ema:.4f} > {max_allowed:.4f})", flush=True)
                    return
        else:
            stop_loss_price = entry_price * 1.01

        if stop_loss_price <= entry_price:
            stop_loss_price = entry_price * 1.01
        risk = stop_loss_price - entry_price
        tp1_raw = entry_price - (risk * 1.0)
        tp1 = math.floor(tp1_raw * 100000) / 100000
        tp2 = entry_price - (risk * 2.0)
        tp3 = entry_price - (risk * 3.0)
        order_side = "SELL"
        tp_side = "BUY"

    try:
        target_usd = MARGIN_USD * LEVERAGE
        total_quantity = target_usd / entry_price
        quantity_str = f"{total_quantity:.4f}"
        if float(quantity_str) == 0:
            print(f"⚠️ Занадто мала кількість для ордера {symbol}", flush=True)
            return
    except Exception as e:
        print(f"⚠️ Помилка розрахунку кількості: {e}", flush=True)
        return

    # Розподіл об'єму на частини (40% / 30% / 30%)
    total_amt = float(quantity_str)
    q1 = round(total_amt * 0.4, 4)
    q2 = round(total_amt * 0.3, 4)
    q3 = round(total_amt - q1 - q2, 4)

    path = "/openApi/swap/v2/trade/order"
    ts = str(int(time.time() * 1000) + server_time_offset)
    params = {
        "positionSide": side,
        "quantity": quantity_str,
        "side": order_side,
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
            print(f"📡 Відповідь біржі на відкриття ордера {symbol}: {res}", flush=True)
            
            if res.get("code") == 0:
                await asyncio.sleep(1.5)
                
                # 1. Встановлюємо початковий стоп-лос (передаємо side!)
                sl_success = await place_stop_loss_order(session, symbol, quantity_str, stop_loss_price, side)
                
                # 2. Встановлюємо лімітні тейк-профіти
                await place_tp_order(session, symbol, tp1, q1, side, tp_side)
                await place_tp_order(session, symbol, tp2, q2, side, tp_side)
                await place_tp_order(session, symbol, tp3, q3, side, tp_side)

                # 3. Зберігаємо моніторинг активної угоди
                active_trade_monitors[symbol] = {
                    "entry_price": entry_price,
                    "tp1": tp1,
                    "sl_moved": False
                }

                # 4. Формуємо сповіщення для Telegram
                sl_status_text = "✅ Встановлено на біржі" if sl_success else "⚠️ Помилка встановлення на біржі"
                msg = (
                    f"🟢 Успішно відкрито {side} по {symbol}!\n"
                    f"💰 Ціна входу (ТВХ): {entry_price:.5f}\n"
                    f"📦 Об'єм: {quantity_str}\n"
                    f"🛑 Стоп-лос: {stop_loss_price:.5f} ({sl_status_text})\n"
                    f"🎯 TP1 (40% | 1:1): {tp1:.5f}\n"
                    f"🎯 TP2 (30% | 1:2): {tp2:.5f}\n"
                    f"🎯 TP3 (30% | 1:3): {tp3:.5f}"
                )
                await send_telegram(session, msg)
            else:
                err_msg = res.get("msg", "Unknown error")
                msg = f"❌ Помилка відкриття {symbol}: {err_msg}"
                await send_telegram(session, msg)

    except Exception as e:
        print(f"❌ Виняток при проводці ордера для {symbol}: {e}", flush=True)


# --- Глобальна функція створення тейк-профіту (розміщується на рівні файлу поза execute_trade) ---

async def place_tp_order(session, sym, price_val, qty_val, side, tp_side):
    """Створення лімітного тейк-профіту на Бінгхекс"""
    try:
        p_path = "/openApi/swap/v2/trade/order"
        p_ts = str(int(time.time() * 1000) + server_time_offset)
        p_params = {
            "positionSide": side,
            "quantity": str(qty_val),
            "side": tp_side,
            "symbol": sym,
            "timestamp": p_ts,
            "type": "LIMIT",
            "price": f"{price_val:.5f}"
        }
        p_query = urllib.parse.urlencode(sorted(p_params.items()))
        p_sig = get_sign(API_SECRET, p_query)
        p_url = f"{BINGX_BASE_URL}{p_path}?{p_query}&signature={p_sig}"
        p_headers = {
            "X-BX-APIKEY": API_KEY,
            "Content-Type": "application/x-www-form-urlencoded"
        }
        async with session.post(p_url, headers=p_headers) as p_resp:
            p_res = await p_resp.json()
            return p_res.get("code") == 0
    except Exception as e:
        print(f"❌ Помилка створення TP для {sym}: {e}", flush=True)
        return False

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

def calculate_atr(klines, period=14):
    if len(klines) < period + 1:
        return 0
    tr_list = []
    for i in range(1, len(klines)):
        high = float(klines[i]["high"])
        low = float(klines[i]["low"])
        prev_close = float(klines[i - 1]["close"])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)
    if not tr_list:
        return 0
    recent_tr = tr_list[-period:]
    return sum(recent_tr) / len(recent_tr)

async def scan_market(session):
    print("🔄 Початок нового циклу сканування ринку...", flush=True)
    try:
        open_pos = await get_open_positions(session)
        if open_pos is None:
            open_pos = []

        risk_positions_count = 0
        for p in open_pos:
            sym = p.get("symbol")
            # Якщо позиція є в моніторингу і стоп вже перенесено (sl_moved == True), вона НЕ ризикова
            if sym in active_trade_monitors and active_trade_monitors[sym].get('sl_moved', False):
                continue
            # Всі інші вважаються ризиковими
            risk_positions_count += 1

        print(f"📌 Ризикових позицій (до TP1): {risk_positions_count} / Максимум на біржі: {MAX_RISK_POSITIONS}", flush=True)
        if risk_positions_count >= MAX_RISK_POSITIONS:
            print("🛑 Сканування зупинено: досягнуто ліміт ризикових позицій.", flush=True)
            return

        url = f"{BINGX_BASE_URL}/openApi/swap/v2/quote/ticker"
        async with session.get(url) as resp:
            if resp.status != 200:
                print(f"❌ Помилка запиту тікерів: статус {resp.status}", flush=True)
                return
            data = await resp.json()
            if not isinstance(data, dict) or data.get("code") != 0:
                print(f"⚠️ Коректна відповідь тікерів від біржі: {data}", flush=True)
                return

        tickers = data.get("data", [])
        print(f"📊 Отримано тікерів від біржі: {len(tickers)}", flush=True)
        if not isinstance(tickers, list):
            return

        scanned_count = 0
        passed_ema_count = 0
        trade_opened_in_this_cycle = False

        for ticker in tickers:
            if trade_opened_in_this_cycle:
                break
            if not isinstance(ticker, dict):
                continue

            symbol = ticker.get("symbol", "")
            if any(p.get("symbol") == symbol for p in open_pos):
                continue

            if not symbol.endswith("USDT"):
                continue

            if any(coin in symbol for coin in ["BNB", "BTC", "ETH", "SOL", "XRP", "LTC", "XAG", "XAU", "USD-USDT"]):
                continue
                
                continue

            scanned_count += 1

            try:
                current_price = float(ticker.get("lastPrice", 0))
                volume_24h = float(ticker.get("volume", 0)) * current_price
            except (ValueError, TypeError):
                continue

            if volume_24h < 300_000 or volume_24h > 30_000_000:
                continue

            # =========================
            # ПЕРЕВІРКА НА ЛОНГ (15m + 1m) - Фільтр 5-35% прибрано
            # =========================
            klines_15m = await get_klines(session, symbol, interval="15m", limit=60)
            if not klines_15m or len(klines_15m) < 55:
                continue

            try:
                clones_15m = [float(k['close']) for k in klines_15m if isinstance(k, dict) and 'close' in k]
                if len(clones_15m) >= 50:
                    ema_current_15 = calculate_ema(clones_15m, period=50)
                    ema_past_15 = calculate_ema(clones_15m[:-1], period=50)
                    
                    # Умова лонга: тренд вгору
                    if ema_current_15 > ema_past_15 and current_price > ema_current_15:
                        klines_1m = await get_klines(session, symbol, interval="1m", limit=20)
                        if klines_1m and len(klines_1m) >= 15:
                            closes_1m = [float(k['close']) for k in klines_1m if isinstance(k, dict) and 'close' in k]
                            ema_1m = calculate_ema(closes_1m, period=50)
                            if current_price > ema_1m:
                                passed_ema_count += 1
                                print(f"🔥 УСПІХ! Лонг по {symbol} (Ціна: {current_price})", flush=True)
                                await execute_trade(session, symbol, current_price, side="LONG")
                                trade_opened_in_this_cycle = True
                                await asyncio.sleep(5)
                                break
            except Exception:
                pass

            if trade_opened_in_this_cycle:
                break

            # =========================
            # ПЕРЕВІРКА НА ШОРТ (1h + 15m)
            # =========================
            klines_1h = await get_klines(session, symbol, interval="1h", limit=60)
            if klines_1h and len(klines_1h) >= 50:
                try:
                    closes_1h = [float(k['close']) for k in klines_1h if isinstance(k, dict) and 'close' in k]
                    if len(closes_1h) >= 50:
                        ema_1h_curr = calculate_ema(closes_1h, period=50)
                        ema_1h_past = calculate_ema(closes_1h[:-1], period=50)

                        # Умова шорта: годинна EMA падає і ціна нижче неї
                        if ema_1h_curr < ema_1h_past and current_price < ema_1h_curr:
                            # Підтвердження на 15m (ціна нижче 15m EMA)
                            if current_price < ema_current_15:
                                passed_ema_count += 1
                                print(f"🔥 УСПІХ! Шорт по {symbol} на 1h (Ціна: {current_price})", flush=True)
                                await execute_trade(session, symbol, current_price, side="SHORT")
                                trade_opened_in_this_cycle = True
                                await asyncio.sleep(5)
                                break
                except Exception:
                    pass

        print(f"📊 Підсумок: перевірено {scanned_count}, пройшли перевірку EMA: {passed_ema_count}", flush=True)
    except Exception as e:
        print(f"⚠️ Помилка у scan_market: {e}", flush=True)
        traceback.print_exc()
        
        
async def main():
    keep_alive()
    async with aiohttp.ClientSession() as session:
        print("🚀 Запуск головної функції бота...", flush=True)
        await sync_time(session)
        print("🤖 Бот успішно запущено, переходимо до безперервного циклу.", flush=True)
        asyncio.create_task(self_ping(session))
        asyncio.create_task(send_periodic_report(session))

        while True:
            try:
                await scan_market(session)
                await monitor_open_trades(session)
            except Exception as e:
                print(f"❌ Помилка у загальному циклі: {e}", flush=True)
            print("⏳ Очікування 60 секунд до наступного циклу...", flush=True)
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
