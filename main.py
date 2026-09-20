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

# --- FLASK ДЛЯ RENDER (щоб додаток не закривався) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "BingX Volume Scanner & Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    print(f"🌐 Вебсервер Flask запущено на порту {port}")
    app.run(host="0.0.0.0", port=port)


# --- ФУНКЦІЇ ДІСКОРДУ ---
def send_discord_alert(message: str):
    """Відправка сповіщень у твій Discord-канал через вебхук"""
    payload = {
        "content": message
    }
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        if response.status_code == 204:
            print("📤 Сповіщення успішно надіслано в Discord!")
        else:
            print(f"❌ Помилка відправки в Discord: {response.status_code}, {response.text}")
    except Exception as e:
        print(f"❌ Виняток при надсиланні на вебхук: {e}")


# --- ФУНКЦІЇ БІРЖІ ТА РОЗРАХУНКУ РИЗИКУ ---
def get_sign(secret_key, params):
    """Створення підпису HMAC SHA256 для запитів BingX API"""
    parameters = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(secret_key.encode('utf-8'), parameters.encode('utf-8'), hashlib.sha256).hexdigest()

def calculate_volume_sl_tp(entry_price, candle_low, candle_high, risk_buffer_pct=0.002):
    """
    Розрахунок стоп-лосса і тейк-профіта на основі діапазону та мінімуму свічки з буфером шуму.
    Захищає від вибивання позиції хибними тінями та ретестами.
    """
    stop_loss = candle_low * (1 - risk_buffer_pct)
    
    if stop_loss >= entry_price:
        stop_loss = entry_price * 0.99  
        
    risk_distance = entry_price - stop_loss
    take_profit = entry_price + (risk_distance * 2.5)  # Співвідношення 1 до 2.5
    
    return round(stop_loss, 4), round(take_profit, 4)

def place_bingx_order(symbol, side, quantity, stop_loss, take_profit):
    """
    Відправка ринкового ордера на ф'ючерси BingX разом із захисними Stop Loss та Take Profit
    """
    endpoint = "/openApi/swap/v2/trade/order"
    url = BASE_URL + endpoint
    
    timestamp = str(int(time.time() * 1000))
    
    params = {
        "symbol": symbol,         # Наприклад, "SUI-USDT"
        "side": side,             # "BUY" або "SELL"
        "positionSide": "LONG",   # Для лонг позиції
        "type": "MARKET",         # Ринковий ордер для швидкого входу
        "quantity": quantity,     # Об'єм контракту
        "stopLoss": str(stop_loss),
        "takeProfit": str(take_profit),
        "timestamp": timestamp
    }
    
    params["signature"] = get_sign(SECRET_KEY, params)
    
    headers = {
        "X-BX-APIKEY": API_KEY
    }
    
    try:
        response = requests.post(url, headers=headers, params=params)
        data = response.json()
        if data.get("code") == 0:
            success_msg = f"✅ Успішно відкрито {side} по {symbol}! SL: {stop_loss}, TP: {take_profit}"
            print(success_msg)
            send_discord_alert(success_msg)
            return data
        else:
            error_msg = f"❌ Помилка від біржі BingX: {data}"
            print(error_msg)
            return None
    except Exception as e:
        error_msg = f"❌ Помилка з'єднання з API BingX: {e}"
        print(error_msg)
        return None


# --- ОСНОВНИЙ ЦИКЛ СКАНЕРА ТА ТОРГІВЛІ ---
def main_scanner_loop():
    print("🚀 Сканер та торговий бот запущені у фоновому режимі...")
    send_discord_alert("🚀 Бот сканування об'ємів та торгівлі успішно запущено й він стежить за ринком!")
    
    counter = 0
    while True:
        try:
            counter += 1
            print(f"🔄 Сканування ринку триває... (Ітерація #{counter})")
            
            # Тут виконується логіка твого сканування об'ємів / свічок
            # Коли знаходиш сигнал, викликаєш:
            # sl, tp = calculate_volume_sl_tp(entry_price, candle_low, candle_high)
            # place_bingx_order(symbol, "BUY", quantity, sl, tp)
            
            time.sleep(60) # Перевірка кожну хвилину
        except Exception as e:
            print(f"❌ Помилка в основному циклі: {e}")
            time.sleep(10)


if __name__ == "__main__":
    # Запускаємо вебсервер у фоновому потоці для Render
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    # Запускаємо основну логіку сканера та торгівлі
    main_scanner_loop()
    
