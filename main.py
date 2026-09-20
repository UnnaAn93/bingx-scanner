import asyncio
import aiohttp
import os
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import hmac
import hashlib
import urllib.parse

# --- НАЛАШТУВАННЯ BINGX API ---
API_KEY = "TAMQgieAMOQJuuik9XpcPa5wHch0wmXaQ8G6yBdXUlZ6G2vv3uS47QgW1NNfcI4BxLmgmeo5XwFzXEQx9dOA"
SECRET_KEY = "18EgcerNxJ5fv7TCnxQ5okPXr1VpYsJPnhYRF1GZOsikgHD2c1owRyqApKieZYQoCY2SeclmXVTdW33nIIjg"
BASE_URL = "https://open-api.bingx.com"

# Налаштування параметрів сканування
VOLUME_MULTIPLIER = 2.2           # Сплеск об'єму у 2.2 рази
APPROACH_PERCENT = 0.007          # 0.7% до рівня (підтримки або опору)
TIMEFRAME = "5m"                  # Таймфрейм 5 хвилин
LIMIT_CANDLES = 30                # Історія свічок
TOP_COINS_LIMIT = 150             # Кількість найактивніших пар
MIN_24H_VOLUME_USDT = 5_000_000   # Мінімальний добовий об'єм у USDT
COOLDOWN_SECONDS = 300            # Кулдаун 5 хвилин на одну монету
LEVERAGE = 20                     # Плече для торгівлі
RISK_DEPOSIT_PCT = 0.02           # Ризик на угоду (2%)

DISCORD_WEBHOOK_URL = os.environ.get("BINGX_API_KEY") # Або встав свою силку вебхука напряму, якщо потрібно
RENDER_URL = "https://bingx-scanner-djbf.onrender.com"

last_alert_time = {}

def get_sign(secret_key, params):
    parameters = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(secret_key.encode('utf-8'), parameters.encode('utf-8'), hashlib.sha256).hexdigest()

def get_bingx_balance():
    endpoint = "/openApi/swap/v1/user/balance"
    url = BASE_URL + endpoint
    timestamp = str(int(time.time() * 1000))
    params = {"timestamp": timestamp}
    params["signature"] = get_sign(SECRET_KEY, params)
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        response = requests_get_sync(url, headers=headers, params=params)
        if response and response.status_code == 200:
            data = response.json()
            if data.get("code") == 0:
                return float(data.get("data", {}).get("balance", {}).get("equity", 0))
    except Exception as e:
        print(f"❌ Помилка балансу: {e}")
    return 0.0

def requests_get_sync(url, headers, params):
    import requests
    try:
        return requests.get(url, headers=headers, params=params, timeout=5)
    except Exception:
        return None

def set_bingx_leverage_sync(symbol, side):
    import requests
    endpoint = "/openApi/swap/v1/trade/leverage"
    url = BASE_URL + endpoint
    timestamp = str(int(time.time() * 1000))
    params = {"symbol": symbol, "leverage": str(LEVERAGE), "side": side, "timestamp": timestamp}
    params["signature"] = get_sign(SECRET_KEY, params)
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        requests.post(url, headers=headers, params=params, timeout=5)
    except Exception as e:
        print(f"❌ Помилка плеча: {e}")

async def fetch_top_bingx_symbols(session):
    url = "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"
    try:
        async with session.get(url, timeout=5) as response:
            if response.status == 200:
                data = await response.json()
                tickers = data.get("data", [])
                
                usdt_tickers = []
                for t in tickers:
                    symbol = t.get("symbol", "")
                    if symbol.endswith("-USDT") and len(symbol) <= 12 and "USD" not in symbol[:-5]:
                        quote_vol = float(t.get("quoteVolume", 0))
                        if quote_vol >= MIN_24H_VOLUME_USDT:
                            usdt_tickers.append((symbol, quote_vol))
                
                usdt_tickers.sort(key=lambda x: x[1], reverse=True)
                top_symbols = [item[0] for item in usdt_tickers[:TOP_COINS_LIMIT]]
                return top_symbols
    except Exception as e:
        print(f"Помилка при отриманні списку монет від BingX: {e}")
    return []

