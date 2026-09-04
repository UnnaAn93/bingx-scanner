import time
import requests
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

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

send_discord_alert("🟢 **Сканер оновлено: додано детектор швидкого тренду (1-2 оновлення хаїв)!**")

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

def get_klines(symbol, interval="15m", limit=30):
    try:
        url = f"https://open-api.bingx.com/openApi/swap/v1/quote/klines?symbol={symbol}&interval={interval}&limit={limit}"
        response = requests.get(url, timeout=4)
        data = response.json()
        if data.get("code") == 0 and data.get("data"):
            closes = [float(c["close"]) for c in data["data"]]
            opens = [float(c["open"]) for c in data["data"]]
            highs = [float(c["high"]) for c in data["data"]]
            lows = [float(c["low"]) for c in data["data"]]
            return closes, opens, highs, lows
    except Exception as e:
        pass
    return None, None, None, None

def analyze_market():
    print("--- Початок сканування ринку (тренд + імпульси) ---")
    symbols = get_bingx_symbols()
    print(f"Отримано активних пар для перевірки: {len(symbols)}")

    if not symbols:
        return

    signals_sent = 0

    for symbol in symbols:
        closes, opens, highs, lows = get_klines(symbol, "15m", 30)
        if not closes or len(closes) < 10:
            continue

        current_close = closes[-1]
        current_open = opens[-1]
        prev_close = closes[-2]
        prev2_close = closes[-3]
        
        current_high = highs[-1]
        prev_high = highs[-2]
        prev2_high = highs[-3]
        
        current_low = lows[-1]
        prev_low = lows[-2]

        lookback = min(15, len(closes) - 2)
        prev_highs = highs[-(lookback+1):-1]
        prev_lows = lows[-(lookback+1):-1]
        
        resistance = max(prev_highs)
        support = min(prev_lows)

        alerts = []

        # 1. Пробій опору / підтримки тілом
        if prev_close <= resistance and current_close > resistance:
            alerts.append(f"🚀 **{symbol}**: Пробій опору ({resistance})! Ціна: {current_close}")
        elif prev_close >= support and current_close < support:
            alerts.append(f"⚠️ **{symbol}**: Пробій підтримки ({support})! Ціна: {current_close}")
        
        # 2. Зняття ліквідності (шпилька)
        elif current_high > resistance and current_close <= resistance:
            alerts.append(f"🎣 **{symbol}**: Зняття ліквідності зверху (вище {resistance})")
        elif current_low < support and current_close >= support:
            alerts.append(f"🎣 **{symbol}**: Зняття ліквідності знизу (нижче {support})")

        # 3. Імпульс по свічці
        body_change = (current_close - current_open) / current_open * 100
        step_change = (current_close - prev_close) / prev_close * 100

        if body_change >= 3.5 or step_change >= 3.5:
            alerts.append(f"🔥 **{symbol}**: Потужний імпульс росту +{max(body_change, step_change):.2f}%! Ціна: {current_close}")
        elif body_change <= -3.5 or step_change <= -3.5:
            alerts.append(f"🩸 **{symbol}**: Різкий дамп {min(body_change, step_change):.2f}%! Ціна: {current_close}")

        # 4. Трендовий детектер: 2 поспіль зелені свічки з оновленням хаїв та вищих мінімумів (HH + HL)
        if (current_close > prev_close and prev_close > prev2_close) and \
           (current_high > prev_high and prev_high > prev2_high) and \
           (current_low > prev_low):
            trend_change = (current_close - prev2_close) / prev2_close * 100
            if trend_change >= 3.0: # Сумарний рух за ці 2 свічки від мінімуму попередньої
                alerts.append(f"📈 **{symbol}**: Постійне оновлення хаїв (Тренд HH/HL) +{trend_change:.2f}%! Ціна: {current_close}")

        for alert in alerts:
            print(f"Надсилаю сигнал: {alert}")
            send_discord_alert(alert)
            signals_sent += 1
            
        time.sleep(0.01)

    print(f"--- Сканування завершено. Надіслано сигналів: {signals_sent} ---")

def main():
    print("Головний цикл запущено.")
    while True:
        try:
            analyze_market()
        except Exception as e:
            print(f"Помилка в основному циклі: {e}")
        time.sleep(180)

if __name__ == "__main__":
    main()
    
