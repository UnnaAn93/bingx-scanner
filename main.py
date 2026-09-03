import time
import requests
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1544732288263389284/y-TPXfbXNQBOF9tAshOib_UlqwyvClHln50VTx08wZTeWtzGNETLJW8UXERU4lkmWWYl"

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"BingX Advanced Scanner is active 24/7!")
    def log_message(self, format, *args):
        pass

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

threading.Thread(target=run_web_server, daemon=True).start()

def get_bingx_symbols():
    try:
        url = "https://open-api.bingx.com/openApi/swap/v1/quote/contracts"
        response = requests.get(url, timeout=10)
        data = response.json()
        if data.get("code") == 0:
            symbols = [item["symbol"] for item in data["data"] if item.get("status") == 1 and item["symbol"].endswith("-USDT")]
            return symbols[:500]
    except Exception as e:
        print(f"Помилка отримання пар: {e}")
    return []

def get_klines(symbol, interval="15m", limit=100):
    try:
        url = f"https://open-api.bingx.com/openApi/swap/v1/quote/klines?symbol={symbol}&interval={interval}&limit={limit}"
        response = requests.get(url, timeout=10)
        data = response.json()
        if data.get("code") == 0:
            closes = [float(candle["close"]) for candle in data["data"]]
            highs = [float(candle["high"]) for candle in data["data"]]
            lows = [float(candle["low"]) for candle in data["data"]]
            return closes, highs, lows
    except Exception as e:
        pass
    return None, None, None

def send_discord_alert(message):
    try:
        payload = {"content": message}
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        print(f"Помилка відправки у Discord: {e}")

def analyze_market():
    print("Запуск розширеного сканування (500 пар, боковики/тренди/трикутники)...")
    symbols = get_bingx_symbols()
    print(f"Знайдено активних пар: {len(symbols)}")

    for symbol in symbols:
        closes, highs, lows = get_klines(symbol, "15m", 100)
        if not closes or len(closes) < 50:
            continue

        current_price = closes[-1]
        prev_price = closes[-2]

        # 1. Боковик та пробій боковика
        # Визначаємо рівні за попередніми 20 свічками (не беручи до уваги поточну)
        box_high = max(highs[-25:-2])
        box_low = min(lows[-25:-2])
        box_range = (box_high - box_low) / current_price * 100

        alerts = []

        # Якщо коридор вузький (< 2%) — це боковик/консолідація
        if box_range < 2.0:
            # Перевірка на пробій боковика
            if prev_price <= box_high and current_price > box_high:
                alerts.append(f"🚀 **{symbol}**: Пробій верхньої межі боковика (Вихід з флєту вгору)!")
            elif prev_price >= box_low and current_price < box_low:
                alerts.append(f"⚠️ **{symbol}**: Пробій нижньої межі боковика (Вихід з флєту вниз)!")
            else:
                # Опціонально можна фіксувати сам факт утримання в боковику, але краще ловити пробої та патерни
                pass

        # 2. Трикутник / Звуження волатильності
        recent_range = (max(highs[-10:]) - min(lows[-10:])) / current_price * 100
        earlier_range = (max(highs[-40:-20]) - min(lows[-40:-20])) / closes[-30] * 100
        if recent_range < 1.2 and earlier_range > recent_range * 2:
            alerts.append(f"📐 **{symbol}**: Формування трикутника / звуження волатильності (Корридор: {recent_range:.2f}%)!")

        # 3. Локальна трендова та пробій трендової (порівняння ковзних / нахилу)
        # Простий розрахунок нахилу тренду через середні за різні періоди
        sma_fast = sum(closes[-10:]) / 10
        sma_slow = sum(closes[-40:]) / 40
        prev_sma_fast = sum(closes[-11:-1]) / 10
        prev_sma_slow = sum(closes[-41:-1]) / 40

        # Перетин швидкої і повільної SMA як заміна пробою трендового нахилу
        if prev_sma_fast <= prev_sma_slow and sma_fast > sma_slow:
            alerts.append(f"📈 **{symbol}**: Пробій / Зміна трендовою лінії знизу вгору (Бичачий сигнал)!")
        elif prev_sma_fast >= prev_sma_slow and sma_fast < sma_slow:
            alerts.append(f"📉 **{symbol}**: Пробій / Зміна трендовою лінії зверху вниз (Ведмежий сигнал)!")

        for alert in alerts:
            send_discord_alert(alert)
            
        time.sleep(0.1)

def main():
    print("Розширений сканер запущено 24/7.")
    while True:
        try:
            analyze_market()
        except Exception as e:
            print(f"Помилка циклу: {e}")
        time.sleep(600)

if __name__ == "__main__":
    main()
    
