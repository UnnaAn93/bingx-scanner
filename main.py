import hmac
import hashlib
import time
import requests
import urllib.parse
import threading
from flask import Flask

# --- НАЛАШТУВАННЯ BINGX API ---
API_KEY = "TAMQgieAMOQJuuik9XpcPa5wHch0wmXaQ8G6yBdXUlZ6G2vv3uS47QgW1NNfcI4BxLmgmeo5XwFzXEQx9dOA"
SECRET_KEY = "18EgcerNxJ5fv7TCnxQ5okPXr1VpYsJPnhYRF1GZOsikgHD2c1owRyqApKieZYQoCY2SeclmXVTdW33nIIjg"
BASE_URL = "https://open-api.bingx.com"

# --- FLASK ДЛЯ RENDER (щоб додаток не закривався) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "BingX Volume Scanner & Bot is running!"

def run_flask():
    app.run(host="0.0.0.0", port=10000)


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
        "type": "MARKET",         # Ринків ордер для швидкого входу
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
            print(f"✅ Успішно відкрито {side} по {symbol}! SL: {stop_loss}, TP: {take_profit}")
            return data
        else:
            print(f"❌ Помилка від біржі: {data}")
            return None
    except Exception as e:
        print(f"❌ Помилка з'єднання з API BingX: {e}")
        return None


# --- ОСНОВНИЙ ЦИКЛ СКАНЕРА ТА ТОРГІВЛІ ---
def main_scanner_loop():
    print("🚀 Сканер та торговий бот запущені у фоновому режимі...")
    while True:
        try:
            # Т тут виконується логіка твого сканування (наприклад, 15-хвилинні таймфрейми)
            # Коли знаходиш сигнал, наприклад для SUI-USDT:
            # symbol = "SUI-USDT"
            # entry_price = 1.4460
            # candle_low = 1.4200
            # candle_high = 1.4500
            # sl, tp = calculate_volume_sl_tp(entry_price, candle_low, candle_high)
            # place_bingx_order(symbol, "BUY", 10, sl, tp)
            
            time.sleep(60) # Перевірка кожну хвилину
        except Exception as e:
            print(f"Помилка в основному циклі: {e}")
            time.sleep(10)


if __name__ == "__main__":
    # Запускаємо вебсервер у фоновому потоці для Render
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    # Запускаємо основну логіку сканера та торгівлі
    main_scanner_loop()
    
