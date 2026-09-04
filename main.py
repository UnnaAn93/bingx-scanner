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

send_discord_alert("🟢 **Сканер BingX оновлено: фільтр імпульсів піднято до 3%!**")

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

def self_ping():
    port = int(os.environ.get("PORT", 10000))
    url = f"http://127.0.0.1:{port}"
    while True:
        try:
            requests.get(url, timeout=3)
        except:
            pass
        time.sleep(300)

threading.Thread(target=self_ping, daemon=True).start()

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

def get_klines(symbol, interval="5m", limit=30):
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
            return closes, opens, highs, lows
    except:
        pass
    return None, None, None, None

def analyze_market():
    symbols = get_top_volatile_symbols(60)
    signals_found = 0

    for symbol in symbols:
        try:
            closes, opens, highs, lows = get_klines(symbol, "5m", 30)
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
            resistance = max(highs[-(lookback+1):-1])
            support = min(lows[-(lookback+1):-1])

            alerts = []

            if prev_close <= resistance and current_close > resistance:
                alerts.append(f"🚀 **{symbol} (5m)**: Пробій опору ({resistance})! Ціна: {current_close}")
            elif prev_close >= support and current_close < support:
                alerts.append(f"⚠️ **{symbol} (5m)**: Пробій підтримки ({support})! Ціна: {current_close}")
            
            elif current_high > resistance and current_close <= resistance:
                alerts.append(f"🎣 **{symbol} (5m)**: Зняття ліквідності зверху (вище {resistance})")
            elif current_low < support and current_close >= support:
                alerts.append(f"🎣 **{symbol} (5m)**: Зняття ліквідності знизу (нижче {support})")

            # Збільшено поріг до 3.0%
            body_change = (current_close - current_open) / current_open * 100
            step_change = (current_close - prev_close) / prev_close * 100

            if body_change >= 3.0 or step_change >= 3.0:
                alerts.append(f"🔥 **{symbol} (5m)**: Потужний імпульс росту +{max(body_change, step_change):.2f}%! Ціна: {current_close}")
            elif body_change <= -3.0 or step_change <= -3.0:
                alerts.append(f"🩸 **{symbol} (5m)**: Жорсткий дамп {min(body_change, step_change):.2f}%! Ціна: {current_close}")

            # Тренд від 2%
            if (current_close > prev_close and prev_close > prev2_close) and \
               (current_high > prev_high and prev_high > prev2_high) and \
               (current_low > prev_low):
                trend_change = (current_close - prev2_close) / prev2_close * 100
                if trend_change >= 2.0:
                    alerts.append(f"📈 **{symbol} (5m)**: Сильний тренд HH/HL +{trend_change:.2f}%! Ціна: {current_close}")

            flat_highs = highs[-7:]
            flat_lows = lows[-7:]
            channel_range = (max(flat_highs) - min(flat_lows)) / current_close * 100
            if channel_range <= 0.3:
                alerts.append(f"🛏️ **{symbol} (5m)**: Вузький флет у коридорі {channel_range:.2f}%")

            for alert in alerts:
                send_discord_alert(alert)
                signals_found += 1
                time.sleep(0.3)
                
        except Exception as e:
            pass
            
        time.sleep(0.01)

    # Звіт надсилаємо тільки якщо знайдено хоч щось, щоб не забивати чат нулями
    if signals_found > 0:
        send_discord_alert(f"🔄 **Цикл завершено**: знайдено сильних сигналів: {signals_found}")

def main():
    while True:
        try:
            analyze_market()
        except Exception as e:
            print(f"Помилка в головному циклі: {e}")
        time.sleep(60)

if __name__ == "__main__":
    main()
    
