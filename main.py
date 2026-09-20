import hmac
import hashlib
import time
import requests
import urllib.parse
import threading
import os
import asyncio
import aiohttp
from flask import Flask

# --- НАЛАШТУВАННЯ BINGX API ---
API_KEY = "TAMQgieAMOQJuuik9XpcPa5wHch0wmXaQ8G6yBdXUlZ6G2vv3uS47QgW1NNfcI4BxLmgmeo5XwFzXEQx9dOA"
SECRET_KEY = "18EgcerNxJ5fv7TCnxQ5okPXr1VpYsJPnhYRF1GZOsikgHD2c1owRyqApKieZYQoCY2SeclmXVTdW33nIIjg"
BASE_URL = "https://open-api.bingx.com"

# --- НАЛАШТУВАННЯ DISCORD ---
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1551243480989433927/cIcSwvFUvrh7vnRbXcCx9c8pRMBkyX6VBNVbsEQBp6PWc7aJmXZsYnLnU79Rh8JJaKMF"

# --- ТОРГОВІ ПАРАМЕТРИ ---
LEVERAGE = 20           
RISK_DEPOSIT_PCT = 0.02  
TIMEFRAME = "5m"        
VOLUME_MULTIPLIER = 2.0 

# --- FLASK ДЛЯ RENDER ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Async Clean BingX Scanner is running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


def send_discord_alert(message: str):
    payload = {"content": message}
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        print(f"❌ Помилка Discord: {e}")


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
        response = requests.get(url, headers=headers, params=params)
        data = response.json()
        if data.get("code") == 0:
            return float(data.get("data", {}).get("balance", {}).get("equity", 0))
    except Exception:
        pass
    return 0.0


def set_bingx_leverage(symbol):
    endpoint = "/openApi/swap/v1/trade/leverage"
    url = BASE_URL + endpoint
    timestamp = str(int(time.time() * 1000))
    params = {"symbol": symbol, "leverage": str(LEVERAGE), "side": "LONG", "timestamp": timestamp}
    params["signature"] = get_sign(SECRET_KEY, params)
    headers = {"X-BX-APIKEY": API_KEY}
    try:
        requests.post(url, headers=headers, params=params)
    except Exception:
        pass


def get_bingx_symbols():
    endpoint = "/openApi/swap/v1/market/contracts"
    url = BASE_URL + endpoint
    try:
        response = requests.get(url)
        data = response.json()
        if data.get("code") == 0:
            contracts = data.get("data", {}).get("contracts", [])
            return [c["symbol"] for c in contracts if c.get("symbol", "").endswith("-USDT") and c.get("status"] == 1]
    except Exception:
        pass
    return ["BTC-USDT", "ETH-USDT", "SOL-USDT"]


async def fetch_klines(session, symbol):
    url = BASE_URL + "/openApi/swap/v1/market/klines"
    params = {"symbol": symbol, "interval": TIMEFRAME, "limit": 20}
    try:
        async with session.get(url, params=params, timeout=5) as response:
            data = await response.json()
            if data.get("code") == 0:
                return symbol, data.get("data", [])
    except Exception:
        pass
    return symbol, []


def calculate_volume_sl_tp(entry_price, candle_low, candle_high, risk_buffer_pct=0.002):
    stop_loss = candle_low * (1 - risk_buffer_pct)
    if stop_loss >= entry_price:
        stop_loss = entry_price * 0.99  
    risk_distance = entry_price - stop_loss
    take_profit = entry_price + (risk_distance * 2.5)  
    return round(stop_loss, 4), round(take_profit, 4)


def place_bingx_order(symbol, side, entry_price, candle_low, candle_high):
    balance = get_bingx_balance()
    if balance <= 0:
        return

    set_bingx_leverage(symbol)

    margin_to_use = balance * RISK_DEPOSIT_PCT
    position_notional = margin_to_use * LEVERAGE
    quantity = round(position_notional / entry_price, 3)
    if quantity <= 0:
        quantity = 1  

    stop_loss, take_profit = calculate_volume_sl_tp(entry_price, candle_low, candle_high)

    endpoint = "/openApi/swap/v2/trade/order"
    url = BASE_URL + endpoint
    timestamp = str(int(time.time() * 1000))
    params = {
        "symbol": symbol, "side": side, "positionSide": "LONG",
        "type": "MARKET", "quantity": str(quantity),
        "stopLoss": str(stop_loss), "takeProfit": str(take_profit),
        "timestamp": timestamp
    }
    params["signature"] = get_sign(SECRET_KEY, params)
    headers = {"X-BX-APIKEY": API_KEY}
    
    try:
        response = requests.post(url, headers=headers, params=params)
        data = response.json()
        if data.get("code") == 0:
            msg = (f"🚨 **УВАГА [ЛОНГ / Об'ємний імпульс 5m]**:\n"
                   f"• Пара: `{symbol}`\n"
                   f"• Плече: {LEVERAGE}x | Ризик: 2% депозиту | Об'єм: {quantity}\n"
                   f"• Вхід: {entry_price} | SL: {stop_loss} | TP: {take_profit}")
            send_discord_alert(msg)
    except Exception:
        pass


async def scan_market():
    startup_msg = f"🚀 Асинхронний чистий сканер запущено! ТФ: {TIMEFRAME} | Плече: {LEVERAGE}x | Ризик: {RISK_DEPOSIT_PCT*100}%."
    send_discord_alert(startup_msg)
    
    async with aiohttp.ClientSession() as session:
        counter = 0
        while True:
            try:
                counter += 1
                symbols = get_bingx_symbols()
                
                tasks = [fetch_klines(session, symbol) for symbol in symbols]
                results = await asyncio.gather(*tasks)
                
                for symbol, klines in results:
                    if len(klines) < 10:
                        continue
                    
                    prev_candle = klines[-2]
                    candle_low = float(prev_candle[3])
                    candle_high = float(prev_candle[2])
                    entry_price = float(prev_candle[4])
                    current_volume = float(prev_candle[5])
                    
                    past_volumes = [float(k[5]) for k in klines[:-2]]
                    avg_volume = sum(past_volumes) / len(past_volumes) if past_volumes else 1.0
                    
                    if current_volume >= (avg_volume * VOLUME_MULTIPLIER):
                        place_bingx_order(symbol, "BUY", entry_price, candle_low, candle_high)
                
                await asyncio.sleep(60)
            except Exception as e:
                print(f"Помилка асинхронного циклу: {e}")
                await asyncio.sleep(15)


def main():
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    asyncio.run(scan_market())


if __name__ == "__main__":
    main()
    