async def fetch_kline_data(session, symbol):
    url = "https://open-api.bingx.com/openApi/swap/v2/quote/klines"
    params = {
        "symbol": symbol,
        "interval": TIMEFRAME,
        "limit": LIMIT_CANDLES
    }
    try:
        async with session.get(url, params=params, timeout=4) as response:
            if response.status == 200:
                data = await response.json()
                return data.get("data", [])
    except Exception:
        pass
    return None

async def send_to_discord(session, webhook_url, message):
    if not webhook_url:
        return
    payload = {"content": message}
    try:
        async with session.post(webhook_url, json=payload) as response:
            if response.status != 200:
                print(f"Помилка відправки у Discord: {response.status}")
    except Exception as e:
        print(f"Виняток при відправці у Discord: {e}")

async def send_two_step_alert(session, webhook_url, symbol, signal_type, alert_message, lows, highs, closes, current_price, surge_percent):
    if not webhook_url:
        return

    # Етап 1: Сповіщення про виявлений сигнал
    step1_msg = (
        f"🎯 **СИГНАЛ [{signal_type} / 5m]**: `{symbol}`\n"
        f"• Ціна входу / поточна: `{current_price}`\n"
        f"• Об'єм свічки: `+{surge_percent}%` від середнього!\n"
        f"• Статус: Перевірка балансу та відкриття позиції на біржі..."
    )
    await send_to_discord(session, webhook_url, step1_msg)

    # Виконання торгової логіки на біржі в окремому потоці або синхронно
    loop = asyncio.get_event_loop()
    balance = await loop.run_in_executor(None, get_bingx_balance)

    if balance <= 0:
        err_msg = f"❌ **ПОМИЛКА по {symbol}**: Нульовий баланс або не вдалося отримати дані акаунта!"
        await send_to_discord(session, webhook_url, err_msg)
        return

    side = "BUY" if "ЛОНГ" in signal_type else "SELL"
    position_side = "LONG" if side == "BUY" else "SHORT"

    await loop.run_in_executor(None, set_bingx_leverage_sync, symbol, position_side)

    margin_to_use = balance * RISK_DEPOSIT_PCT
    position_notional = margin_to_use * LEVERAGE
    quantity = round(position_notional / current_price, 3)
    if quantity <= 0:
        quantity = 1

    # Розрахунок SL та TP
    if side == "BUY":
        stop_loss = min(lows[:-1]) * 0.998
        take_profit = current_price + ((current_price - stop_loss) * 2.5)
    else:
        stop_loss = max(highs[:-1]) * 1.002
        take_profit = current_price - ((stop_loss - current_price) * 2.5)

    stop_loss = round(stop_loss, 4)
    take_profit = round(take_profit, 4)

    endpoint = "/openApi/swap/v2/trade/order"
    url = BASE_URL + endpoint
    timestamp = str(int(time.time() * 1000))
    params = {
        "symbol": symbol, 
        "side": side, 
        "positionSide": position_side,
        "type": "MARKET", 
        "quantity": str(quantity),
        "stopLoss": str(stop_loss), 
        "takeProfit": str(take_profit),
        "timestamp": timestamp
    }
    params["signature"] = get_sign(SECRET_KEY, params)
    headers = {"X-BX-APIKEY": API_KEY}

    import requests
    try:
        response = await loop.run_in_executor(
            None, 
            lambda: requests.post(url, headers=headers, params=params, timeout=5)
        )
        if response and response.status_code == 200:
            data = response.json()
            if data.get("code") == 0:
                # Етап 2: Успішне відкриття позиції
                success_msg = (
                    f"✅ **ПОЗИЦІЮ ВІДКРИТО [{signal_type}]**:\n"
                    f"• Пара: `{symbol}`\n"
                    f"• Плече: `{LEVERAGE}x` | Об'єм: `{quantity}`\n"
                    f"• Вхід: `{current_price}` | SL: `{stop_loss}` | TP: `{take_profit}`"
                )
                await send_to_discord(session, webhook_url, success_msg)
            else:
                fail_msg = f"⚠️ **ВІДМОВА БІРЖІ по {symbol}**:\n• Відповідь API: `{data}`"
                await send_to_discord(session, webhook_url, fail_msg)
        else:
            await send_to_discord(session, webhook_url, f"❌ **ПОМИЛКА МЕРЕЖІ при замовленні {symbol}**")
    except Exception as e:
        await send_to_discord(session, webhook_url, f"❌ **ВИНЯТОК ПРИ ЗАМОВЛЕННІ {symbol}**: `{e}`")

