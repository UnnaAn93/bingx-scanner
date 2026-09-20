import asyncio
import aiohttp
import os
import time
import hmac
import hashlib
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# --- НАЛАШТУВАННЯ ТОРГІВЛІ ТА СКАНУВАННЯ ---
VOLUME_MULTIPLIER = 2.2           # Сплеск об'єму у 2.2 рази
APPROACH_PERCENT = 0.007          # 0.7% до рівня (підтримки або опору)
TIMEFRAME = "5m"                  # Таймфрейм 5 хвилин
LIMIT_CANDLES = 30                # Історія свічок
TOP_COINS_LIMIT = 150             # Кількість найактивніших пар
MIN_24H_VOLUME_USDT = 5_000_000   # Мінімальний добовий об'єм у USDT
COOLDOWN_SECONDS = 300            # Кулдаун 5 хвилин на одну монету

# Торгові параметри
LEVERAGE = 20                     # Плече
TRADE_USDT_AMOUNT = 10.0          # Сума ордера в USDT (розмір позиції)

# API ключі BingX (замість плейсхолдерів встав свої реальні ключі)
BINGX_API_KEY = os.environ.get("BINGX_API_KEY", "ТВОЇ_API_КЛЮЧІ_БІРЖІ")
BINGX_SECRET_KEY = os.environ.get("BINGX_SECRET_KEY", "ТВОЙ_SECRET_KEY_БІРЖІ")
BINGX_BASE_URL = "https://open-api.bingx.com"

DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1551243480989433927/cIcSwvFUvrh7vnRbXcCx9c8pRMBkyX6VBNVbsEQBp6PWc7aJmXZsYnLnU79Rh8JJaKMF"
RENDER_URL = "https://bingx-scanner-djbf.onrender.com"

last_alert_time = {}
open_positions = {}  # Зберігає поточні відкриті позиції: {symbol: "LONG" або "SHORT"}

