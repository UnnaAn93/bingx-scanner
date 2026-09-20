import os
import time
import requests

# --- НАЛАШТУВАННЯ ---
VOLUME_MULTIPLIER = 2.2           
APPROACH_PERCENT = 0.007          
TIMEFRAME = "15"                  
LIMIT_CANDLES = 30                
TOP_COINS_LIMIT = 50              
MIN_24H_VOLUME_USDT = 100_000     

BINANCE_BASE_URL = "https://fapi.binance.com"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

def send_to_discord(message):
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=5)
    except Exception:
        pass

def fetch_top_symbols():
    url = f"{BINANCE_BASE_URL}/fapi/v1/ticker/24hr"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            tickers = response.json()
            if not isinstance(tickers, list):
                return []
            
            usdt_tickers = []
            for t in tickers:
                symbol = t.get("symbol", "")
                if symbol.endswith("USDT"):
                    try:
                        turnover = float(t.get("quoteVolume", 0))
                        if turnover >= MIN_24H_VOLUME_USDT:
                            usdt_tickers.append((symbol, turnover))
                    except Exception:
                        continue
            usdt_tickers.sort(key=lambda x: x[1], reverse=True)
            return [item[0] for item in usdt_tickers[:TOP_COINS_LIMIT]]
    except Exception as e:
        print(f"❌ Помилка отримання списку монет: {e}")
    return []

def fetch_kline_data(symbol):
    url = f"{BINANCE_BASE_URL}/fapi/v1/klines?symbol={symbol}&interval={TIMEFRAME}m&limit={LIMIT_CANDLES}"
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            raw_list = response.json()
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

def main():
    print("🚀 Запуск моніторингу ринку (ТФ: 15хв)...")
    
    symbols_list = fetch_top_symbols()
    if not symbols_list:
        print("❌ Не вдалося отримати список монет.")
        return

    print(f"Перевіряємо топ монет: {len(symbols_list)}")

    for symbol in symbols_list:
        kline_data = fetch_kline_data(symbol)
        if not kline_data or len(kline_data) < 20:
            continue

        volumes = [x['volume'] for x in kline_data]
        lows = [x['low'] for x in kline_data]
        highs = [x['high'] for x in kline_data]
        closes = [x['close'] for x in kline_data]
        
        current_volume = volumes[-1]
        if current_volume <= 0:
            continue

        avg_volume = sum(volumes[:-1]) / (len(volumes) - 1)
        if avg_volume <= 0:
            continue
        
        current_price = closes[-1]
        
        if current_volume < (avg_volume * VOLUME_MULTIPLIER):
            continue

        surge_percent = int((current_volume / avg_volume - 1) * 100)

        # ЛОНГ (Підтримка на 15m)
        support_level = min(lows[:-1])
        if support_level > 0 and 0 <= (current_price - support_level) / support_level <= APPROACH_PERCENT:
            alert_msg = (
                f"🟢🎯 **СИГНАЛ НА ЛОНГ [15m]**: `{symbol}`\n"
                f"• Ціна: `{current_price}` (Підтримка: `{support_level}`)\n"
                f"• Об'єм: `+{surge_percent}%` (Рекомендація: розглянути вхід)"
            )
            send_to_discord(alert_msg)
            continue

        # ШОРТ (Опір на 15m)
        resistance_level = max(highs[:-1])
        if resistance_level > 0 and 0 <= (resistance_level - current_price) / resistance_level <= APPROACH_PERCENT:
            alert_msg = (
                f"🔴🎯 **СИГНАЛ НА ШОРТ [15m]**: `{symbol}`\n"
                f"• Ціна: `{current_price}` (Опір: `{resistance_level}`)\n"
                f"• Об'єм: `+{surge_percent}%` (Рекомендація: розглянути вхід)"
            )
            send_to_discord(alert_msg)
            continue

    print("🏁 Сканування завершено.")

if __name__ == "__main__":
    main()
    
