import time
import requests
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

def send_discord_alert(message):
    if not DISCORD_WEBHOOK_URL:
        print("Помилка: не задано DISCORD_WEBHOOK_URL!")
        return
    try:
        payload = {"content": message}
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        print(f"Помилка відправки у Discord: {e}")

send_discord_alert("🟢 **Сканер оновлено: виправлено точність ретестів та фільтрацію!**")

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

def get_top_volatile_symbols(top_n=60):
    url = "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"
    try:
        response = requests.get(url, timeout=5)
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
        symbols = [item[0] for item in valid_tickers[:top_n]]
        return symbols
    except Exception as e:
        print(f"Помилка отримання волатильних пар з BingX: {e}")
        return ["BTC-USDT", "ETH-USDT", "SOL-USDT"]

def get_klines(symbol, interval="15m", limit=35):
    url = f"https://open-api.bingx.com/openApi/swap/v2/quote/klines?symbol={symbol}&interval={interval}&limit={limit}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, headers=headers, timeout=3)
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
    symbols = get_top_volatile_symbols(60)
    signals_found = 0

    for symbol in symbols:
        try:
            closes, opens, highs, lows, volumes = get_klines(symbol, "15m", 35)
            if not closes or len(closes) < 15:
                continue

            current_close = closes[-1]
            current_open = opens[-1]
            prev_close = closes[-2]
            
            current_high = highs[-1]
            current_low = lows[-1]

            # Визначаємо свіжі рівні підтримки/опору на основі останніх 15 свічок (без зайвої історії)
            lookback = 15
            resistance = max(highs[-(lookback+2):-2])
            support = min(lows[-(lookback+2):-2])

            alerts = []

            # 1. Пробій опору / підтримки
            if prev_close <= resistance and current_close > resistance:
                alerts.append(f"🚀 **{symbol} (15m)**: Пробій опору ({resistance:.4f})! Ціна: {current_close}")
            elif prev_close >= support and current_close < support:
                alerts.append(f"⚠️ **{symbol} (15m)**: Пробій підтримки ({support:.4f})! Ціна: {current_close}")

            # 2. Жорсткий точний ретест (перевіряємо попередню свічку як пробій, а поточну як точний відкат до цього рівня)
            recent_resistance = max(highs[-8:-2]) # свіжий локальний хай
            # Якщо 2 свічки тому був пробій, а зараз ціна опустилася до цього рівня (у межах 0.3%) і відскочила вгору
            if closes[-3] < recent_resistance and closes[-2] >= recent_resistance:
                if current_low <= recent_resistance * 1.003 and current_low >= recent_resistance * 0.995 and current_close > recent_resistance:
                    alerts.append(f"🎯 **{symbol} (15m)**: Ретест свіжого опору ({recent_resistance:.4f}) та відскік!")

            # 3. Імпульси від 3%
            body_change = (current_close - current_open) / current_open * 100
            step_change = (current_close - prev_close) / prev_close * 100

            if body_change >= 3.0 or step_change >= 3.0:
                alerts.append(f"🔥 **{symbol} (15m)**: Імпульс росту +{max(body_change, step_change):.2f}%!")
            elif body_change <= -3.0 or step_change <= -3.0:
                alerts.append(f"🩸 **{symbol} (15m)**: Дамп {min(body_change, step_change):.2f}%!")

            # 4. Боковик за об'ємом
            recent_highs = highs[-6:]
            recent_lows = lows[-6:]
            channel_range = (max(recent_highs) - min(recent_lows)) / current_close * 100
            
            avg_volume_past = sum(volumes[-20:-6]) / 14 if len(volumes) >= 20 else sum(volumes) / len(volumes)
            avg_volume_recent = sum(volumes[-5:]) / 5

            if channel_range <= 0.7 and avg_volume_recent < avg_volume_past * 0.5:
                alerts.append(f"🛏️ **{symbol} (15m)**: Зона консолідації (боковик на знижених об'ємах {channel_range:.2f}%)")

            for alert in alerts:
                send_discord_alert(alert)
                signals_found += 1
                time.sleep(0.3)
                
        except Exception as e:
            pass
            
        time.sleep(0.01)

    if signals_found > 0:
        send_discord_alert(f"🔄 **Цикл завершено (15m)**: знайдено сигналів: {signals_found}")

def main():
    while True:
        try:
            analyze_market()
        except Exception as e:
            print(f"Помилка в головному циклі: {e}")
        time.sleep(120)

if __name__ == "__main__":
    main()
        
