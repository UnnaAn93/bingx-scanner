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
VOLUME_MULTIPLIER_ENTRY = 2.2     # Сплеск для відкриття позиції
VOLUME_MULTIPLIER_EXIT = 3.0      # Сильний сплеск (кульмінація) для виходу з позиції
VOLUME_DECREASE_EXIT = 0.5        # Зниження об'єму (затухання) для виходу з позиції

APPROACH_PERCENT = 0.007          
TIMEFRAME = "15m"                 
LIMIT_CANDLES = 30                
TOP_COINS_LIMIT = 150             
MIN_24H_VOLUME_USDT = 5_000_000   
COOLDOWN_SECONDS = 300            

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
RENDER_URL = os.environ.get("RENDER_URL", "https://bingx-scanner-djbf.onrender.com")

BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_SECRET_KEY = os.environ.get("BITGET_SECRET_KEY", "")
BITGET_PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "")

BITGET_BASE_URL = "https://api.bitget.com"

last_alert_time = {}


def get_bitget_signature(timestamp, method, request_path, body=""):
    message = str(timestamp) + method.upper() + request_path + body
    mac = hmac.new(bytes(BITGET_SECRET_KEY, encoding='utf-8'), bytes(message, encoding='utf-8'), digestmod=hashlib.sha256)
    result = base64.b64encode(mac.digest()).decode('utf-8')
    return result


