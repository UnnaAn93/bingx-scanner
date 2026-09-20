import asyncio
import aiohttp
import os
import time
import hmac
import hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import json

# --- НАЛАШТУВАННЯ ТОРГІВЛІ ТА СКАНУВАННЯ ---
VOLUME_MULTIPLIER = 2.2           # Сплеск об'єму у 2.2 рази
APPROACH_PERCENT = 0.007          # 0.7% до рівня (підтримки або опору)
TIMEFRAME = "5"                   # Таймфрейм 5 хвилин
LIMIT_CANDLES = 30                # Історія свічок
TOP_COINS_LIMIT = 50              # Кількість пар для сканування
MIN_24H_VOLUME_USDT = 100_000     # Мінімальний об'єм
COOLDOWN_SECONDS = 300            # Кулдаун 5 хвилин на одну монету

# Торгові параметри
LEVERAGE = 10                     # Плече
TRADE_USDT_AMOUNT = 10.0          # Сума ордера в USDT

# Ключі Binance Demo, які ти щойно згенерувала
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "ТВОЙ_API_KEY")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "ТВОЙ_SECRET_KEY")

# Демо-ендпоінт ф'ючерсів Binance
BINANCE_BASE_URL = "https://testnet.binancefuture.com"

DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1551243480989433927/cIcSwvFUvrh7vnRbXcCx9c8pRMBkyX6VBNVbsEQBp6PWc7aJmXZsYnLnU79Rh8JJaKMF"
RENDER_URL = "https://bingx-scanner-djbf.onrender.com"

last_alert_time = {}
open_positions = {}  # {symbol: "LONG" або "SHORT"}

# --- ФУНКЦІЇ ПІДПИСУ ТА ЗАПИТІВ ДО BINANCE API ---
def get_binance_signature(secret, query_string):
    return hmac.new(secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()

async def binance_request(session, method, path, params=None):
    if params is None:
        params = {}
    
    params["timestamp"] = int(time.time() * 1000)
    query_string = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
    signature = get_binance_signature(BINANCE_SECRET_KEY, query_string)
    query_string += f"&signature={signature}"
    
    url = f"{BINANCE_BASE_URL}{path}?{query_string}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    
    try:
        if method == "GET":
            async with session.get(url, headers=headers, timeout=5) as response:
                return await response.json()
        elif method == "POST":
            async with session.post(url, headers=headers, timeout=5) as response:
                return await response.json()
        elif method == "DELETE":
            async with session.delete(url, headers=headers, timeout=5) as response:
                return await response.json()
    except Exception as e:
        print(f"Помилка запиту до Binance API: {e}")
    return None

async def set_leverage(session, symbol):
    path = "/fapi/v1/leverage"
    params = {"symbol": symbol, "leverage": LEVERAGE}
    await binance_request(session, "POST", path, params)

async def open_market_order(session, symbol, side):
    """
    side: 'BUY' (Лонг) або 'SELL' (Шорт)
    """
    await set_leverage(session, symbol)
    
    # Отримуємо ціну для розрахунку кількості
    ticker_url = f"{BINANCE_BASE_URL}/fapi/v1/ticker/price?symbol={symbol}"
    try:
        async with session.get(ticker_url) as resp:
            t_data = await resp.json()
            current_price = float(t_data['price'])
    except Exception:
        return

    notional_size = TRADE_USDT_AMOUNT * LEVERAGE
    qty = notional_size / current_price
    
    # Округлення під правила пари (тут можна залишити стандартне або спростити)
    qty = round(qty, 2)
    if qty <= 0:
        qty = 0.01

    path = "/fapi/v1/order"
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty
    }
    
    res = await binance_request(session, "POST", path, params)
    if res and "orderId" in res:
        print(f"Binance Demo: Успішно відкрито ордер [{side}] для {symbol}")
    else:
        print(f"Binance Demo Помилка ордера для {symbol}: {res}")

async def close_position(session, symbol):
    current_side = open_positions.get(symbol)
    if not current_side:
        return
    
    close_side = "SELL" if current_side == "LONG" else "BUY"
    
    # Отримуємо інформацію про позицію
    pos_path = "/fapi/v2/positionRisk"
    pos_res = await binance_request(session, "GET", pos_path)
    
    try:
        position_qty = 0.0
        if isinstance(pos_res, list):
            for p in pos_res:
                if p['symbol'] == symbol:
                    position_qty = float(p['positionAmt'])
                    break
        
        if position_qty == 0:
            open_positions.pop(symbol, None)
            return

        abs_qty = abs(position_qty)
        path = "/fapi/v1/order"
        params = {
            "symbol": symbol,
            "side": close_side,
            "type": "MARKET",
            "quantity": abs_qty
        }
        res = await binance_request(session, "POST", path, params)
        if res and "orderId" in res:
            print(f"Binance Demo: Позицію по {symbol} закрито.")
            open_positions.pop(symbol, None)
    except Exception as e:
        print(f"Помилка закриття позиції на Binance: {e}")

