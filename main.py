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

send_discord_alert("🟢 **Сканер 15m оновлено: синтаксичну помилку деплою усунено!**")

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

def get_klines(symbol, interval="15m", limit=60):
    url = f"https://open-api.bingx.com/openApi/swap/v2/quote/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        response = session.get(url, timeout=4)
        res_data = response.json()
        data = res_data.get("data", [])
        if isinstance(data, list) and len(data) > 0:
            closes = [float(c.get("close", 0)) for c in data]
            highs = [float(c.get("high", 0)) for c in data]
            lows = [float(c.get("low", 0)) for c in data]
            volumes = [float(c.get("volume", 0)) for c in data]
            return closes, highs, lows, volumes
    except:
        pass
    return None, None, None, None

def find_clustered_levels(highs, lows, tolerance=0.008):
    resistance_level = None
    support_level = None
    
    local_highs = []
    for i in range(2, len(highs) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            local_highs.append(highs[i])
            
    local_lows = []
    for i in range(2, len(lows) - 2):
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            local_lows.append(lows[i])

    for h in local_highs:
        matches = [x for x in local_highs if abs(x - h) / h <= tolerance]
        if len(matches) >= 2:
            resistance_level = sum(matches) / len(matches)
            break

    for l in local_lows:
        matches = [x for x in local_lows if abs(x - l) / l <= tolerance]
        if len(matches) >= 2:
            support_level = sum(matches) / len(matches)
            break

    return resistance_level, support_level

def analyze_market():
    symbols = get_top_volatile_symbols(50)
    signals_found = 0

    for symbol in symbols:
        try:
            closes, highs, lows, volumes = get_klines(symbol, "15m", 60)
            if not closes or len(closes) < 50:
                time.sleep(0.05)
                continue

            current_close = closes[-1]
            prev_close = closes[-2]
            current_volume = volumes[-1]
            
            recent_vols = volumes[-20:-1]
            avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else current_volume

            res_lvl, sup_lvl = find_clustered_levels(highs, lows)
            alerts = []

            if res_lvl and prev_close <= res_lvl and current_close > res_lvl and current_volume >= avg_vol * 1.4:
                alerts.append(f"🎯🚀 **{symbol} (15m)**: **Пробій рівного опору** ({res_lvl:.4f}) на об'ємі!")

            elif sup_lvl and prev_close >= sup_lvl and current_close < sup_lvl and current_volume >= avg_vol * 1.4:
                alerts.append(f"🎯⚠️ **{symbol} (15m)**: **Пробій рівної підтримки** ({sup_lvl:.4f}) на об'ємі!")

            for alert in alerts:
                send_discord_alert(alert)
                signals_found += 1
                time.sleep(0.4)
                
        except Exception as e:
            pass
            
        time.sleep(0.1)

    if signals_found > 0:
        send_discord_alert(f"⏱️ **Цикл завершено (15m)**: знайдено рівних рівнів: {signals_found}")

def main():
    while True:
        try:
            analyze_market()
        except Exception as e:
            print(f"Помилка: {e}")
        time.sleep(120)

if __name__ == "__main__":
    main()
    
