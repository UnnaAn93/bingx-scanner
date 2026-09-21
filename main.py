import os
import time
import requests

# --- НАЛАШТУВАННЯ BYBIT ---
VOLUME_MULTIPLIER = 2.2           
APPROACH_PERCENT = 0.007          
TIMEFRAME = "15"                  # Таймфрейм 15 хвилин
LIMIT_CANDLES = 30                
TOP_COINS_LIMIT = 50              
MIN_24H_VOLUME_USDT = 100_000     

# Використовуємо публічний API Bybit (працює і для Testnet/Mainnet для споту та ф'ючерсів)
BYBIT_BASE_URL = "https://api.bybit.com"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

def send_to_discord(message):
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=5)
    except Exception:
        pass

def fetch_top_symbols():
    # Отримання тікерів лінійки Linear (USDT Futures) від Bybit
    url = f"{BYBIT_BASE_URL}/v5/market/tickers?category=linear"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get("retCode") == 0:
                list_tickers = data.get("result", {}).get("list", [])
                usdt_tickers = []
                
                for t in list_tickers:
                    symbol = t.get("symbol", "")
                    if symbol.endswith("USDT"):
                        try:
                            turnover = float(t.get("turnover24h", 0))
                            if turnover >= MIN_24H_VOLUME_USDT:
                                usdt_tickers.append((symbol, turnover))
                        except Exception:
                            continue
                            
                usdt_tickers.sort(key=lambda x: x[1], reverse=True)
                return [item[0] for item in usdt_tickers[:TOP_COINS_LIMIT]]
    except Exception as e:
        print(f"❌ Помилка отримання списку монет Bybit: {e}")
    return []

def fetch_kline_data(symbol):
    # У Bybit таймфрейми задаються хвилинами (15) або рядками ("15")
    url = f"{BYBIT_BASE_URL}/v5/market/kline?category=linear&symbol={symbol}&interval={TIMEFRAME}&limit={LIMIT_CANDLES}"
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get("retCode") == 0:
                raw_list = data.get("result", {}).get("list", [])
                # Bybit повертає свічки від найновішої до найстарішої, тому розгортаємо ([::-1])
                raw_list.reverse()
                formatted = []
                for item in raw_list:
                    # Формат відповіді Bybit kline: [startTime, open, high, low, close, volume, turnover]
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
    print("🚀 Запуск моніторингу Bybit (ТФ: 15хв)...")
    
    symbols_list = fetch_top_symbols()
    if not symbols_list:
        print("❌ Не вдалося отримати список монет від Bybit.")
        return

    print(f"Перевіряємо топ монет Bybit: {len(symbols_list)}")

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
                f"🟢🎯 **СИГНАЛ НА ЛОНГ [Bybit 15m]**: `{symbol}`\n"
                f"• Напрямок: 🚀 Підхід до дна / Відскок\n"
                f"• Ціна: `{current_price}` (Підтримка: `{support_level}`)\n"
                f"• Об'єм: `+{surge_percent}%` від середнього!"
            )
            send_to_discord(alert_msg)
            continue

        # ШОРТ (Опір на 15m)
        resistance_level = max(highs[:-1])
        if resistance_level > 0 and 0 <= (resistance_level - current_price) / resistance_level <= APPROACH_PERCENT:
            alert_msg = (
                f"🔴🎯 **СИГНАЛ НА ШОРТ [Bybit 15m]**: `{symbol}`\n"
                f"• Напрямок: 📉 Підхід до хаю / Відбій\n"
                f"• Ціна: `{current_price}` (Опір: `{resistance_level}`)\n"
                f"• Об'єм: `+{surge_percent}%` від середнього!"
            )
            send_to_discord(alert_msg)
            continue

    print("🏁 Сканування Bybit завершено.")

if __name__ == "__main__":
    main()
    
