import os
import time
import hmac
import hashlib
import requests

# --- НАЛАШТУВАННЯ ---
VOLUME_MULTIPLIER = 2.2           
APPROACH_PERCENT = 0.007          
TIMEFRAME = "5"                   
LIMIT_CANDLES = 30                
TOP_COINS_LIMIT = 50              
MIN_24H_VOLUME_USDT = 100_000     

LEVERAGE = 10                     
TRADE_USDT_AMOUNT = 10.0          

# Ключі та адреси (для Binance Demo використовується бойовий домен fapi.binance.com)
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "")
BINANCE_BASE_URL = "https://fapi.binance.com"

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

def get_binance_signature(secret, query_string):
    return hmac.new(secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()

def binance_request(method, path, params=None):
    if params is None:
        params = {}
    
    params["timestamp"] = int(time.time() * 1000)
    query_string = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
    signature = get_binance_signature(BINANCE_SECRET_KEY, query_string)
    query_string += f"&signature={signature}"
    
    url = f"{BINANCE_BASE_URL}{path}?{query_string}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    
    try:
        if method == "GET":
            response = requests.get(url, headers=headers, timeout=5)
            return response.json()
        elif method == "POST":
            response = requests.post(url, headers=headers, timeout=5)
            return response.json()
    except Exception as e:
        print(f"Помилка запиту до Binance API ({path}): {e}")
    return None

def set_leverage(symbol):
    path = "/fapi/v1/leverage"
    params = {"symbol": symbol, "leverage": LEVERAGE}
    res = binance_request("POST", path, params)
    print(f"Встановлення кредитного плеча {LEVERAGE}x для {symbol}: {res}")

def get_open_positions():
    path = "/fapi/v2/positionRisk"
    res = binance_request("GET", path)
    positions = {}
    if isinstance(res, list):
        for p in res:
            try:
                amt = float(p.get('positionAmt', 0))
                if amt != 0:
                    symbol = p['symbol']
                    side = "LONG" if amt > 0 else "SHORT"
                    positions[symbol] = side
            except Exception:
                continue
    return positions

def open_market_order(symbol, side):
    print(f"🔄 Спроба відкрити ринковий ордер: {symbol} | Сторона: {side}")
    set_leverage(symbol)
    
    ticker_url = f"{BINANCE_BASE_URL}/fapi/v1/ticker/price?symbol={symbol}"
    try:
        resp = requests.get(ticker_url, timeout=5).json()
        current_price = float(resp['price'])
    except Exception as e:
        print(f"❌ Не вдалося отримати ціну для {symbol}: {e}")
        return

    notional_size = TRADE_USDT_AMOUNT * LEVERAGE
    qty = round(notional_size / current_price, 3)
    if qty <= 0:
        qty = 0.001

    path = "/fapi/v1/order"
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty
    }
    
    res = binance_request("POST", path, params)
    if res and "orderId" in res:
        print(f"✅ Успішно відкрито ордер [{side}] для {symbol}, ID: {res['orderId']}")
    else:
        print(f"❌ Помилка відкриття ордера для {symbol}: {res}")

def close_position(symbol, current_side):
    close_side = "SELL" if current_side == "LONG" else "BUY"
    print(f"🔄 Закриваємо позицію по {symbol} (сторона: {close_side})")
    
    pos_res = binance_request("GET", "/fapi/v2/positionRisk")
    abs_qty = 0.0
    if isinstance(pos_res, list):
        for p in pos_res:
            if p['symbol'] == symbol:
                abs_qty = abs(float(p.get('positionAmt', 0)))
                break
                
    if abs_qty > 0:
        path = "/fapi/v1/order"
        params = {
            "symbol": symbol,
            "side": close_side,
            "type": "MARKET",
            "quantity": abs_qty
        }
        res = binance_request("POST", path, params)
        if res and "orderId" in res:
            print(f"✅ Позицію по {symbol} успішно закрито.")
        else:
            print(f"❌ Помилка закриття позиції по {symbol}: {res}")

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
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            tickers = response.json()
            usdt_tickers = []
            for t in tickers:
                symbol = t.get("symbol", "")
                if symbol.endswith("USDT"):
                    turnover = float(t.get("quoteVolume", 0))
                    if turnover >= MIN_24H_VOLUME_USDT:
                        usdt_tickers.append((symbol, turnover))
            usdt_tickers.sort(key=lambda x: x[1], reverse=True)
            return [item[0] for item in usdt_tickers[:TOP_COINS_LIMIT]]
    except Exception as e:
        print(f"Помилка завантаження списку монет: {e}")
    return []

def fetch_kline_data(symbol):
    url = f"{BINANCE_BASE_URL}/fapi/v1/klines?symbol={symbol}&interval={TIMEFRAME}m&limit={LIMIT_CANDLES}"
    try:
        response = requests.get(url, timeout=4)
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
    print("🚀 Запуск сканування ринку через GitHub Actions...")
    
    current_positions = get_open_positions()
    print(f"Поточні відкриті позиції на біржі: {current_positions}")
    
    symbols_list = fetch_top_symbols()
    if not symbols_list:
        print("❌ Не вдалося отримати список монет.")
        return

    print(f"Отримано топ монет для перевірки: {len(symbols_list)}")

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

        # 1. ЛОНГ (Підтримка)
        support_level = min(lows[:-1])
        if support_level > 0 and 0 <= (current_price - support_level) / support_level <= APPROACH_PERCENT:
            
            if current_positions.get(symbol) == "SHORT":
                close_position(symbol, "SHORT")

            alert_msg = (
                f"🟢🎯 **DEMO [ЛОНГ / Підтримка 5m]**: `{symbol}`\n"
                f"• Ціна: `{current_price}` (Підтримка: `{support_level}`)\n"
                f"• Об'єм: `+{surge_percent}%`"
            )
            send_to_discord(alert_msg)
            
            if symbol not in current_positions:
                open_market_order(symbol, "BUY")
            continue

        # 2. ШОРТ (Опір)
        resistance_level = max(highs[:-1])
        if resistance_level > 0 and 0 <= (resistance_level - current_price) / resistance_level <= APPROACH_PERCENT:
            
            if current_positions.get(symbol) == "LONG":
                close_position(symbol, "LONG")

            alert_msg = (
                f"🔴🎯 **DEMO [ШОРТ / Опір 5m]**: `{symbol}`\n"
                f"• Ціна: `{current_price}` (Опір: `{resistance_level}`)\n"
                f"• Об'єм: `+{surge_percent}%`"
            )
            send_to_discord(alert_msg)
            
            if symbol not in current_positions:
                open_market_order(symbol, "SELL")
            continue

    print("🏁 Сканування завершено.")

if __name__ == "__main__":
    main()
                    
