import time
import requests
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1544732288263389284/y-TPXfbXNQBOF9tAshOib_UlqwyvClHln50VTx08wZTeWtzGNETLJW8UXERU4lkmWWYl"

def send_discord_alert(message):
    try:
        payload = {"content": message}
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        print(f"Помилка відправки у Discord: {e}")

send_discord_alert("🟢 **Сканер перезапущено у відлагодженому режимі!**")

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
        print(f"Помилка отримання списку пар: {e}")
    return []

def get_klines(symbol, interval="15m", limit=50):
    try:
        url = f"https://open-api.bingx.com/openApi/swap/v1/quote/klines?symbol={symbol}&interval={interval}&limit={limit}"
        response = requests.get(url, timeout=5)
        data = response.json()
        if data.get("code") == 0 and data.get("data"):
            closes = [float(c["close"]) for c in data["data"]]
            highs = [float(c["high"]) for c in data["data"]]
            lows = [float(c["low"]) for c in data["data"]]
            return closes, highs, lows
    except Exception as e:
        pass
    return None, None, None

def analyze_market():
    print("--- Початок сканування ринку ---")
    symbols = get_bingx_symbols()
    print(f"Отримано активних пар для перевірки: {len(symbols)}")

    if not symbols:
        print("Список пар пустий! Пропускаємо ітерацію.")
        return

    signals_sent = 0

    for symbol in symbols:
        closes, highs, lows = get_klines(symbol, "15m", 50)
        if not closes or len(closes) < 30:
            continue

        current_price = closes[-1]
        prev_price = closes[-2]

        # Визначаємо локальний діапазон за попередні 20 свічок (без урахування поточної)
        prev_highs = highs[-21:-1]
        prev_lows = lows[-21:-1]
        
        resistance = max(prev_highs)
        support = min(prev_lows)

        alerts = []

        # 1. Пробій опору вгору
        if prev_price <= resistance and current_price > resistance:
            alerts.append(f"🚀 **{symbol}**: Пробій опору (вище {resistance})! Ціна: {current_price}")

        # 2. Пробій підтримки вниз
        elif prev_price >= support and current_price < support:
            alerts.append(f"⚠️ **{symbol}**: Пробій підтримки (нижче {support})! Ціна: {current_price}")

        # 3. Тест / дотик до опору або підтримки (в межах 0.5%)
        elif abs(current_price - resistance) / resistance <= 0.005:
            alerts.append(f"🔴 **{symbol}**: Ціна тестує опір ({resistance})!")
        elif abs(current_price - support) / support <= 0.005:
            alerts.append(f"🟢 **{symbol}**: Ціна тестує підтримку ({support})!")

        # 4. Імпульсний рух (різка зміна ціни за останні 3 свічки більш ніж на 3%)
        change_3c = (current_price - closes[-4]) / closes[-4] * 100
        if change_3c > 3.0:
            alerts.append(f"📈 **{symbol}**: Сильний бичачий імпульс (+{change_3c:.2f}%) за годину!")
        elif change_3c < -3.0:
            alerts.append(f"📉 **{symbol}**: Сильний ведмежий імпульс ({change_3c:.2f}%) за годину!")

        for alert in alerts:
            print(f"Надсилаю сигнал: {alert}")
            send_discord_alert(alert)
            signals_sent += 1
            
        time.sleep(0.05)

    print(f"--- Сканування завершено. Надіслано сигналів: {signals_sent} ---")

def main():
    print("Головний цикл запущено.")
    while True:
        try:
            analyze_market()
        except Exception as e:
            print(f"Помилка в основному циклі: {e}")
        # Пауза між скануваннями (наприклад, 5 хвилин для швидшої реакції)
        time.sleep(300)

if __name__ == "__main__":
    main>
    
