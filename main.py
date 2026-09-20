import hmac
import hashlib
import time
import requests
import urllib.parse
import threading
import os
from flask import Flask

# --- НАЛАШТУВАННЯ BINGX API ---
API_KEY = "TAMQgieAMOQJuuik9XpcPa5wHch0wmXaQ8G6yBdXUlZ6G2vv3uS47QgW1NNfcI4BxLmgmeo5XwFzXEQx9dOA"
SECRET_KEY = "18EgcerNxJ5fv7TCnxQ5okPXr1VpYsJPnhYRF1GZOsikgHD2c1owRyqApKieZYQoCY2SeclmXVTdW33nIIjg"
BASE_URL = "https://open-api.bingx.com"

# --- НАЛАШТУВАННЯ DISCORD ---
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1545120862875815986/khalqqspIhWB0cVtqQpPHq7kYBdj55Nsx70z0xATsarbD4oJpD1FgPUsFSozwfFv9xOz"

# --- ТОРГОВІ ПАРАМЕТРИ ---
LEVERAGE = 20           # Кредитне плече 20x
RISK_DEPOSIT_PCT = 0.02  # 2% від депозиту на позицію
TIMEFRAME = "5m"        # Таймфрейм 5 хвилин
VOLUME_MULTIPLIER = 2.0 # Поріг перевищення середнього об'єму (у 2 рази)

# --- FLASK ДЛЯ RENDER (підтримка активності) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Clean BingX Volume & BOS Scanner is running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    print(f"🌐 Вебсервер Flask запущено на порту {port}")
    app.run(host="0.0.0.0", port=port)


# --- ФУНКЦІЇ ДІСКОРДУ ---
def send_discord_alert(message: str):
    """Надсилання сповіщень виключно у твій Discord-канал"""
    payload = {"content": message}
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        if response.status_code == 204:
            print("📤 Сповіщення успішно надіслано в Discord!")
        else:
            print(f"❌ Помилка відправки в Discord: {response.status_code}, {response.text}")
    except Exception as e:
        print(f"❌ Виняток при надсиланні на вебхук: {e}")


# --- БІРЖОВІ ФУНКЦІЇ BINGX ---
def get_sign(secret_key, params):
    """Створення підпису HMAC SHA256"""
    parameters = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(secret_key.encode('utf-8'), parameters.encode('utf-8'), hashlib.sha256).hexdigest()

def get_bingx_balance():
    """Отримання балансу ф'ючерсного акаунта"""
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
            balance_info = data.get("data", {}).get("balance", {})
            return float(balance_info.get("equity", 0))
    except Exception as e:
        print(f"❌ Помилка отримання балансу: {e}")
    return 0.0

def set_bingx_leverage(symbol):
    """Встановлення плеча 20x"""
    endpoint = "/openApi/swap/v1/trade/leverage"
    url = BASE_URL + endpoint
    timestamp = str(int(time.time() * 1000))
    params = {
        "symbol": symbol,
        "leverage": str(LEVERAGE),
        "side": "LONG",
        "timestamp": timestamp
    }
    params["signature"] = get_sign(SECRET_KEY, params)
    headers = {"X-BX-APIKEY": API_KEY}
    
    try:
        response = requests.post(url, headers=headers, params=params)
        data = response.json()
        if data.get("code") != 0:
            print(f"⚠️ Попередження плеча для {symbol}: {data}")
    except Exception as e:
        print(f"❌ Помилка встановлення плеча: {e}")

def get_klines(symbol):
    """Отримання свічок (крок 5 хвилин) з BingX API"""
    endpoint = "/openApi/swap/v1/market/klines"
    url = BASE_URL + endpoint
    params = {
        "symbol": symbol,
        "interval": TIMEFRAME,
        "limit": 20  # Беремо останні 20 свічок для аналізу середнього об'єму
    }
    try:
        response = requests.get(url, params=params)
        data = response.json()
        if data.get("code") == 0:
            return data.get("data", [])
    except Exception as e:
        print(f"❌ Помилка отримання свічок для {symbol}: {e}")
    return []

def calculate_volume_sl_tp(entry_price, candle_low, candle_high, risk_buffer_pct=0.002):
    """Розрахунок стоп-лосса і тейк-профіта з буфером шуму"""
    stop_loss = candle_low * (1 - risk_buffer_pct)
    if stop_loss >= entry_price:
        stop_loss = entry_price * 0.99  
    risk_distance = entry_price - stop_loss
    take_profit = entry_price + (risk_distance * 2.5)  # Співвідношення 1 до 2.5
    return round(stop_loss, 4), round(take_profit, 4)

def place_bingx_order(symbol, side, entry_price, candle_low, candle_high):
    """Відкриття позиції з розрахунком 2% від депозиту та 20x плечем"""
    balance = get_bingx_balance()
    if balance <= 0:
        print("❌ Нульовий баланс!")
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
        "symbol": symbol,
        "side": side,
        "positionSide": "LONG",
        "type": "MARKET",
        "quantity": str(quantity),
        "stopLoss": str(stop_loss),
        "takeProfit": str(take_profit),
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
            print(msg)
            send_discord_alert(msg)
        else:
            print(f"❌ Помилка ордера BingX по {symbol}: {data}")
    except Exception as e:
        print(f"❌ Помилка з'єднання при відправці ордера: {e}")


# --- ОСНОВНИЙ СКАНЕР РИНКУ ---
def main_scanner_loop():
    startup_msg = f"🚀 Чистий бот сканування запущено! ТФ: {TIMEFRAME} | Плече: {LEVERAGE}x | Ризик: {RISK_DEPOSIT_PCT*100}%."
    print(startup_msg)
    send_discord_alert(startup_msg)
    
    # Список монет для моніторингу (можеш додавати інші пари)
    symbols_to_scan = ["BTC-USDT", "ETH-USDT", "SUI-USDT", "SOL-USDT", "AVAX-USDT"]
    
    counter = 0
    while True:
        try:
            counter += 1
            print(f"🔄 Сканування ринку (ТФ: {TIMEFRAME}) триває... (Ітерація #{counter})")
            
            for symbol in symbols_to_scan:
                klines = get_klines(symbol)
                if len(klines) < 10:
                    continue
                
                # Аналізуємо передостанню закриту свічку
                prev_candle = klines[-2]
                # Формат klines на BingX зазвичай: [timestamp, open, high, low, close, volume, ...]
                candle_low = float(prev_candle[3])
                candle_high = float(prev_candle[2])
                entry_price = float(prev_candle[4])
                current_volume = float(prev_candle[5])
                
                # Розрахунок середнього об'єму за попередні свічки
                past_volumes = [float(k[5]) for k in klines[:-2]]
                avg_volume = sum(past_volumes) / len(past_volumes) if past_volumes else 1.0
                
                # Умова сплеску об'єму (у N разів вище середнього)
                if current_volume >= (avg_volume * VOLUME_MULTIPLIER):
                    print(f"🎯 Знайдено сплеск об'єму по {symbol}!")
                    place_bingx_order(symbol, "BUY", entry_price, candle_low, candle_high)
            
            time.sleep(60) # Пауза хвилину між ітераціями сканування
        except Exception as e:
            print(f"❌ Помилка в основному циклі: {e}")
            time.sleep(15)


if __name__ == "__main__":
    # Запуск Flask у фоні
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    # Запуск основного сканера
    main_scanner_loop()
    