# --- ФУНКЦІЇ ПІДПИСУ ТА ЗАПИТІВ ДО BINGX API ---
def get_sign(api_secret, payload):
    signature = hmac.new(api_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return signature

async def bingx_request(session, method, path, params=None):
    if params is None:
        params = {}
    
    params["timestamp"] = str(int(time.time() * 1000))
    parameters = urllib.parse.urlencode(sorted(params.items()))
    signature = get_sign(BINGX_SECRET_KEY, parameters)
    url = f"{BINGX_BASE_URL}{path}?{parameters}&signature={signature}"
    
    headers = {"X-BX-APIKEY": BINGX_API_KEY}
    try:
        async with session.request(method, url, headers=headers, timeout=5) as response:
            data = await response.json()
            return data
    except Exception as e:
        print(f"Помилка запиту до BingX API ({path}): {e}")
        return None

async def set_leverage(session, symbol):
    path = "/openApi/swap/v2/trade/leverage"
    params = {
        "symbol": symbol,
        "leverage": str(LEVERAGE),
        "side": "BOTH"
    }
    await bingx_request(session, "POST", path, params)

async def open_market_order(session, symbol, side):
    """
    side: 'BUY' (для Лонг) або 'SELL' (для Шорт)
    """
    await set_leverage(session, symbol)
    
    # Отримуємо поточну ціну для розрахунку кількості монет
    ticker_data = await session.get(f"https://open-api.bingx.com/openApi/swap/v2/quote/ticker?symbol={symbol}")
    ticker_json = await ticker_data.json()
    try:
        current_price = float(ticker_json['data']['lastPrice'])
    except Exception:
        return

    # Розрахунок кількості з урахуванням плеча
    notional_size = TRADE_USDT_AMOUNT * LEVERAGE
    quantity = notional_size / current_price
    quantity = round(quantity, 4) # Спрощене округлення

    path = "/openApi/swap/v2/trade/order"
    params = {
        "symbol": symbol,
        "side": side,
        "positionSide": "BOTH",
        "type": "MARKET",
        "quantity": str(quantity)
    }
    
    res = await bingx_request(session, "POST", path, params)
    if res and res.get("code") == 0:
        print(f"Успішно відкрито ордер [{side}] для {symbol} на суму {notional_size} USDT")
    else:
        print(f"Помилка відкриття ордера для {symbol}: {res}")

async def close_position(session, symbol):
    """Закриття поточної позиції за ринком"""
    # Перевіряємо напрямок відкритої позиції, щоб закрити протилежним ордером
    current_side = open_positions.get(symbol)
    if not current_side:
        return
    
    close_side = "SELL" if current_side == "LONG" else "BUY"
    
    # Отримуємо поточну інформацію про позицію для закриття об'єму
    path_pos = "/openApi/swap/v2/user/positions"
    pos_res = await bingx_request(session, "GET", path_pos, {"symbol": symbol})
    
    try:
        positions = pos_res.get("data", [])
        position_amt = 0
        for p in positions:
            if p["symbol"] == symbol:
                position_amt = float(p["positionAmt"])
                break
        
        if position_amt == 0:
            open_positions.pop(symbol, None)
            return

        path_order = "/openApi/swap/v2/trade/order"
        params = {
            "symbol": symbol,
            "side": close_side,
            "positionSide": "BOTH",
            "type": "MARKET",
            "quantity": str(abs(position_amt))
        }
        res = await bingx_request(session, "POST", path_order, params)
        if res and res.get("code") == 0:
            print(f"Позицію по {symbol} закрито за ринком.")
            open_positions.pop(symbol, None)
    except Exception as e:
        print(f"Помилка при закритті позиції {symbol}: {e}")

# --- СКАНЕР РИНКУ ---
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
                return [item[0] for item in usdt_tickers[:TOP_COINS_LIMIT]]
    except Exception:
        pass
    return []

async def fetch_kline_data(session, symbol):
    url = "https://open-api.bingx.com/openApi/swap/v2/quote/klines"
    params = {"symbol": symbol, "interval": TIMEFRAME, "limit": LIMIT_CANDLES}
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
        if current_volume < (avg_volume * VOLUME_MULTIPLIER):
            return

        surge_percent = int((current_volume / avg_volume - 1) * 100)

        # 1. ЛОНГ (Підтримка знизу)
        support_level = min(lows[:-1])
        if support_level > 0 and 0 <= (current_price - support_level) / support_level <= APPROACH_PERCENT:
            
            # Якщо вже була відкрита ШОРТ позиція — закриваємо її перед розворотом
            if open_positions.get(symbol) == "SHORT":
                await close_position(session, symbol)

            alert_message = (
                f"🟢🎯 **УВАГА [ЛОНГ / Підтримка 5m]**: `{symbol}`\n"
                f"• Напрямок: 🚀 **Підхід до локального дна / Відскок**\n"
                f"• Ціна: `{current_price}` (Підтримка: `{support_level}`)\n"
                f"• Об'єм свічки: `+{surge_percent}%` від середнього!"
            )
            last_alert_time[symbol] = current_time
            await send_to_discord(session, discord_webhook_url, alert_message)
            
            # ВІДКРИТТЯ ЛОНГ ПОЗИЦІЇ ЗА РИНКОМ
            if symbol not in open_positions:
                await open_market_order(session, symbol, "BUY")
                open_positions[symbol] = "LONG"
            return

        # 2. ШОРТ (Опір зверху)
        resistance_level = max(highs[:-1])
        if resistance_level > 0 and 0 <= (resistance_level - current_price) / resistance_level <= APPROACH_PERCENT:
            
            # Якщо вже була відкрита ЛОНГ позиція — закриваємо її перед розворотом
            if open_positions.get(symbol) == "LONG":
                await close_position(session, symbol)

            alert_message = (
                f"🔴🎯 **УВАГА [ШОРТ / Опір 5m]**: `{symbol}`\n"
                f"• Напрямок: 📉 **Підхід до локального хаю / Відбій**\n"
                f"• Ціна: `{current_price}` (Опір: `{resistance_level}`)\n"
                f"• Об'єм свічки: `+{surge_percent}%` від середнього!"
            )
            last_alert_time[symbol] = current_time
            await send_to_discord(session, discord_webhook_url, alert_message)
            
            # ВІДКРИТТЯ ШОРТ ПОЗИЦІЇ ЗА РИНКОМ
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
    print("Бот запущено: Сканер + Автоматичний вхід/вихід за сигналом...")
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
        self.wfile.write(b"BingX Auto-Trading Scanner Bot is running!")
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
            
