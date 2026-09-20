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
LEVERAGE = 20          # Кредитне плече 20x
RISK_DEPOSIT_PCT = 0.02 # 2% від депозиту на позицію
TIMEFRAME = "5m"       # Таймфрейм 5 хвилин

# --- FLASK ДЛЯ RENDER ---
app = Flask(__name__)

@app.route('/')
def home():
    return "BingX Volume Scanner & Bot (5m, 20x) is running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    print(f"🌐 Вебсервер Flask запущено на порту {port}")
    app.run(host="0.0.0.0", port=port)


# --- ФУНКЦІЇ ДІСКОРДУ ---
def send_discord_alert(message: str):
    """Відправка сповіщень виключно у твій Discord-канал"""
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
            equity = float(balance_info.get("equity", 0))
            return equity
    except Exception as e:
        print(f"❌ Помилка отримання балансу: {e}")
    return 0.0

def set_bingx_leverage(symbol):
    """Встановлення кредитного плеча 20x для пари"""
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
        if data.get("code") == 0:
            print(f"⚙️ Плече {LEVERAGE}x успішно встановлено для {symbol}")
        else:
            print(f"⚠️ Попередження при встановленні плеча для {symbol}: {data}")
    except Exception as e:
        print(f"❌ Помилка з'єднання при встановленні плеча: {e}")

def calculate_volume_sl_tp(entry_price, candle_low, candle_high, risk_buffer_pct=0.002):
    """Розрахунок SL та TP з урахуванням буфера шуму"""
    stop_loss = candle_low * (1 - risk_buffer_pct)
    if stop_loss >= entry_price:
        stop_loss = entry_price * 0.99  
    risk_distance = entry_price - stop_loss
    take_profit = entry_price + (risk_distance * 2.5)  # Співвідношення 1 до 2.5
    return round(stop_loss, 4), round(take_profit, 4)

def place_bingx_order(symbol, side, entry_price, candle_low, candle_high):
    """Розрахунок об'єму (2% від балансу з 20x плечем), встановлення плеча та відправка ордера"""
    balance = get_bingx_balance()
    if balance <= 0:
        print("❌ Неможливо отримати баланс або він нульовий!")
        return None

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
            success_msg = (f"✅ Відкрито {side} по {symbol} (ТФ: {TIMEFRAME})!\n"
                           f"• Плече: {LEVERAGE}x | Маржа: 2% | Об'єм: {quantity}\n"
                           f"• Вхід: {entry_price} | SL: {stop_loss} | TP: {take_profit}")
            print(success_msg)
            send_discord_alert(success_msg)
            return data
        else:
            error_msg = f"❌ Помилка від біржа BingX по {symbol}: {data}"
            print(error_msg)
            return None
    except Exception as e:
        error_msg = f"❌ Помилка з'єднання з API BingX: {e}"
        print(error_msg)
        return None


# --- ОСНОВНИЙ ЦИКЛ СКАНЕРА (5м) ---
def main_scanner_loop():
    startup_msg = f"🚀 Бот запущено! Таймфрейм: {TIMEFRAME} | Плече: {LEVERAGE}x | Ризик: {RISK_DEPOSIT_PCT*100}% від депозиту."
    print(startup_msg)
    send_discord_alert(startup_msg)
    
    counter = 0
    while True:
        try:
            counter += 1
            print(f"🔄 Сканування ринку (ТФ: {TIMEFRAME}) триває... (Ітерація #{counter})")
            
            # Логіка пошуку свічок та об'ємів на 5м
            
            time.sleep(60) 
        except Exception as e:
            print(f"❌ Помилка в основному циклі: {e}")
            time.sleep(10)


if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    main_scanner_loop()
    
