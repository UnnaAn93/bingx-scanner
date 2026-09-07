import time
import requests
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

def send_discord_alert(message):
    if not DISCORD_WEBHOOK_URL:
        print("Помилка: не задано DISCORD_WEBHOOK_URL!")
        return
    try:
        payload = {"content": message}
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        print(f"Помилка відправки у Discord: {e}")

send_discord_alert("🟢 **Сканер 15m оновлено: налаштовано фокус на пробій та закріплення тілом вище/нижче меж боковика!**")

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Scanner is active 24/7!")
    def log_message(self, format, *args):
        pass

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

threading.Thread(target=run_web_server, daemon=True).start()

def self_ping_worker():
    time.sleep(10)
    app_url = RENDER_EXTERNAL_URL 
    while True:
        try:
            if app_url:
                requests.get(app_url, timeout=5)
        except:
            pass
        time.sleep(300)

threading.Thread(target=self_ping_worker, daemon=True).start()

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0"})

def get_top_volatile_symbols(top_n=50):
    url = "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"
    try:
        response = session.get(url, timeout=5)
        data = response.json()
        tickers = data.get("data", [])
        
        valid_tickers = []
        for t in tickers:
            sym = t.get("symbol", "")
            if sym.endswith("-USDT"):
                base_part = sym.split("-")[0]
                if "2USD" in base_part:
                    continue
                try:
                    change = abs(float(t.get("priceChangePercent", 0)))
                    valid_tickers.append((sym, change))
                except:
                    pass
        
        valid_tickers.sort(key=lambda x: x[1], reverse=True)
        return [item[0] for item in valid_tickers[:top_n]]
    except Exception as e:
        print(f"Помилка отримання волатильних пар: {e}")
        return ["BTC-USDT", "ETH-USDT", "SOL-USDT"]

def get_klines(symbol, interval="15m", limit=50):
    url = f"https://open-api.bingx.com/openApi/swap/v2/quote/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        response = session.get(url, timeout=4)
        res_data = response.json()
        data = res_data.get("data", [])
        if isinstance(data, list) and len(data) > 0:
            closes = [float(c.get("close", 0)) for c in data]
            opens = [float(c.get("open", 0)) for c in data]
            highs = [float(c.get("high", 0)) for c in data]
            lows = [float(c.get("low", 0)) for c in data]
            volumes = [float(c.get("volume", 0)) for c in data]
            return closes, opens, highs, lows, volumes
    except:
        pass
    return None, None, None, None, None

def analyze_market():
    symbols = get_top_volatile_symbols(50)
    signals_found = 0

    BOX_LENGTHS = [15, 20, 25, 30, 40]

    for symbol in symbols:
        try:
            closes, opens, highs, lows, volumes = get_klines(symbol, "15m", 50)
            if not closes or len(closes) < 45:
                time.sleep(0.05)
                continue

            current_close = closes[-1]
            current_open = opens[-1]
            prev_close = closes[-2]
            current_volume = volumes[-1]

            alerts = []
            signal_triggered = False

            # Шукаємо чіткий пробій та закріплення тілом свічки вище/нижче боковика
            for L in BOX_LENGTHS:
                if len(closes) < L + 2:
                    continue

                window_highs = highs[-(L+2):-2]
                window_lows = lows[-(L+2):-2]
                window_vols = volumes[-(L+2):-2]

                box_top = max(window_highs)
                box_bottom = min(window_lows)
                box_width_pct = (box_top - box_bottom) / current_close * 100
                avg_box_vol = sum(window_vols) / len(window_vols) if window_vols else current_volume

                if box_width_pct <= 5.5:
                    # Закріплення вище верхньої межі (пробій + утримання рівня)
                    if prev_close > box_top and current_close >= box_top and current_volume >= avg_box_vol * 1.1:
                        alerts.append(f"🎯 **{symbol} (15m)**: **Закріплення вище боковика ВГОРУ** (опір {box_top:.4f})! Готово до руху.")
                        signal_triggered = True
                        break
                    # Закріплення нижче нижньої межі
                    elif prev_close < box_bottom and current_close <= box_bottom and current_volume >= avg_box_vol * 1.1:
                        alerts.append(f"⚠️ **{symbol} (15m)**: **Закріплення нижче боковика ВНИЗ** (підтримка {box_bottom:.4f})! Шлях вільний.")
                        signal_triggered = True
                        break

            for alert in alerts:
                send_discord_alert(alert)
                signals_found += 1
                time.sleep(0.4)
                
        except Exception as e:
            pass
            
        time.sleep(0.1)

    if signals_found > 0:
        send_discord_alert(f"⏱️ **Цикл завершено (15m)**: знайдено сигналів: {signals_found}")

def main():
    while True:
        try:
            analyze_market()
        except Exception as e:
            print(f"Помилка: {e}")
        time.sleep(120)

if __name__ == "__main__":
    main()
            
