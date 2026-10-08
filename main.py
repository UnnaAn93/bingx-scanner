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
import websockets

# --- КОНСТАНТИ ТА НАЛАШТУВАННЯ ---
API_KEY = os.environ.get("BINGX_API_KEY", "")
API_SECRET = os.environ.get("BINGX_SECRET_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
RENDER_URL = os.environ.get("RENDER_URL", "")
BINGX_BASE_URL = "https://open-api.bingx.com"

LEVERAGE = 10
MARGIN_USD = 0.5
MAX_RISK_POSITIONS = 2
server_time_offset = 0

active_trade_monitors = {}
coin_cooldowns = {}  # {symbol: timestamp паузи після помилки}

# --- KEEP-ALIVE SERVER (для Render) ---
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive and running")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
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

# --- ДОПОМІЖНІ ФУНКЦІЇ ПІДПИСУ ТА TELEGRAM ---
def get_sign(secret_key: str, payload: str) -> str:
    return hmac.new(secret_key.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()

async def send_telegram(session, message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        async with session.post(url, json=payload) as resp:
            await resp.text()
    except Exception as e:
        print(f"⚠️ Помилка надсилання в Telegram: {e}", flush=True)

# --- СИНХРОНІЗАЦІЯ ЧАСУ ---
async def sync_server_time(session):
    global server_time_offset
    try:
        url = f"{BINGX_BASE_URL}/openApi/swap/v2/server/time"
        async with session.get(url) as resp:
            data = await resp.json()
            if data.get("code") == 0:
                server_time = data.get("data", {}).get("serverTime", int(time.time() * 1000))
                local_time = int(time.time() * 1000)
                server_time_offset = server_time - local_time
                print(f"🕒 Час синхронізовано. Зміщення: {server_time_offset} мс", flush=True)
    except Exception as e:
        print(f"⚠️ Помилка синхронізації часу: {e}", flush=True)

# --- ІНДИКАТОРИ ТА КЛІЙНИ ---
async def get_klines(session, symbol, interval="15m", limit=60):
    url = f"{BINGX_BASE_URL}/openApi/swap/v2/quote/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        async with session.get(url) as resp:
            data = await resp.json()
            if data.get("code") == 0:
                return data.get("data", [])
    except Exception as e:
        print(f"⚠️ Помилка отримання свічок для {symbol}: {e}", flush=True)
    return []

def calculate_ema(closes, period=50):
    if not closes or len(closes) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def calculate_atr(klines, period=14):
    if not klines or len(klines) < period + 1:
        return 0
    tr_list = []
    for i in range(1, len(klines)):
        high = float(klines[i]['high'])
        low = float(klines[i]['low'])
        prev_close = float(klines[i-1]['close'])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)
    if not tr_list:
        return 0
    recent_tr = tr_list[-period:]
    return sum(recent_tr) / len(recent_tr)

# --- РОБОТА З БІРЖЕЮ (ПЛЕЧЕ ТА ОРДЕРИ) ---
async def set_leverage(session, symbol, leverage, side):
    path = "/openApi/swap/v2/trade/leverage"
    ts = str(int(time.time() * 1000) + server_time_offset)
    params = {"symbol": symbol, "leverage": LEVERAGE, "side": side, "timestamp": ts}
    query_str = urllib.parse.urlencode(sorted(params.items()))
    sig = get_sign(API_SECRET, query_str)
    url = f"{BINGX_BASE_URL}{path}?{query_str}&signature={sig}"
    headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    try:
        async with session.post(url, headers=headers) as resp:
            res = await resp.json()
            if res.get("code") != 0:
                return False, res.get("msg", "Unknown error")
            return True, ""
    except Exception as e:
        return False, str(e)

async def place_stop_loss_order(session, symbol, quantity_str, stop_price, pos_side):
    path = "/openApi/swap/v2/trade/order"
    ts = str(int(time.time() * 1000) + server_time_offset)
    stop_side = "SELL" if pos_side == "LONG" else "BUY"
    
    params = {
        "positionSide": pos_side,
        "quantity": quantity_str,
        "side": stop_side,
        "symbol": symbol,
        "timestamp": ts,
        "type": "STOP_MARKET",
        "stopPrice": f"{stop_price:.5f}",
        "workingType": "MARK_PRICE",
        #"reduceOnly": "true"  # Гарантує закриття позиції в межах її реального розміру
    }
    
    query_str = urllib.parse.urlencode(sorted(params.items()))
    sig = get_sign(API_SECRET, query_str)
    url = f"{BINGX_BASE_URL}{path}?{query_str}&signature={sig}"
    headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    
    try:
        async with session.post(url, headers=headers) as resp:
            res = await resp.json()
            if res.get("code") != 0:
                return False, res.get("msg", "Unknown error")
            return True, ""
    except Exception as e:
        return False, str(e)

async def place_tp_order(session, sym, price_val, qty_val, side):
    path = "/openApi/swap/v2/trade/order"
    ts = str(int(time.time() * 1000) + server_time_offset)
    params = {
        "positionSide": side,
        "quantity": str(qty_val),
        "side": "SELL" if side == "LONG" else "BUY",
        "symbol": sym,
        "timestamp": ts,
        "type": "LIMIT",
        "price": f"{price_val:.5f}"
    }
    query_str = urllib.parse.urlencode(sorted(params.items()))
    sig = get_sign(API_SECRET, query_str)
    url = f"{BINGX_BASE_URL}{path}?{query_str}&signature={sig}"
    headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    try:
        async with session.post(url, headers=headers) as resp:
            res = await resp.json()
            if res.get("code") != 0:
                return False, res.get("msg", "Unknown error")
            return True, ""
    except Exception as e:
        return False, str(e)

async def get_open_positions(session):
    path = "/openApi/swap/v2/user/positions"
    ts = str(int(time.time() * 1000) + server_time_offset)
    params = {"timestamp": ts}
    query_str = urllib.parse.urlencode(sorted(params.items()))
    sig = get_sign(API_SECRET, query_str)
    url = f"{BINGX_BASE_URL}{path}?{query_str}&signature={sig}"
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            if data.get("code") == 0:
                return [p for p in data.get("data", []) if float(p.get("positionAmt", 0)) != 0]
    except Exception as e:
        print(f"⚠️ Помилка отримання позицій: {e}", flush=True)
    return []

# --- ВИКОНАННЯ УГОДИ ---
async def execute_trade(session, symbol, entry_price, side="LONG"):
    print(f"🚀 Спроба відкрити позицію ({side}) по {symbol} за ціною {entry_price}", flush=True)

    # Встановлюємо плече із урахуванням Hedge mode (side)
    lev_success, lev_err = await set_leverage(session, symbol, LEVERAGE, side)
    if not lev_success:
        err_msg = f"❌ Помилка встановлення плеча по {symbol} ({side}): {lev_err}"
        print(err_msg, flush=True)
        asyncio.create_task(send_telegram(session, err_msg))
        return False

    klines = await get_klines(session, symbol, interval="15m", limit=40)

    if side == "LONG":
        lows = [float(k['low']) for k in klines if isinstance(k, dict) and 'low' in k]
        min_low = min(lows) if lows else entry_price * 0.98
        stop_loss_price = min_low * 0.997  # Найнижча свічка мінус 0.3%
        risk = entry_price - stop_loss_price
        
        tp1 = math.ceil((entry_price + risk * 1.0) * 100000) / 100000  # Заокруглення в більшу сторону
        tp2 = entry_price + risk * 2.0
        tp3 = entry_price + risk * 3.0
        order_side = "BUY"
        tp_side = "LONG"
    else:
        highs = [float(k['high']) for k in klines if isinstance(k, dict) and 'high' in k]
        max_high = max(highs) if highs else entry_price * 1.02
        stop_loss_price = max_high * 1.003  # Найвища свічка плюс 0.3%
        risk = stop_loss_price - entry_price
        
        tp1 = math.ceil((entry_price - risk * 1.0) * 100000) / 100000  # Заокруглення в більшу сторону
        tp2 = entry_price - risk * 2.0
        tp3 = entry_price - risk * 3.0
        order_side = "SELL"
        tp_side = "SHORT"

    try:
        target_usd = MARGIN_USD * LEVERAGE
        total_quantity = target_usd / entry_price
        quantity_str = f"{total_quantity:.4f}"
        if float(quantity_str) == 0:
            raise ValueError("Розрахований об'єм позиції дорівнює 0")
    except Exception as e:
        err_msg = f"❌ Помилка об'єму по {symbol}: {e}"
        print(err_msg, flush=True)
        asyncio.create_task(send_telegram(session, err_msg))
        return False

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
    headers = {"X-BX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}

    try:
        async with session.post(url, headers=headers) as resp:
            res = await resp.json()
            if res.get("code") != 0:
                err_msg = f"❌ Помилка маркет-ордера по {symbol}: {res.get('msg', 'Unknown')}"
                print(err_msg, flush=True)
                asyncio.create_task(send_telegram(session, err_msg))
                return False

        # Чекаємо трохи і запитуємо реальну ціну входу (avgPrice) з біржі, щоб уникнути прослизання
        await asyncio.sleep(1.5)
        open_positions = await get_open_positions(session)
        real_entry_price = entry_price
        
        for p in open_positions:
            if p.get("symbol") == symbol and p.get("positionSide") == side:
                real_entry_price = float(p.get("avgPrice", entry_price))
                break

        # Перераховуємо стоп і тейки від РЕАЛЬНОЇ ціни входу
        if side == "LONG":
            stop_loss_price = min_low - 0.003
            if risk <= 0:
                stop_loss_price = real_entry_price * 0.98
            tp1 = math.ceil((real_entry_price + risk * 1.0) * 100000) / 100000
            tp2 = real_entry_price + risk * 2.0
            tp3 = real_entry_price + risk * 3.0
        else:
            stop_loss_price = max_high + 1.003
            if risk <= 0:
                stop_loss_price = real_entry_price * 1.02
            tp1 = math.ceil((real_entry_price - risk * 1.0) * 100000) / 100000
            tp2 = real_entry_price - risk * 2.0
            tp3 = real_entry_price - risk * 3.0

        await asyncio.sleep(0.5)
        ok_sl, sl_err = await place_stop_loss_order(session, symbol, quantity_str, stop_loss_price, side)
        if not ok_sl:
            err_msg = f"⚠️ Позицію відкрито (за ціною {real_entry_price}), але не вдалося поставити Стоп-Лосс по {symbol}: {sl_err}"
            print(err_msg, flush=True)
            asyncio.create_task(send_telegram(session, err_msg))

        await place_tp_order(session, symbol, tp1, q1, tp_side)
        await place_tp_order(session, symbol, tp2, q2, tp_side)
        await place_tp_order(session, symbol, tp3, q3, tp_side)

        active_trade_monitors[symbol] = {
            'entry_price': real_entry_price,
            'tp': [tp1, tp2, tp3],
            'side': side,
            'sl_moved': False,
            'quantity_str': quantity_str
        }

        success_msg = f"🟢 Успішно відкрито {side} по {symbol}\nЦіна входу: {real_entry_price:.5f}\nСтоп: {stop_loss_price:.5f}\nТП1: {tp1:.5f}"
        print(success_msg, flush=True)
        asyncio.create_task(send_telegram(session, success_msg))
        return True

    except Exception as e:
        err_msg = f"❌ Критична помилка виконання угоди по {symbol}: {e}"
        print(err_msg, flush=True)
        asyncio.create_task(send_telegram(session, err_msg))
        return False

async def restore_active_monitors_from_exchange(session):
    """Відновлює active_trade_monitors з біржі після перезапуску бота (наприклад, після рестарту Render)"""
    global active_trade_monitors
    print("🔄 Відновлення стану активних позицій з біржі...", flush=True)
    try:
        open_pos = await get_open_positions(session)
        if not open_pos:
            return

        for p in open_pos:
            symbol = p.get("symbol")
            side = p.get("positionSide")
            avg_price = float(p.get("avgPrice", 0))
            
            orders_path = "/openApi/swap/v2/trade/openOrders"
            ts_ord = str(int(time.time() * 1000) + server_time_offset)
            o_params = {"symbol": symbol, "timestamp": ts_ord}
            o_query = urllib.parse.urlencode(sorted(o_params.items()))
            o_sig = get_sign(API_SECRET, o_query)
            o_url = f"{BINGX_BASE_URL}{orders_path}?{o_query}&signature={o_sig}"
            
            async with session.get(o_url, headers={"X-BX-APIKEY": API_KEY}) as o_resp:
                o_data = await o_resp.json()
                if o_data.get("code") == 0:
                    raw_orders = o_data.get("data", [])
                    orders_list = raw_orders.get("orders", []) if isinstance(raw_orders, dict) else raw_orders
                    
                    tp_prices = [float(o.get("price")) for o in orders_list if o.get("type") == "TAKE_PROFIT" or "TP" in o.get("type", "")]
                    
                    sl_moved = False
                    for ord_item in orders_list:
                        if ord_item.get("type") in ["STOP", "STOP_MARKET"]:
                            stop_price = float(ord_item.get("stopPrice", ord_item.get("price", 0)))
                            # Якщо це LONG і стоп вище або дорівнює входу
                            if side == "LONG" and stop_price >= avg_price:
                                sl_moved = True
                            # Якщо це SHORT і стоп нижче або дорівнює входу
                            elif side == "SHORT" and stop_price <= avg_price:
                                sl_moved = True

                    active_trade_monitors[symbol] = {
                        'entry_price': avg_price,
                        'tp': sorted(tp_prices) if tp_prices else [avg_price * 1.02, avg_price * 1.04, avg_price * 1.06],
                        'side': side,
                        'sl_moved': sl_moved,
                        'quantity_str': str(p.get("positionAmt", "0"))
                    }
                    print(f"✅ Відновлено моніторинг для {symbol} ({side}), SL в БУ: {sl_moved}", flush=True)
    except Exception as e:
        print(f"⚠️ Помилка відновлення позицій з біржі: {e}", flush=True)

# --- WEBSOCKET ПРОСЛУХОВУВАННЯ ВІДПОВІДЕЙ БІРЖІ ---
async def bingx_websocket_listener(session):
    listen_key_path = "/openApi/swap/v2/user/listenKey"
    while True:
        try:
            ts = str(int(time.time() * 1000) + server_time_offset)
            params = {"timestamp": ts}
            query_str = urllib.parse.urlencode(sorted(params.items()))
            sig = get_sign(API_SECRET, query_str)
            url = f"{BINGX_BASE_URL}{listen_key_path}?{query_str}&signature={sig}"
            headers = {"X-BX-APIKEY": API_KEY}

            listen_key = None
            async with session.post(url, headers=headers) as resp:
                data = await resp.json()
                if data.get("code") == 0:
                    listen_key = data.get("data", {}).get("listenKey")

            if not listen_key:
                await asyncio.sleep(10)
                continue

            ws_url = f"wss://open-api-swap.bingx.com/swap-market?listenKey={listen_key}"
            async with websockets.connect(ws_url) as websocket:
                print("🔌 WebSocket підключено до BingX для відстеження тейків", flush=True)
                while True:
                    message = await websocket.recv()
                    if "ORDER_TRADE_UPDATE" in message or "order" in message:
                        try:
                            open_pos = await get_open_positions(session)
                            open_symbols = [p.get("symbol") for p in open_pos]

                            for symbol, info in list(active_trade_monitors.items()):
                                if not info.get('sl_moved', False):
                                    orders_path = "/openApi/swap/v2/trade/openOrders"
                                    ts_ord = str(int(time.time() * 1000) + server_time_offset)
                                    o_params = {"symbol": symbol, "timestamp": ts_ord}
                                    o_query = urllib.parse.urlencode(sorted(o_params.items()))
                                    o_sig = get_sign(API_SECRET, o_query)
                                    o_url = f"{BINGX_BASE_URL}{orders_path}?{o_query}&signature={o_sig}"
                                    
                                    async with session.get(o_url, headers={"X-BX-APIKEY": API_KEY}) as o_resp:
                                        o_data = await o_resp.json()
                                        if o_data.get("code") == 0:
                                            raw_orders = o_data.get("data", [])
                                            orders_list = raw_orders.get("orders", []) if isinstance(raw_orders, dict) else raw_orders
                                            
                                            tp_list = info.get('tp', [])
                                            tp1_target = tp_list[0] if tp_list else None
                                            
                                            tp_still_active = False
                                            if tp1_target is not None:
                                                tp_still_active = any(abs(float(o.get("price", 0)) - tp1_target) < 0.00001 for o in orders_list)

                                            if tp1_target is not None and not tp_still_active:
                                                old_stop_id = None
                                                for ord_item in orders_list:
                                                    if ord_item.get("type") in ["STOP", "STOP_MARKET"]:
                                                        old_stop_id = ord_item.get("orderId")

                                                if old_stop_id:
                                                    del_path = "/openApi/swap/v2/trade/order"
                                                    del_params = {"symbol": symbol, "orderId": str(old_stop_id), "timestamp": str(int(time.time() * 1000) + server_time_offset)}
                                                    del_query = urllib.parse.urlencode(sorted(del_params.items()))
                                                    del_sig = get_sign(API_SECRET, del_query)
                                                    del_uri = f"{BINGX_BASE_URL}{del_path}?{del_query}&signature={del_sig}"
                                                    async with session.delete(del_uri, headers={"X-BX-APIKEY": API_KEY}) as d_resp:
                                                        await d_resp.json()

                                                success, sl_err = await place_stop_loss_order(session, symbol, info.get('quantity_str'), info.get('entry_price'), info.get('side'))
                                                if success:
                                                    active_trade_monitors[symbol]['sl_moved'] = True
                                                    asyncio.create_task(send_telegram(session, f"🛡️ ТР1 досягнуто по {symbol} (підтверджено через WebSocket)! Стоп перенесено в безубиток (ТВХ)."))
                                                else:
                                                    asyncio.create_task(send_telegram(session, f"⚠️ ТР1 досягнуто по {symbol}, але помилка перенесення в БУ: {sl_err}"))

                                if symbol not in open_symbols:
                                    del active_trade_monitors[symbol]
                                    asyncio.create_task(send_telegram(session, f"❌ Позиція {symbol} повністю закрита, видалено зі звіту."))

                        except Exception as inner_e:
                            print(f"⚠️ Помилка обробки WebSocket повідомлення: {inner_e}", flush=True)

        except Exception as e:
                            print(f"⚠️ Помилка у WebSocket з'єднанні: {e}, перепідключення через 5 секунд...", flush=True)
                            await asyncio.sleep(5)

# --- ПЕРІОДИЧНИЙ ЗВІТ КОЖНІ 15 ХВИЛИН ---
async def send_periodic_report(session):
    while True:
        try:
            positions = await get_open_positions(session)
            if not positions:
                report = "📊 Періодичний звіт: наразі немає відкритих позицій."
            else:
                report = "📊 Періодичний звіт по активних позиціях:\n"
                for p in positions:
                    sym = p.get("symbol")
                    amt = p.get("positionNet")
                    entry = p.get("avgPrice")
                    pnl = p.get("unrealizedProfit", "0")
                    status = "🛡 Б/У" if sym in active_trade_monitors and active_trade_monitors[sym].get('sl_moved') else "⏳ Ризик"
                    report += f"🔷 {sym} ({status}) | Об'єм: {amt} | ТВХ: {entry} | PnL: {pnl} USDT\n"
            asyncio.create_task(send_telegram(session, report))
        except Exception as e:
            print(f"⚠️ Помилка відправки періодичного звіту: {e}")
        
        await asyncio.sleep(900)

# --- СКАНУВАННЯ РИНКУ ---
async def scan_market(session):
    print("🔍 Початок нового циклу сканування ринку...", flush=True)
    try:
        open_pos = await get_open_positions(session)
        if open_pos is None:
            open_pos = []

        # Надійний підрахунок кількості позицій з ризиком
        risk_positions_count = 0
        for p in open_pos:
            sym = p.get("symbol")
            pos_side = p.get("positionSide")
            
            is_sl_in_be = False
            if sym in active_trade_monitors and active_trade_monitors[sym].get('side') == pos_side:
                is_sl_in_be = active_trade_monitors[sym].get('sl_moved', False)
            
            if not is_sl_in_be:
                risk_positions_count += 1

        if risk_positions_count >= MAX_RISK_POSITIONS:
            print(f"🛑 Зупинка сканування: вже є {risk_positions_count} позиції з ризиком (ліміт: {MAX_RISK_POSITIONS})", flush=True)
            return

        url = f"{BINGX_BASE_URL}/openApi/swap/v2/quote/ticker"
        async with session.get(url) as resp:
            if resp.status != 200:
                return
            data = await resp.json()
            if data.get("code") != 0:
                return
            tickers = data.get("data", [])

        trade_opened_this_cycle = False
        current_time_ts = time.time()

        for ticker in tickers:
            if trade_opened_this_cycle:
                break
            if not isinstance(ticker, dict):
                continue

            symbol = ticker.get("symbol", "")

            if any(p.get("symbol") == symbol for p in open_pos):
                continue
            if not symbol.endswith("USDT"):
                continue

            if symbol in coin_cooldowns:
                if current_time_ts < coin_cooldowns[symbol]:
                    continue
                else:
                    del coin_cooldowns[symbol]

            excluded_substrings = ["BTC", "LTC", "XCP", "NCS", "USD-USDT", "BNB", "ETH", "SOL", "XRP", "NCFX"]
            if any(sub in symbol for sub in excluded_substrings):
                continue

            try:
                volume_24h = float(ticker.get("volume", 0)) * float(ticker.get("lastPrice", 0))
            except (ValueError, TypeError):
                continue

            if volume_24h < 300_000 or volume_24h > 30_000_000:
                continue

            # Отримуємо 1h свічки
            klines_th = await get_klines(session, symbol, interval="1h", limit=60)
            if not klines_th or len(klines_th) < 50:
                continue

            closes_th = [float(k['close']) for k in klines_th if isinstance(k, dict)]
            ema_th_curr = calculate_ema(closes_th[-1], period=50)
            ema_th_past = calculate_ema(closes_th[-2], period=50)
            atr_th = calculate_atr(klines_th, period=14)

            if not ema_th_curr or not ema_th_past:
                continue

            candle_th = klines_th[-2]
            o_th = float(candle_th['open'])
            c_th = float(candle_th['close'])

            # Отримуємо 15m свічки
            klines_15m = await get_klines(session, symbol, interval="15m", limit=60)
            if not klines_15m or len(klines_15m) < 50:
                continue

            closes_15m = [float(k['close']) for k in klines_15m if isinstance(k, dict)]
            ema_current_15 = calculate_ema(closes_15m[-1], period=50)
            ema_past_15 = calculate_ema(closes_15m[-2], period=50)
            atr_15m = calculate_atr(klines_15m, period=14)

            if not ema_current_15 or not ema_past_15 or not atr_15m:
                continue

            candle_15m = klines_15m[-2]
            o_15m = float(candle_15m['open'])
            c_15m = float(candle_15m['close'])
            current_price = float(ticker.get("lastPrice", c_15m))

            # Динамічна зона біля EMA через ATR (0.5 від ATR)
            ema_zone = atr_15m * 0.5

            # Перевірка об'ємної аномалії
            recent_candle = klines_15m[-2]
            vol_list = [float(k['volume']) for k in klines_15m[-15:-2]]
            avg_vol = sum(vol_list) / len(vol_list) if vol_list else 1.0
            current_vol = float(recent_candle['volume'])
            is_volume_spike = current_vol > (avg_vol * 1.8)

            c_high = float(recent_candle['high'])
            c_low = float(recent_candle['low'])
            c_open = float(recent_candle['open'])
            c_close = float(recent_candle['close'])

            # Умови поглинання / відбиття від рівня біля EMA на 15m
            long_absorption = (
                is_volume_spike and
                abs(c_close - ema_current_15) <= ema_zone and
                (min(c_open, c_close) - c_low) > (c_high - max(c_open, c_close))
            )

            short_absorption = (
                is_volume_spike and
                abs(c_close - ema_current_15) <= ema_zone and
                (c_high - max(c_open, c_close)) > (min(c_open, c_close) - c_low)
            )

            # Головні умови входу
            long_condition = (
                (ema_th_curr > ema_th_past and ema_current_15 > ema_past_15 and min(c_15m, o_15m) > ema_current_15)
                or
                (ema_th_curr > ema_th_past and long_absorption)
            )

            short_condition = (
                (ema_th_curr < ema_th_past and ema_current_15 < ema_past_15 and max(c_15m, o_15m) < ema_current_15)
                or
                (ema_th_curr < ema_th_past and short_absorption)
            )

            if long_condition:
                success = await execute_trade(session, symbol, current_price, side="BUY")
                if success:
                    trade_opened_this_cycle = True
                    break
                else:
                    coin_cooldowns[symbol] = time.time() + 300

            elif short_condition:
                success = await execute_trade(session, symbol, current_price, side="SELL")
                if success:
                    trade_opened_this_cycle = True
                    break
                else:
                    coin_cooldowns[symbol] = time.time() + 300

    except Exception as e:
        print(f"⚠️ Помилка у scan_market: {e}", flush=True)
        traceback.print_exc()
        
        

async def market_scanner_loop(session):
    while True:
        try:
            # Даємо на весь цикл сканування максимум 50 секунд. 
            # Якщо якесь зависання — воно обірветься і цикл піде далі.
            await asyncio.wait_for(scan_market(session), timeout=50.0)
        except asyncio.TimeoutError:
            print("⚠️ Увага: Час виконання циклу сканування перевищив 50 секунд (можливо, зависання мережі). Продовжуємо...", flush=True)
        except Exception as e:
            print(f"⚠️ Помилка в циклі сканування: {e}", flush=True)
            
        await asyncio.sleep(60)

# --- ГОЛОВНА ТОЧКА ВХОДУ ---
async def main():
    keep_alive()
    async with aiohttp.ClientSession() as session:
        await sync_server_time(session)
        asyncio.create_task(self_ping(session))
        asyncio.create_task(bingx_websocket_listener(session))
        asyncio.create_task(send_periodic_report(session))

        await restore_active_monitors_from_exchange(session)
        
        print("🚀 Бот запущено за повною логікою!", flush=True)
        await market_scanner_loop(session)

if __name__ == "__main__":
    asyncio.run(main())
