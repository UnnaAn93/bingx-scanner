import time
import requests
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1544732288263389284/y-TPXfbXNQBOF9tAshOib_UlqwyvClHln50VTx08wZTeWtzGNETLJW8UXERU4lkmWWYl"

# Мікро-сервер для того, щоб Render був задоволений вебсервісом
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"BingX Scanner is active 24/7!")
    def log_message(self, format, *args):
        pass

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

# Запускаємо вебсервер у фоновому потоці
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
    print("Запуск сканування ринку (500 пар, 15m)...")
    symbols = get_bingx_symbols()
    print(f"Знайдено активних пар: {len(symbols)}")

    for symbol in symbols:
        closes, highs, lows = get_klines(symbol, "15m", 100)
        if not closes or len(closes) < 50:
            continue

        sma_50 = sum(closes[-50:]) / 50
        current_price = closes[-1]

        recent_highs = max(highs[-20:])
        recent_lows = min(lows[-20:])
        range_percent = (recent_highs - recent_lows) / current_price * 100

        earlier_range = (max(highs[-50:-20]) - min(lows[-50:-20])) / closes[-30] * 100

        alerts = []

        if closes[-2] < sma_50 and current_price >= sma_50:
            alerts.append(f"🟢 **{symbol}**: Перетин SMA 50 знизу вгору!")
        elif closes[-2] > sma_50 and current_price <= sma_50:
            alerts.append(f"🔴 **{symbol}**: Перетин SMA 50 зверху вниз!")

        if range_percent < 1.5 and earlier_range > range_percent * 1.5:
            alerts.append(f"📐 **{symbol}**: Звуження діапазону (можливий пробій / трикутник)! Коридор: {range_percent:.2f}%")

        for alert in alerts:
            send_discord_alert(alert)
            
        time.sleep(0.1)

def main():
    print("Сканер запущено 24/7.")
    while True:
        try:
            analyze_market()
        except Exception as e:
            print(f"Помилка циклу: {e}")
        time.sleep(600)

if __name__ == "__main__":
    main()
        