async def check_single_coin(session, symbol, discord_webhook_url):
    current_time = time.time()
    if symbol in last_alert_time and current_time - last_alert_time[symbol] < COOLDOWN_SECONDS:
        return

    kline_data = await fetch_kline_data(session, symbol)
    
    if not kline_data or len(kline_data) < 20:
        return

    try:
        volumes = [float(x['volume']) for x in kline_data]
        lows = [float(x['low']) for x in kline_data]
        highs = [float(x['high']) for x in kline_data]
        closes = [float(x['close']) for x in kline_data]
        
        current_volume = volumes[-1]
        if current_volume <= 0:
            return

        avg_volume = sum(volumes[:-1]) / (len(volumes) - 1)
        if avg_volume <= 0:
            return
        
        current_price = closes[-1]
        is_volume_spike = current_volume >= (avg_volume * VOLUME_MULTIPLIER)
        
        if not is_volume_spike:
            return

        surge_percent = int((current_volume / avg_volume - 1) * 100)

        # 1. Перевірка на ЛОНГ (Підтримка знизу)
        support_level = min(lows[:-1])
        if support_level > 0:
            distance_to_support = (current_price - support_level) / support_level
            if 0 <= distance_to_support <= APPROACH_PERCENT:
                last_alert_time[symbol] = current_time
                await send_two_step_alert(
                    session, discord_webhook_url, symbol, "ЛОНГ / Підтримка 5m",
                    "", lows, highs, closes, current_price, surge_percent
                )
                print(f"Лонг сигнал 5m для {symbol}")
                return

        # 2. Перевірка на ШОРТ (Опір зверху)
        resistance_level = max(highs[:-1])
        if resistance_level > 0:
            distance_to_resistance = (resistance_level - current_price) / resistance_level
            if 0 <= distance_to_resistance <= APPROACH_PERCENT:
                last_alert_time[symbol] = current_time
                await send_two_step_alert(
                    session, discord_webhook_url, symbol, "ШОРТ / Опір 5m",
                    "", lows, highs, closes, current_price, surge_percent
                )
                print(f"Шорт сигнал 5m для {symbol}")
                return

    except Exception as e:
        pass

async def self_ping_loop(session):
    while True:
        await asyncio.sleep(240)
        try:
            async with session.get(RENDER_URL, timeout=5) as response:
                pass
        except Exception:
            pass

async def main():
    print("Бот сканує ринок на ЛОНГ (підтримка) та ШОРТ (опір) на 5m із двоступеневими сповіщеннями...")
    
    async with aiohttp.ClientSession() as session:
        asyncio.create_task(self_ping_loop(session))
        
        while True:
            start_time = asyncio.get_event_loop().time()
            
            symbols_list = await fetch_top_bingx_symbols(session)
            
            if symbols_list and DISCORD_WEBHOOK_URL:
                tasks = [check_single_coin(session, symbol, DISCORD_WEBHOOK_URL) for symbol in symbols_list]
                await asyncio.gather(*tasks)
            
            elapsed = asyncio.get_event_loop().time() - start_time
            sleep_time = max(1, 15 - elapsed)
            await asyncio.sleep(sleep_time)

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"BingX Long/Short Scanner Bot is running!")
    
    def log_message(self, format, *args):
        return

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот зупинений користувачем.")
                           
