import time
import requests
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import math

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

send_discord_alert("🟢 **Сканер 15m оновлено: логіка Боковик/Тренд (Боллінджер + Об'єм) активована!**")

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

def calculate_bollinger_bands(data, period=20, num_std=2.0):
    if len(data) < period:
        return [], [], []
    middle, upper, lower = [], [], []
    for i in range(period - 1, len(data)):
        window = data[i - period + 1 : i + 1]
        m = sum(window) / period
        variance = sum((x - m) ** 2 for x in window) / period
        std = math.sqrt(variance)
        middle.append(m)
        upper.append(m + (num_std * std))
        lower.append(m - (num_std * std))
    return middle, upper, lower

def analyze_market():
    symbols = get_top_volatile_symbols(50)
    signals_found = 0

    for symbol in symbols:
        try:
            closes, opens, highs, lows, volumes = get_klines(symbol, "15m", 50)
            if not closes or len(closes) < 40:
                time.sleep(0.05)
                continue

            middle, upper, lower = calculate_bollinger_bands(closes, period=20, num_std=2.0)
            if not upper or len(upper) < 3:
                continue

            current_close = closes[-1]
            prev_close = closes[-2]
            current_open = opens[-1]
            current_volume = volumes[-1]
            
            recent_vols = volumes[-20:-1]
            avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else current_volume

            # Ширина каналу Боллінджера у відсотках (показує стиснення / боковик)
            bb_width = (upper[-1] - lower[-1]) / middle[-1] * 100
            prev_bb_width = (upper[-2] - lower[-2]) / middle[-2] * 100

            # Визначаємо чи був боковик (вузькі смуги, наприклад ширина < 3.5%)
            is_tight_range = prev_bb_width < 3.5

            candle_body_pct = abs(current_close - current_open) / current_close * 100
            alerts = []

            # 1. ВИХІД З БОВОВИКА (НАКОПИЧЕННЯ) ВГОРУ
            if is_tight_range and prev_close <= upper[-2] and current_close > upper[-1] and current_volume >= avg_vol * 1.5:
                alerts.append(f"📦🚀 **{symbol} (15m)**: **Вихід з боковика ВГОРУ**! Імпульс на об'ємі (ціна: {current_close:.4f}, ширина була: {prev_bb_width:.2f}%)")

            # 2. ВИХІД З БОВИКА ВНИЗ
            elif is_tight_range and prev_close >= lower[-2] and current_close < lower[-1] and current_volume >= avg_vol * 1.5:
                alerts.append(f"📦⚠️ **{symbol} (15m)**: **Вихід з боковика ВНИЗ**! Пробій підтримки (ціна: {current_close:.4f}, ширина була: {prev_bb_width:.2f}%)")

            # 3. СИЛЬНИЙ ТРЕНДОВИЙ РУХ (розширення каналу)
            elif not is_tight_range and current_close > upper[-1] and candle_body_pct <= 3.0 and current_volume >= avg_vol * 1.3:
                alerts.append(f"📈 **{symbol} (15m)**: **Тренд угору (Пробій стінки)** (ціна: {current_close:.4f})")

            elif not is_tight_range and current_close < lower[-1] and candle_body_pct <= 3.0 and current_volume >= avg_vol * 1.3:
                alerts.append(f"📉 **{symbol} (15m)**: **Тренд униз (Пробій стінки)** (ціна: {current_close:.4f})")

            for alert in alerts:
                send_discord_alert(alert)
                signals_found += 1
                time.sleep(0.4)
                
        except Exception as e:
            pass
            
        time.sleep(0.1)

    if signals_found > 0:
        send_discord_alert(f"⏱️ **Цикл завершено (15m)**: знайдено структурних сигналів: {signals_found}")

def main():
    while True:
        try:
            analyze_market()
        except Exception as e:
            print(f"Помилка: {e}")
        time.sleep(120)

if __name__ == "__main__":
    main()
                
