import asyncio
import aiohttp
import os
import time
import hmac
import hashlib
import base64
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# --- НАЛАШТУВАННЯ ПАРАМЕТРІВ СКАНУВАННЯ ---
VOLUME_MULTIPLIER_ENTRY = 2.0     
VOLUME_MULTIPLIER_EXIT = 3.0      
VOLUME_DECREASE_EXIT = 0.5        

APPROACH_PERCENT = 0.003          
TIMEFRAME = "15m"                 
LIMIT_CANDLES = 60                # Збільшено історію для коректного розрахунку EMA 50
TOP_COINS_LIMIT = 150             
MIN_24H_VOLUME_USDT = 5_000_000   
COOLDOWN_SECONDS = 300            

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1551243480989433927/cIcSwvFUvrh7vnRbXcCx9c8pRMBkyX6VBNVbsEQBp6PWc7aJmXZsYnLnU79Rh8JJaKMF")
RENDER_URL = os.environ.get("RENDER_URL", "https://bingx-scanner-djbf.onrender.com")

BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_SECRET_KEY = os.environ.get("BITGET_SECRET_KEY", "")
BITGET_PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "")

BITGET_BASE_URL = "https://api.bitget.com"

last_alert_time = {}


def calculate_ema(closes, period=50):
    if len(closes) < period:
        return sum(closes) / len(closes)
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def get_bitget_signature_v1(timestamp, method, request_path, body=""):
    message = timestamp + method.upper() + request_path + body
    mac = hmac.new(bytes(BITGET_SECRET_KEY, encoding='utf-8'), bytes(message, encoding='utf-8'), digestmod=hashlib.sha256)
    return base64.b64encode(mac.digest()).decode('utf-8')


async def fetch_open_positions(session):
    if not BITGET_API_KEY or not BITGET_SECRET_KEY or not BITGET_PASSPHRASE:
        return {} 

    method = "GET"
    path = "/api/mix/v1/position/allPosition"
    query_string = "productType=umcbl"
    request_uri = f"{path}?{query_string}"
    url = f"{BITGET_BASE_URL}{request_uri}"
    
    timestamp = str(int(time.time() * 1000))
    sign = get_bitget_signature_v1(timestamp, method, request_uri)
    
    headers = {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_PASSPHRASE,
        "Content-Type": "application/json",
        "locale": "en-US"
    }
    
    try:
        async with session.get(url, headers=headers, timeout=5) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("code") == "00000":
                    positions = {}
                    for pos in data.get("data", []):
                        total_size = float(pos.get("total", 0) or pos.get("holdPosition", 0))
                        if total_size > 0:
                            symbol = pos.get("symbol")
                            hold_side = pos.get("holdSide") or pos.get("positionSide", "short")
                            entry_price = float(pos.get("averageOpenPrice", 0) or pos.get("openPriceAvg", 0))
                            unrealized_pnl = float(pos.get("unrealizedPL", 0) or pos.get("achievedProfits", 0))
                            
                            positions[symbol] = {
                                "side": hold_side,
                                "size": total_size,
                                "entry_price": entry_price,
                                "pnl": unrealized_pnl
                            }
                    return positions
    except Exception as e:
        print(f"Помилка запиту позицій: {e}")
    return {}


async def fetch_top_bitget_symbols(session):
    url = f"{BITGET_BASE_URL}/api/v2/mix/market/tickers?productType=USDT-FUTURES"
    try:
        async with session.get(url, timeout=5) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("code") == "00000":
                    list_tickers = data.get("data", [])
                    usdt_tickers = []
                    for t in list_tickers:
                        symbol = t.get("symbol", "")
                        if symbol.endswith("USDT"):
                            try:
                                quote_vol = float(t.get("usdtVolume", 0))
                                if quote_vol >= MIN_24H_VOLUME_USDT:
                                    usdt_tickers.append((symbol, quote_vol))
                            except Exception:
                                continue
                    usdt_tickers.sort(key=lambda x: x[1], reverse=True)
                    return [item[0] for item in usdt_tickers[:TOP_COINS_LIMIT]]
    except Exception as e:
        print(f"Помилка при отриманні списку монет: {e}")
    return []


