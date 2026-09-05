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

send_discord_alert("🟢 **Сканер 15m оновлено: інтегровано розрахунок проторгованих об'ємів (Volume Profile / POC)!**")

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

def get_klines(symbol, interval="15m", limit=35):
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

def calculate_volume_profile(highs, lows, volumes, bins=15):
    """Будує примітивний профіль об'ємів для знаходження POC та границь зони інтересу"""
    if not highs or not lows or not volumes:
        return None, None, None
    
    min_price = min(lows)
    max_price = max(highs)
    if min_price == max_price:
        return min_price, min_price, max_price

    step = (max_price - min_price) / bins
    price_bins = [min_price + i * step for i in range(bins + 1)]
    bin_volumes = [0.0] * bins

    # Розподіляємо об'єм кожної свічки по цінових бінах
    for h, l, v in zip(highs, lows, volumes):
        if h == l:
            continue
        for i in range(bins):
            b_low = price_bins[i]
            b_high = price_bins[i+1]
            # Перетин свічки з ціновим діапазоном біна
            overlap_low = max(l, b_low)
            overlap_high = min(h, b_high)
            if overlap_low < overlap_high:
                fraction = (overlap_high - overlap_low) / (h - l)
                bin_volumes[i] += v * fraction

    # Знаходимо POC (бін з максимальним об'ємом)
    max_vol_idx = bin_volumes.index(max(bin_volumes))
    poc_price = (price_bins[max_vol_idx] + price_bins[max_vol_idx+1]) / 2

    # Визначаємо межі активного торгової зони (Value Area / Рендж по об'ємах)
    box_low = price_bins[0]
    box_high = price_bins[-1]
    
    # Шукаємо реально заторговану зону (відкидаємо крайні пусті хвости)
    active_bins = [i for i, vol in enumerate(bin_volumes) if vol > (max(bin_volumes) * 0.15)]
    if active_bins:
        box_low = price_bins[min(active_bins)]
        box_high = price_bins[max(active_bins) + 1]

    return poc_price, box_low, box_high

def analyze_market():
    symbols = get_top_volatile_symbols(50)
    signals_found = 0

    for symbol in symbols:
        try:
            closes, opens, highs, lows, volumes = get_klines(symbol, "15m", 35)
            if not closes or len(closes) < 20:
                time.sleep(0.05)
                continue

            current_close = closes[-1]
            current_open = opens[-1]
            prev_close = closes[-2]
            current_volume = volumes[-1]

            avg_volume = sum(volumes[-25:-1]) / 24 if len(volumes) >= 25 else sum(volumes) / len(volumes)

            # Беремо останні 20 свічок для побудови профілю об'ємів боковика
            p_highs = highs[-21:-1]
            p_lows = lows[-21:-1]
            p_vols = volumes[-21:-1]

            poc, box_bottom, box_top = calculate_volume_profile(p_highs, p_lows, p_vols)
            if not poc:
                continue

            box_width_pct = (box_top - box_bottom) / current_close * 100
            alerts = []

            # Перевіряємо, чи є це сформованим діапазоном (боковиком) за об'ємами
            is_consolidation = box_width_pct <= 4.0

            if is_consolidation:
                # Зона консолідації навколо POC
                recent_channel = (max(highs[-5:]) - min(lows[-5:])) / current_close * 100
                if recent_channel <= 1.8 and current_volume < avg_volume * 0.9:
                    alerts.append(f"🛏️ **{symbol} (15m)**: Зона боковика / POC на {poc:.4f} (ширина {box_width_pct:.2f}%)")
                
                # Вихід з наторгованого боковика ВГОРУ
                elif prev_close <= box_top and current_close > box_top and current_volume > avg_volume * 1.3:
                    alerts.append(f"🚀 **{symbol} (15m)**: Вихід з боковика ВГОРУ вище об'ємів ({box_top:.4f})! Ціна: {current_close}")

                # Вихід з наторгованого боковика ВНИЗ 
                elif prev_close >= box_bottom and current_close < box_bottom and current_volume > avg_volume * 1.3:
                    alerts.append(f"⚠️ **{symbol} (15m)**: Вихід з боковика ВНИЗ нижче об'ємів ({box_bottom:.4f})! Ціна: {current_close}")

            # Імпульси від 3.5%
            body_change = (current_close - current_open) / current_open * 100
            step_change = (current_close - prev_close) / prev_close * 100

            if body_change >= 3.5 or step_change >= 3.5:
                alerts.append(f"🔥 **{symbol} (15m)**: Імпульс росту +{max(body_change, step_change):.2f}%!")
            elif body_change <= -3.5 or step_change <= -3.5:
                alerts.append(f"🩸 **{symbol} (15m)**: Дамп {min(body_change, step_change):.2f}%!")

            for alert in alerts:
                send_discord_alert(alert)
                signals_found += 1
                time.sleep(0.4)
                
        except Exception as e:
            pass
            
        time.sleep(0.1)

    if signals_found > 0:
        send_discord_alert(f"🔄 **Цикл завершено (15m)**: знайдено сигналів: {signals_found}")

def main():
    while True:
        try:
            analyze_market()
        except Exception as e:
            print(f"Помилка: {e}")
        time.sleep(120)

if __name__ == "__main__":
    main()
    
