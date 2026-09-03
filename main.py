import time
import requests
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# Безпечне зчитування вебхука з налаштувань Render
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

def send_discord_alert(message):
    if not DISCORD_WEBHOOK_URL:
        print("Помилка: не задано DISCORD_WEBHOOK_URL у змінних середовища!")
        return
    try:
        payload = {"content": message}
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        print(f"Помилка відправки у Discord: {e}")

send_discord_alert("🟢 **Сканер оновлено: додано прямий детектор потужних імпульсів свічки!**")

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
            symbols = [item["symbol"] for item in data["data"] if item.get("status"] == 1 and item["symbol"].endswith("-USDT")]
            return symbols[:500]
    except Exception as e:
        print(f"Помилка отримання списку пар: {e}")
    return []

def get_klines(symbol, interval="15m", limit=30):
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
    print("--- Початок сканування ринку (з імпульсним фільтром) ---")
    symbols = get_bingx_symbols()
    print(f"Отримано активних пар для перевірки: {len(symbols)}")

    if not symbols:
        print("Список пар пустий! Пропускаємо ітерацію.")
        return

    signals_sent = 0

    for symbol in symbols:
        closes, highs, lows = get_klines(symbol, "15m", 30)
        if not closes or len(closes) < 10:
            continue

        current_close = closes[-1]
        prev_close = closes[-2]
        current_high = highs[-1]
        current_low = lows[-1]

        # Визначаємо межі короткого діапазону (останні 15 свічок)
        lookback = min(15, len(closes) - 2)
        prev_highs = highs[-(lookback+1):-1]
        prev_lows = lows[-(lookback+1):-1]
        
        resistance = max(prev_highs)
        support = min(prev_lows)

        alerts = []

        # 1. Повноцінний пробій рівнів боковика
        if prev_close <= resistance and current_close > resistance:
            alerts.append(f"🚀 **{symbol}**: Пробій опору ({resistance}) тілом! Ціна: {current_close}")
        elif prev_close >= support and current_close < support:
            alerts.append(f"⚠️ **{symbol}**: Пробій підтримки ({support}) тілом! Ціна: {current_close}")
        
        # 2. Зняття ліквідності (шпилька за рівень з поверненням всередину)
        elif current_high > resistance and current_close <= resistance:
            alerts.append(f"🎣 **{symbol}**: Зняття ліквідності зверху (шпилька вище {resistance}, закрились у діапазоні)")
        elif current_low < support and current_close >= support:
            alerts.append(f"🎣 **{symbol}**: Зняття ліквідності знизу (шпилька нижче {support}, закрились у діапазоні)")

        # 3. Прямий детектор волатильності: якщо свічка летить більш ніж на 4% за 15 хвилин
        candle_change = (current_close - prev_close) / prev_close * 100
        if candle_change >= 4.0:
            alerts.append(f"🔥 **{symbol}**: Потужний імпульс свічки +{candle_change:.2f}%! Ціна: {current_close}")
        elif candle_change <= -4.0:
            alerts.append(f"🩸 **{symbol}**: Різкий дамп свічки {candle_change:.2f}%! Ціна: {current_close}")

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
        time.sleep(300)

if __name__ == "__main__":
    main()
                      