# --- СКАНЕР РИНКУ ---
async def fetch_top_binance_symbols(session):
    url = f"{BINANCE_BASE_URL}/fapi/v1/ticker/24hr"
    try:
        async with session.get(url, timeout=5) as response:
            if response.status == 200:
                tickers = await response.json()
                usdt_tickers = []
                for t in tickers:
                    symbol = t.get("symbol", "")
                    if symbol.endswith("USDT"):
                        turnover = float(t.get("quoteVolume", 0))
                        if turnover >= MIN_24H_VOLUME_USDT:
                            usdt_tickers.append((symbol, turnover))
                usdt_tickers.sort(key=lambda x: x[1], reverse=True)
                return [item[0] for item in usdt_tickers[:TOP_COINS_LIMIT]]
    except Exception:
        pass
    return []

async def fetch_kline_data(session, symbol):
    # Binance приймає інтервали на кшталт '5m'
    url = f"{BINANCE_BASE_URL}/fapi/v1/klines?symbol={symbol}&interval={TIMEFRAME}m&limit={LIMIT_CANDLES}"
    try:
        async with session.get(url, timeout=4) as response:
            if response.status == 200:
                raw_list = await response.json()
                formatted = []
                for item in raw_list:
                    formatted.append({
                        "low": float(item[3]),
                        "high": float(item[2]),
                        "close": float(item[4]),
                        "volume": float(item[5])
                    })
                return formatted
    except Exception:
        pass
    return None

async def send_to_discord(session, webhook_url, message):
    if not webhook_url:
        return
    try:
        await session.post(webhook_url, json={"content": message})
    except Exception:
        pass

async def check_single_coin(session, symbol, discord_webhook_url):
    current_time = time.time()
    if symbol in last_alert_time and current_time - last_alert_time[symbol] < COOLDOWN_SECONDS:
        return

    kline_data = await fetch_kline_data(session, symbol)
    if not kline_data or len(kline_data) < 20:
        return

    try:
        volumes = [x['volume'] for x in kline_data]
        lows = [x['low'] for x in kline_data]
        highs = [x['high'] for x in kline_data]
        closes = [x['close'] for x in kline_data]
        
        current_volume = volumes[-1]
        if current_volume <= 0:
            return

        avg_volume = sum(volumes[:-1]) / (len(volumes) - 1)
        if avg_volume <= 0:
            return
        
        current_price = closes[-1]
        if current_volume < (avg_volume * VOLUME_MULTIPLIER):
            return

        surge_percent = int((current_volume / avg_volume - 1) * 100)

        # 1. ЛОНГ (Підтримка)
        support_level = min(lows[:-1])
        if support_level > 0 and 0 <= (current_price - support_level) / support_level <= APPROACH_PERCENT:
            
            if open_positions.get(symbol) == "SHORT":
                await close_position(session, symbol)

            alert_message = (
                f"🟢🎯 **BINANCE DEMO [ЛОНГ / Підтримка 5m]**: `{symbol}`\n"
                f"• Напрямок: 🚀 **Підхід до дна / Відскок**\n"
                f"• Ціна: `{current_price}` (Підтримка: `{support_level}`)\n"
                f"• Об'єм: `+{surge_percent}%` від середнього!"
            )
            last_alert_time[symbol] = current_time
            await send_to_discord(session, discord_webhook_url, alert_message)
            
            if symbol not in open_positions:
                await open_market_order(session, symbol, "BUY")
                open_positions[symbol] = "LONG"
            return

        # 2. ШОРТ (Опір)
        resistance_level = max(highs[:-1])
        if resistance_level > 0 and 0 <= (resistance_level - current_price) / resistance_level <= APPROACH_PERCENT:
            
            if open_positions.get(symbol) == "LONG":
                await close_position(session, symbol)

            alert_message = (
                f"🔴🎯 **BINANCE DEMO [ШОРТ / Опір 5m]**: `{symbol}`\n"
                f"• Напрямок: 📉 **Підхід до хаю / Відбій**\n"
                f"• Ціна: `{current_price}` (Опір: `{resistance_level}`)\n"
                f"• Об'єм: `+{surge_percent}%` від середнього!"
            )
            last_alert_time[symbol] = current_time
            await send_to_discord(session, discord_webhook_url, alert_message)
            
            if symbol not in open_positions:
                await open_market_order(session, symbol, "SELL")
                open_positions[symbol] = "SHORT"
            return

    except Exception:
        pass

async def self_ping_loop(session):
    while True:
        await asyncio.sleep(240)
        try:
            async with session.get(RENDER_URL, timeout=5):
                pass
        except Exception:
            pass

async def main():
    print("Бот запущено для Binance Demo (Сканер + Авто-угоди)...")
    async with aiohttp.ClientSession() as session:
        asyncio.create_task(self_ping_loop(session))
        
        while True:
            start_time = asyncio.get_event_loop().time()
            symbols_list = await fetch_top_binance_symbols(session)
            
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
        self.wfile.write(b"Binance Demo Scanner Bot is running!")
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
        print("Бот зупинений.")
                         
