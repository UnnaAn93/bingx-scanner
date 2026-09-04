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

send_discord_alert("🟢 **Сканер активовано через альтернативний публічний шлюз!**")

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

def get_top_volatile_symbols(top_n=35):
    # Використовуємо публічний шлюз Coincap, який повністю відкритий для будь-яких сервісів і хмар
    try:
        url = "https://api.coincap.io/v2/assets?limit=100"
        response = requests.get(url, timeout=5)
        data = response.json()
        if "data" in data:
            assets = data["data"]
            # Сортуємо за зміною ціни за 24 години (changePercent24Hr)
            valid_assets = []
            for asset in assets:
                try:
                    change = float(asset.get("changePercent24Hr") or 0)
                    symbol = asset.get("symbol") + "USDT"
                    valid_assets.append({"symbol": symbol, "abs_change": abs(change)})
                except:
                    continue
            valid_assets.sort(key=lambda x: x["abs_change"], reverse=True)
            return [item["symbol"] for item in valid_assets[:top_n]]
    except Exception as e:
        print(f"Помилка шлюзу: {e}")
    return []

def get_klines(symbol, interval="5m", limit=30):
    # Оскільки беремо пари з прив'язкою до USDT, тягнемо свічки через загальнодоступний бэкап Binance з заголовками браузера
    clean_symbol = symbol.replace("USDT", "USDT")
    url = f"https://api1.binance.com/api/v3/klines?symbol={clean_symbol}&interval={interval}&limit={limit}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        response = requests.get(url, headers=headers, timeout=3)
        data = response.json()
        if isinstance(data, list) and len(data) > 0:
            closes = [float(c[4]) for c in data]
            opens = [float(c[1]) for c in data]
            highs = [float(c[2]) for c in data]
            lows = [float(c[3]) for c in data]
            return closes, opens, highs, lows
    except:
        pass
    return None, None, None, None

def analyze_market():
    send_discord_alert("🔍 Починаю сканування топ-35 волатильних пар...")
    symbols = get_top_volatile_symbols(35)
    
    if not symbols:
        send_discord_alert("⚠️ Помилка: не вдалося завантажити список через шлюз!")
        return

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

            body_change = (current_close - current_open) / current_open * 100
            step_change = (current_close - prev_close) / prev_close * 100

            if body_change >= 1.0 or step_change >= 1.0:
                alerts.append(f"🔥 **{symbol} (5m)**: Імпульс росту +{max(body_change, step_change):.2f}%! Ціна: {current_close}")
            elif body_change <= -1.0 or step_change <= -1.0:
                alerts.append(f"🩸 **{symbol} (5m)**: Дамп {min(body_change, step_change):.2f}%! Ціна: {current_close}")

            if (current_close > prev_close and prev_close > prev2_close) and \
               (current_high > prev_high and prev_high > prev2_high) and \
               (current_low > prev_low):
                trend_change = (current_close - prev2_close) / prev2_close * 100
                if trend_change >= 0.8:
                    alerts.append(f"📈 **{symbol} (5m)**: Тренд HH/HL +{trend_change:.2f}%! Ціна: {current_close}")

            for alert in alerts:
                send_discord_alert(alert)
                signals_found += 1
                time.sleep(0.4)
                
        except Exception as e:
            pass
            
        time.sleep(0.02)

    send_discord_alert(f"🔄 **Цикл завершено**: перевірено топ-35 пар. Знайдено сигналів: {signals_found}")

def main():
    while True:
        try:
            analyze_market()
        except Exception as e:
            print(f"Помилка в головному циклі: {e}")
        time.sleep(90)

if __name__ == "__main__":
    main()
    
