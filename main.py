import hmac
import hashlib
import time
import requests
import urllib.parse

# Твої дані демо-рахунку BingX
API_KEY = "TAMQgieAMOQJuuik9XpcPa5wHch0wmXaQ8G6yBdXUlZ6G2vv3uS47QgW1NNfcI4BxLmgmeo5XwFzXEQx9dOA"
SECRET_KEY = "18EgcerNxJ5fv7TCnxQ5okPXr1VpYsJPnhYRF1GZOsikgHD2c1owRyqApKieZYQoCY2SeclmXVTdW33nIIjg"

# Базовий URL для BingX Futures (демо/тестове середовище або стандартний прод-ендпоінт для ф'ючерсів)
BASE_URL = "https://open-api.bingx.com"

def get_sign(secret_key, params):
    """Створення підпису HMAC SHA256 для запитів BingX API"""
    parameters = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(secret_key.encode('utf-8'), parameters.encode('utf-8'), hashlib.sha256).hexdigest()

def calculate_volume_sl_tp(entry_price, candle_low, candle_high, risk_buffer_pct=0.002):
    """
    Розрахунок стоп-лосса і тейк-профіта на основі діапазону та мінімуму свічки з буфером шуму.
    
    :param entry_price: ціна входження (BUY)
    :param candle_low: мінімум свічки / нижня графіка зони накопичення (як на твойму скріншоті)
    :param candle_high: максимум свічки
    :param risk_buffer_pct: додатковий відступ (буфер) нижче мінімуму від ринкового шуму
    :return: (stop_loss_price, take_profit_price)
    """
    # Стоп ставимо нижче мінімуму свічки з урахуванням буфера, щоб уникнути вибивання тінями
    stop_loss = candle_low * (1 - risk_buffer_pct)
    
    # Якщо розрахований стоп чомусь вище або дорівнює входу (захист від непередбачуваних даних)
    if stop_loss >= entry_price:
        stop_loss = entry_price * 0.99  
        
    # Визначаємо ризик (відстань від входу до стопу)
    risk_distance = entry_price - stop_loss
    
    # Тейк-профіт робимо з фіксованим співвідношенням ризик/прибуток (наприклад, 1 до 2 або 1 до 3)
    # або прив'язуємо до верхньої межі імпульсу (candle_high)
    take_profit = entry_price + (risk_distance * 2.5)
    
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
        "type": "MARKET",         # Ринків ордер для швидкого входу за сигналом
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
            print(f"❌ Помилка від біріж: {data}")
            return None
    except Exception as e:
        print(f"❌ Помилка з'єднання з API BingX: {e}")
        return None
        