async def fetch_kline_data(session, symbol):
    url = f"{BITGET_BASE_URL}/api/v2/mix/market/candles"
    params = {
        "symbol": symbol,
        "productType": "USDT-FUTURES",
        "granularity": TIMEFRAME,
        "limit": str(LIMIT_CANDLES)
    }
    try:
        async with session.get(url, params=params, timeout=4) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("code") == "00000":
                    raw_list = data.get("data", [])
                    raw_list.sort(key=lambda x: int(x[0]))
                    formatted = []
                    for item in raw_list:
                        formatted.append({
                            "open": float(item[1]),
                            "high": float(item[2]),
                            "low": float(item[3]),
                            "close": float(item[4]),
                            "volume": float(item[5])
                        })
                    return formatted
    except Exception:
        pass
    return None


async def send_to_discord(session, webhook_url, message):
    if not webhook_url:
        return
    payload = {"content": message}
    try:
        async with session.post(webhook_url, json=payload) as response:
            if response.status != 200:
                print(f"Помилка відправки у Discord: {response.status}")
    except Exception as e:
        print(f"Виняток при відправці у Discord: {e}")


async def check_single_coin(session, symbol, open_positions, discord_webhook_url):
    current_time = time.time()
    if symbol in last_alert_time and current_time - last_alert_time[symbol] < COOLDOWN_SECONDS:
        return

    kline_data = await fetch_kline_data(session, symbol)
    if not kline_data or len(kline_data) < 55:
        return

    try:
        opens = [x['open'] for x in kline_data]
        highs = [x['high'] for x in kline_data]
        lows = [x['low'] for x in kline_data]
        closes = [x['close'] for x in kline_data]
        volumes = [x['volume'] for x in kline_data]
        
        current_volume = volumes[-1]
        if current_volume <= 0:
            return

        avg_volume = sum(volumes[:-1]) / (len(volumes) - 1)
        if avg_volume <= 0:
            return
        
        current_price = closes[-1]
        current_open = opens[-1]
        surge_percent = int((current_volume / avg_volume - 1) * 100)

        # Розрахунок трендового фільтра EMA 50
        ema_50 = calculate_ema(closes, period=50)

        # Рівні підтримки / опору
        historical_prices = []
        for i in range(len(closes) - 1):
            historical_prices.append(lows[i])
            historical_prices.append(highs[i])

        clusters = []
        cluster_threshold = 0.001
        sorted_prices = sorted(historical_prices)
        if sorted_prices:
            current_cluster = [sorted_prices[0]]
            for p in sorted_prices[1:]:
                if abs(p - current_cluster[-1]) / current_cluster[-1] <= cluster_threshold:
                    current_cluster.append(p)
                else:
                    if len(current_cluster) >= 2:
                        clusters.append(sum(current_cluster) / len(current_cluster))
                    current_cluster = [p]
            if len(current_cluster) >= 2:
                clusters.append(sum(current_cluster) / len(current_cluster))

        lower_clusters = [c for c in clusters if c < current_price]
        upper_clusters = [c for c in clusters if c > current_price]

        support_level = max(lower_clusters) if lower_clusters else min(lows[:-1])
        resistance_level = min(upper_clusters) if upper_clusters else max(highs[:-1])

        # --- 1. СУПРОВІД ПОЗИЦІЙ ---
        if symbol in open_positions:
            pos_info = open_positions[symbol]
            pos_side = pos_info["side"].upper()
            entry_price = pos_info["entry_price"]
            pnl = pos_info["pnl"]

            is_volume_spike_exit = current_volume >= (avg_volume * VOLUME_MULTIPLIER_EXIT)
            is_volume_drop_exit = current_volume <= (avg_volume * VOLUME_DECREASE_EXIT)

            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            pnl_text = f"+{pnl:.2f} USDT" if pnl >= 0 else f"{pnl:.2f} USDT"

            if "LONG" in pos_side and resistance_level > 0:
                dist_to_res = (resistance_level - current_price) / resistance_level
                if 0 <= dist_to_res <= APPROACH_PERCENT and (is_volume_spike_exit or is_volume_drop_exit):
                    exit_reason = "Кульмінаційний сплеск (>=3x)" if is_volume_spike_exit else "Затухання об'ємів"
                    alert_message = (
                        f"🔔 МЕНЕДЖЕР [ЛОНГ 15m]: `{symbol}`\n"
                        f"• Вхід: `{entry_price}` | Поточна: `{current_price}`\n"
                        f"• PnL: {pnl_emoji} **{pnl_text}**\n"
                        f"• Вихід: 🚨 **Підхід до опору (`{resistance_level}`) + {exit_reason}!**"
                    )
                    last_alert_time[symbol] = current_time
                    await send_to_discord(session, discord_webhook_url, alert_message)
                    return

            elif "SHORT" in pos_side and support_level > 0:
                dist_to_sup = (current_price - support_level) / support_level
                if 0 <= dist_to_sup <= APPROACH_PERCENT and (is_volume_spike_exit or is_volume_drop_exit):
                    exit_reason = "Кульмінаційний сплеск (>=3x)" if is_volume_spike_exit else "Затухання об'ємів"
                    alert_message = (
                        f"🔔 МЕНЕДЖЕР [ШОРТ 15m]: `{symbol}`\n"
                        f"• Вхід: `{entry_price}` | Поточна: `{current_price}`\n"
                        f"• PnL: {pnl_emoji} **{pnl_text}**\n"
                        f"• Вихід: 🚨 **Підхід до підтримки (`{support_level}`) + {exit_reason}!**"
                    )
                    last_alert_time[symbol] = current_time
                    await send_to_discord(session, discord_webhook_url, alert_message)
                    return

        # --- 2. ПОШУК СИГНАЛІВ З ТРЕНДОВИМ ФІЛЬТРОМ EMA 50 ---
        else:
            is_volume_spike_entry = current_volume >= (avg_volume * VOLUME_MULTIPLIER_ENTRY)
            if not is_volume_spike_entry:
                return

            is_green_candle = current_price >= current_open

            # СИГНАЛ НА ЛОНГ: тільки якщо ціна вище EMA 50 (висхідний/флєтовий контекст)
            if support_level > 0 and is_green_candle and current_price > ema_50:
                distance_to_support = (current_price - support_level) / support_level
                if 0 <= distance_to_support <= APPROACH_PERCENT:
                    alert_message = (
                        f"🟢🎯 **РАННІЙ ЛОНГ [Підтримка 15m]**: `{symbol}`\n"
                        f"• Точка інтересу: 🚀 **Підхід до підтримки по тренду**\n"
                        f"• Ціна: `{current_price}` (Підтримка: `{support_level}`, EMA50: `{ema_50:.2f}`)\n"
                        f"• Об'єм свічки: `+{surge_percent}%` від середнього\n"
                        f"⏳ Сигнал на відскок вгору за трендом!"
                    )
                    last_alert_time[symbol] = current_time
                    await send_to_discord(session, discord_webhook_url, alert_message)
                    return

            # СИГНАЛ НА ШОРТ: тільки якщо ціна нижче EMA 50 (низхідний/флєтовий контекст)
            if resistance_level > 0 and not is_green_candle and current_price < ema_50:
                distance_to_resistance = (resistance_level - current_price) / resistance_level
                if 0 <= distance_to_resistance <= APPROACH_PERCENT:
                    alert_message = (
                        f"🔴🎯 **РАННІЙ ШОРТ [Опір 15m]**: `{symbol}`\n"
                        f"• Точка інтересу: 📉 **Підхід до опору по тренду**\n"
                        f"• Ціна: `{current_price}` (Опір: `{resistance_level}`, EMA50: `{ema_50:.2f}`)\n"
                        f"• Об'єм свічки: `+{surge_percent}%` від середнього\n"
                        f"⏳ Сигнал на відбій вниз за трендом!"
                    )
                    last_alert_time[symbol] = current_time
                    await send_to_discord(session, discord_webhook_url, alert_message)
                    return

    except Exception as e:
        pass


async def self_ping_loop(session):
    while True:
        await asyncio.sleep(240)
        try:
            async with session.get(RENDER_URL, timeout=5) as response:
                pass
        except Exception:
            pass


async def main():
    print("Бот сканує ринок (фільтр EMA 50 + ранній вхід на рівнях 15m)...")
    
    async with aiohttp.ClientSession() as session:
        asyncio.create_task(self_ping_loop(session))
        
        while True:
            start_time = asyncio.get_event_loop().time()
            
            open_positions = await fetch_open_positions(session)
            symbols_list = await fetch_top_bitget_symbols(session)
            
            if symbols_list and DISCORD_WEBHOOK_URL:
                tasks = [check_single_coin(session, symbol, open_positions, DISCORD_WEBHOOK_URL) for symbol in symbols_list]
                await asyncio.gather(*tasks)
            
            elapsed = asyncio.get_event_loop().time() - start_time
            sleep_time = max(1, 10 - elapsed)
            await asyncio.sleep(sleep_time)


class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"EMA Filtered Scanner Bot (15m) is running!")
    
    def log_message(self, format, *args):
        return


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()


if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот зупинений користувачем.")
            