async def fetch_open_positions(session):
    """Отримує список відкритих ф'ючерсних позицій з Bitget разом з поточним PnL"""
    if not BITGET_API_KEY or not BITGET_SECRET_KEY or not BITGET_PASSPHRASE:
        return {} 

    method = "GET"
    path = "/api/v2/mix/position/all-pos-list"
    params = "?productType=USDT-FUTURES"
    url = f"{BITGET_BASE_URL}{path}{params}"
    
    timestamp = str(int(time.time() * 1000))
    sign = get_bitget_signature(timestamp, method, f"/api/v2/mix/position/all-pos-list{params}")
    
    headers = {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_PASSPHRASE,
        "Content-Type": "application/json"
    }
    
    try:
        async with session.get(url, headers=headers, timeout=5) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("code") == "00000":
                    positions = {}
                    for pos in data.get("data", []):
                        total_size = float(pos.get("total", 0))
                        if total_size > 0:
                            symbol = pos.get("symbol")
                            hold_side = pos.get("holdSide") # long або short
                            entry_price = float(pos.get("averageOpenPrice", 0))
                            unrealized_pnl = float(pos.get("unrealizedPL", 0)) # поточний прибуток/збиток у USDT
                            
                            positions[symbol] = {
                                "side": hold_side,
                                "size": total_size,
                                "entry_price": entry_price,
                                "pnl": unrealized_pnl
                            }
                    return positions
    except Exception as e:
        print(f"Помилка отримання позицій Bitget: {e}")
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
        print(f"Помилка при отриманні списку монет від Bitget: {e}")
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
                            "low": float(item[3]),
                            "high": float(item[2]),
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
    if not kline_data or len(kline_data) < 20:
        return

    try:
        volumes = [x['volume'] for x in kline_data]
        lows = [x['low'] for x in kline_data]
        highs = [x['high'] for x in kline_data]
        closes = [x['close'] for x in kline_data]
        
        current_volume = volumes[-1]
        if current_volume <= 0:
            return

        avg_volume = sum(volumes[:-1]) / (len(volumes) - 1)
        if avg_volume <= 0:
            return
        
        current_price = closes[-1]
        surge_percent = int((current_volume / avg_volume - 1) * 100)
        support_level = min(lows[:-1])
        resistance_level = max(highs[:-1])

        # ПЕРЕВІРКА ПОЗИЦІЇ ДЛЯ КОНКРЕТНОЇ МОНЕТИ (Вихід з позиції)
        if symbol in open_positions:
            pos_info = open_positions[symbol]
            pos_side = pos_info["side"].upper()
            entry_price = pos_info["entry_price"]
            pnl = pos_info["pnl"]

            is_volume_spike_exit = current_volume >= (avg_volume * VOLUME_MULTIPLIER_EXIT)
            is_volume_drop_exit = current_volume <= (avg_volume * VOLUME_DECREASE_EXIT)

            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            pnl_text = f"+{pnl:.2f} USDT" if pnl >= 0 else f"{pnl:.2f} USDT"

            if pos_side == "LONG" and resistance_level > 0:
                dist_to_res = (resistance_level - current_price) / resistance_level
                if 0 <= dist_to_res <= APPROACH_PERCENT and (is_volume_spike_exit or is_volume_drop_exit):
                    exit_reason = "Кульмінаційний сплеск об'єму (>=3x)" if is_volume_spike_exit else "Зниження об'ємів (затухання)"
                    alert_message = (
                        f"🔔 МЕНЕДЖЕР ПОЗИЦІЙ [АКТИВНИЙ ЛОНГ]: `{symbol}` (Bitget)\n"
                        f"• Вхід: `{entry_price}` | Поточна ціна: `{current_price}`\n"
                        f"• Поточний PnL: {pnl_emoji} **{pnl_text}**\n"
                        f"• Сигнал на закриття: 🚨 **Підхід до опору (`{resistance_level}`) + {exit_reason}!**"
                    )
                    last_alert_time[symbol] = current_time
                    await send_to_discord(session, discord_webhook_url, alert_message)
                    return

            elif pos_side == "SHORT" and support_level > 0:
                dist_to_sup = (current_price - support_level) / support_level
                if 0 <= dist_to_sup <= APPROACH_PERCENT and (is_volume_spike_exit or is_volume_drop_exit):
                    exit_reason = "Кульмінаційний сплеск об'єму (>=3x)" if is_volume_spike_exit else "Зниження об'ємів (затухання)"
                    alert_message = (
                        f"🔔 МЕНЕДЖЕР ПОЗИЦІЙ [АКТИВНИЙ ШОРТ]: `{symbol}` (Bitget)\n"
                        f"• Вхід: `{entry_price}` | Поточна ціна: `{current_price}`\n"
                        f"• Поточний PnL: {pnl_emoji} **{pnl_text}**\n"
                        f"• Сигнал на закриття: 🚨 **Підхід до підтримки (`{support_level}`) + {exit_reason}!**"
                    )
                    last_alert_time[symbol] = current_time
                    await send_to_discord(session, discord_webhook_url, alert_message)
                    return

        # СТАНДАРТНИЙ ВХІД (працює ТІЛЬКИ якщо взагалі немає відкритих позицій на акаунті)
        else:
            is_volume_spike_entry = current_volume >= (avg_volume * VOLUME_MULTIPLIER_ENTRY)
            if not is_volume_spike_entry:
                return

            if support_level > 0:
                distance_to_support = (current_price - support_level) / support_level
                if 0 <= distance_to_support <= APPROACH_PERCENT:
                    alert_message = (
                        f"🟢🎯 **СИГНАЛ НА ВІДКРИТТЯ [ЛОНГ 15m]**: `{symbol}` (Bitget)\n"
                        f"• Підтримка: `{support_level}` (Поточна: `{current_price}`)\n"
                        f"• Об'єм свічки: `+{surge_percent}%` від середнього!"
                    )
                    last_alert_time[symbol] = current_time
                    await send_to_discord(session, discord_webhook_url, alert_message)
                    return

            if resistance_level > 0:
                distance_to_resistance = (resistance_level - current_price) / resistance_level
                if 0 <= distance_to_resistance <= APPROACH_PERCENT:
                    alert_message = (
                        f"🔴🎯 **СИГНАЛ НА ВІДКРИТТЯ [ШОРТ 15m]**: `{symbol}` (Bitget)\n"
                        f"• Опір: `{resistance_level}` (Поточна: `{current_price}`)\n"
                        f"• Об'єм свічки: `+{surge_percent}%` від середнього!"
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
    print("Бот запущено: перевірка позицій Bitget + режим тиші при відкритій позиції...")
    
    async with aiohttp.ClientSession() as session:
        asyncio.create_task(self_ping_loop(session))
        
        while True:
            start_time = asyncio.get_event_loop().time()
            
            # 1. Затягуємо список відкритих позицій з Bitget
            open_positions = await fetch_open_positions(session)
            
            # 2. Якщо є відкрита позиція, надсилаємо звіт про її стан (з обмеженням у часі, наприклад раз на 15 хв)
            if open_positions and DISCORD_WEBHOOK_URL:
                for symbol, pos in open_positions.items():
                    pnl = pos["pnl"]
                    pnl_emoji = "🟢" if pnl >= 0 else "🔴"
                    pnl_text = f"+{pnl:.2f} USDT" if pnl >= 0 else f"{pnl:.2f} USDT"
                    
                    status_msg = (
                        f"📊 **МОНІТОРИНГ ВІДКРИТОЇ ПОЗИЦІЇ**: `{symbol}` ({pos['side'].upper()})\n"
                        f"• Ціна входу: `{pos['entry_price']}`\n"
                        f"• Поточний результат (PnL): {pnl_emoji} **{pnl_text}**\n"
                        f"🔒 *Режим сканування нових входів на паузі до закриття позиції.*"
                    )
                    # Надсилаємо статус по відкритій позиції раз на 15 хвилин
                    await send_to_discord(session, DISCORD_WEBHOOK_URL, status_msg)
            
            # 3. Отримуємо топ монети для сканування
            symbols_list = await fetch_top_bitget_symbols(session)
            
            # 4. Перевіряємо монети
            if symbols_list and DISCORD_WEBHOOK_URL:
                tasks = [check_single_coin(session, symbol, open_positions, DISCORD_WEBHOOK_URL) for symbol in symbols_list]
                await asyncio.gather(*tasks)
            
            elapsed = asyncio.get_event_loop().time() - start_time
            sleep_time = max(1, 60 - elapsed) # Сканування раз на хвилину, коли є позиція, або стандартно
            await asyncio.sleep(sleep_time)


class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bitget Position Manager & Scanner Bot is running!")
    
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
                    
