import os
import time
import asyncio
import aiohttp
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
BINGX_BASE_URL = "https://open-api.bingx.com"

LEVERAGE = 10
DEPOSIT_USD = 20.0
RISK_USD = 0.60  # 3% від $20 (ризик на стоп)

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is running!")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()

async def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload, timeout=10) as resp:
                pass
        except:
            pass

async def get_klines(session, symbol, interval, limit=100):
    url = f"{BINGX_BASE_URL}/openApi/swap/v3/quote/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        async with session.get(url, params=params, timeout=5) as resp:
            if resp.status == 200:
                data = await resp.json()
                klines = data.get("data", [])
                if not klines and isinstance(data, list):
                    klines = data
                return klines
    except:
        pass
    return []

def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return 0.0
    tr_list = []
    for i in range(1, len(candles)):
        high = float(candles[i]['high'])
        low = float(candles[i]['low'])
        prev_close = float(candles[i-1]['close'])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)
    return sum(tr_list[-period:]) / period
    
def calculate_kd(candles, n=9, m1=3, m2=3):
    try:
        if len(candles) < n:
            return [], [], []

        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]
        closes = [float(c["close"]) for c in candles]

        rsv_list = []

        for i in range(n - 1, len(candles)):
            highest = max(highs[i - n + 1:i + 1])
            lowest = min(lows[i - n + 1:i + 1])

            if highest == lowest:
                rsv = 50.0
            else:
                rsv = (
                    (closes[i] - lowest)
                    / (highest - lowest)
                    * 100.0
                )

            rsv_list.append(rsv)

        if not rsv_list:
            return [], [], []

        k_list = []
        d_list = []
        j_list = []

        k = 50.0
        d = 50.0

        for rsv in rsv_list:
            k = (2.0 / 3.0) * k + (1.0 / 3.0) * rsv
            d = (2.0 / 3.0) * d + (1.0 / 3.0) * k
            j = 3.0 * k - 2.0 * d

            k_list.append(k)
            d_list.append(d)
            j_list.append(j)

        return k_list, d_list, j_list

    except Exception as e:
        print(f"[KDJ ERROR] {e}", flush=True)
        return [], [], []
        
def calculate_correlation(prices_a, prices_b):
    if len(prices_a) != len(prices_b) or len(prices_a) < 10:
        return 0.0
    
    n = len(prices_a)
    mean_a = sum(prices_a) / n
    mean_b = sum(prices_b) / n
    
    num = sum((prices_a[i] - mean_a) * (prices_b[i] - mean_b) for i in range(n))
    den_a = sum((prices_a[i] - mean_a) ** 2 for i in range(n))
    den_b = sum((prices_b[i] - mean_b) ** 2 for i in range(n))
    
    if den_a == 0 or den_b == 0:
        return 0.0
        
    return num / ((den_a * den_b) ** 0.5)
    
# --- 1. ЩОДЕННИЙ ЗВІТ ПО ЗАКРИТИХ СВІПАХ (1D) З ДАНИМИ СВІЧОК ---
# --- 1. СУВОРИЙ ЩОДЕННИЙ ЗВІТ ПО ЗАКРИТИХ СВІПАХ (1D) ---
async def job_daily_sweep_report(session, symbols):
    print("[INFO] Формування звіту по закритих денних свіпах (1D)...", flush=True)
    bullish_sweeps = []
    bearish_sweeps = []

    batch_size = 20
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        # Запитуємо лише 5 свічок для максимальної точності та швидкості
        tasks = [get_klines(session, sym, '1d', 5) for sym in batch]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for j, klines_1d in enumerate(results):
                if isinstance(klines_1d, Exception) or not klines_1d or len(klines_1d) < 3:
                    continue
                symbol = batch[j]
                
                # Сортуємо свічки за часом (на випадок, якщо біржа віддає їх у розкид)
                try:
                    klines_1d = sorted(klines_1d, key=lambda x: int(x.get('time', x.get('openTime', 0))))
                except:
                    pass
                
                # [-1] — поточна незакрита доба
                # [-2] — повністю закрита вчорашня доба
                # [-3] — позавчорашня доба
                c_prev = klines_1d[-3]  
                c_closed = klines_1d[-2] 
                
                try:
                    open_prev, high_prev, low_prev, close_prev = float(c_prev['open']), float(c_prev['high']), float(c_prev['low']), float(c_prev['close'])
                    open_closed, high_closed, low_closed, close_closed = float(c_closed['open']), float(c_closed['high']), float(c_closed['low']), float(c_closed['close'])

                    is_low_sweep = (low_closed < low_prev) and (close_closed > low_prev)
                    is_high_sweep = (high_closed > high_prev) and (close_closed < high_prev)

                    if is_low_sweep:
                        b_str = (
                            f"🟢 `{symbol}` *(Low Sweep)*\n"
                            f"   • Попередній Low: `{low_prev}`\n"
                            f"   • Закрита доба 1D: Low `{low_closed}` | Close `{close_closed}`"
                        )
                        if b_str not in bullish_sweeps:
                            bullish_sweeps.append(b_str)
                    
                    if is_high_sweep:
                        s_str = (
                            f"🔴 `{symbol}` *(High Sweep)*\n"
                            f"   • Попередній High: `{high_prev}`\n"
                            f"   • Закрита доба 1D: High `{high_closed}` | Close `{close_closed}`"
                        )
                        if s_str not in bearish_sweeps:
                            bearish_sweeps.append(s_str)
                except:
                    continue
        except:
            pass
        await asyncio.sleep(0.2)

    report = "🔔 *Щоденний звіт по закритих свіпах (1D)*\n\n"
    if bullish_sweeps:
        report += "*Бичачі свіпи (Лонг):*\n\n" + "\n\n".join(bullish_sweeps[:10]) + "\n\n"
    if bearish_sweeps:
        report += "*Ведмежі свіпи (Шорт):*\n\n" + "\n\n".join(bearish_sweeps[:10]) + "\n\n"
    if not bullish_sweeps and not bearish_sweeps:
        report += "ℹ️ За минулу добу чітких свіпів за заданими умовами не виявлено."

    await send_telegram(report)
    print("[INFO] Звіт по свіпах успішно надіслано!", flush=True)

signal_cooldowns = {}
COOLDOWN_PERIOD = 3600  # 1 година

def candle_timestamp_ms(candle):
    ts = candle.get("time", candle.get("openTime"))

    if ts is None:
        return 0

    ts = int(ts)

    # Якщо час прийшов у секундах, переводимо в мілісекунди
    if ts < 10_000_000_000:
        ts *= 1000

    return ts


def get_closed_candles(raw_candles, interval_ms):
    now_ms = int(time.time() * 1000)

    ordered = sorted(
        raw_candles,
        key=candle_timestamp_ms
    )

    closed = []

    for candle in ordered:
        open_time = candle_timestamp_ms(candle)

        if open_time <= 0:
            continue

        # Свічка закрита, якщо минув увесь її інтервал
        if open_time + interval_ms <= now_ms:
            closed.append(candle)

    return closed

btc_cache = {"time": 0, "candles": []}

async def get_btc_1h_candles(session):
    now = time.time()
    # Оновлюємо дані по BTC раз на 15 хвилин (900 секунд)
    if btc_cache["candles"] and (now - btc_cache["time"] < 900):
        return btc_cache["candles"]
    
    raw_btc = await get_klines(session, "BTC-USDT", "1h", 50)
    if not raw_btc:
        return btc_cache["candles"]
        
    closed_btc = get_closed_candles(raw_btc, 60 * 60 * 1000)
    btc_cache["candles"] = closed_btc
    btc_cache["time"] = now
    return closed_btc
    

async def analyze_signal(session, symbol):
    try:
        # =====================================================
        # 0. COOLDOWN
        # =====================================================
        now = time.time()

        if (
            symbol in signal_cooldowns
            and now - signal_cooldowns[symbol] < COOLDOWN_PERIOD
        ):
            return None

        # =====================================================
        # 1. ЗАВАНТАЖЕННЯ СВІЧОК
        # =====================================================
        async def fetch_data():
            raw_1h = await get_klines(
                session, symbol, "1h", 200
            )

            raw_15m = await get_klines(
                session, symbol, "15m", 60
            )

            return raw_1h, raw_15m

        raw_1h, raw_15m = await asyncio.wait_for(
            fetch_data(),
            timeout=6.0
        )

        if not raw_1h or not raw_15m:
            return None

        # Сортуємо та залишаємо тільки закриті свічки
        raw_1h = get_closed_candles(
            raw_1h,
            60 * 60 * 1000
        )

        raw_15m = get_closed_candles(
            raw_15m,
            15 * 60 * 1000
        )

        if len(raw_1h) < 30 or len(raw_15m) < 40:
            return None

        # =====================================================
        # 2. ПІДГОТОВКА ДАНИХ
        # =====================================================
        candles_1h = []
        candles_15m = []

        for c in raw_1h:
            candles_1h.append({
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "time": candle_timestamp_ms(c)
            })

        for c in raw_15m:
            volume = (
                c.get("volume")
                or c.get("Volume")
                or c.get("vol")
                or 0
            )

            candles_15m.append({
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": float(volume),
                "time": candle_timestamp_ms(c)
            })

        # =====================================================
        # 3. СПЛЕСК ОБ'ЄМУ 15m — ПЕРША ПЕРЕВІРКА
        # =====================================================
        # Базовий об'єм: свічки від -25 до -5
        base_volumes = [
            c["volume"]
            for c in candles_15m[-25:-5]
        ]

        # Останні 5 повністю закритих свічок
        recent_volumes = [
            c["volume"]
            for c in candles_15m[-5:]
        ]

        if not base_volumes or not recent_volumes:
            return None

        avg_volume = sum(base_volumes) / len(base_volumes)

        if avg_volume <= 0:
            return None

        # Хоча б одна з останніх 5 свічок має бути
        # з об'ємом >= 2.5 середнього базового об'єму
        has_volume_spike = any(
            volume >= avg_volume * 2.5
            for volume in recent_volumes
        )

        # Якщо сплеску немає — одразу STOP
        if not has_volume_spike:
            return None

        # =====================================================
        # 4. РОЗРАХУНОК KDJ
        # =====================================================
        k_15m, d_15m, j_15m = calculate_kd(candles_15m)
        k_1h, d_1h, j_1h = calculate_kd(candles_1h)

        if (
            len(k_15m) < 35
            or len(d_15m) != len(k_15m)
            or len(j_15m) != len(k_15m)
        ):
            return None

        if (
            len(k_1h) < 10
            or len(d_1h) != len(k_1h)
        ):
            return None

        # =====================================================
        # 5. ПОТОЧНИЙ ПЕРЕТИН KDJ НА 15m
        # =====================================================
        # Усі свічки вже закриті.
        # Остання закрита свічка — [-1].
        current_idx = len(k_15m) - 1
        previous_idx = current_idx - 1

        cross_up = (
            k_15m[previous_idx] <= d_15m[previous_idx]
            and k_15m[current_idx] > d_15m[current_idx]
        )

        cross_down = (
            k_15m[previous_idx] >= d_15m[previous_idx]
            and k_15m[current_idx] < d_15m[current_idx]
        )

        # Якщо поточного перетину немає — STOP
        if not cross_up and not cross_down:
            return None

        is_long_signal = cross_up
        is_short_signal = cross_down

        # =====================================================
        # 6. ПІДТВЕРДЖЕННЯ НА 1h
        # =====================================================
        h_last = len(k_1h) - 1

        # Обидві лінії K і D повинні рухатися вгору
        h_lines_rising = (
            k_1h[h_last] > k_1h[h_last - 1]
            and d_1h[h_last] > d_1h[h_last - 1]
        )

        # Обидві лінії K і D повинні рухатися вниз
        h_lines_falling = (
            k_1h[h_last] < k_1h[h_last - 1]
            and d_1h[h_last] < d_1h[h_last - 1]
        )

        # Шукаємо перетин потрібного напрямку
        # серед останніх 6 закритих годинних свічок
        h_cross_up = False
        h_cross_down = False

        h_start = max(1, len(k_1h) - 6)

        for i in range(h_start, len(k_1h)):
            if (
                k_1h[i - 1] <= d_1h[i - 1]
                and k_1h[i] > d_1h[i]
            ):
                h_cross_up = True

            if (
                k_1h[i - 1] >= d_1h[i - 1]
                and k_1h[i] < d_1h[i]
            ):
                h_cross_down = True

        # Для LONG: перетин вгору АБО обидві лінії ростуть
        if is_long_signal:
            if not (h_cross_up or h_lines_rising):
                return None

        # Для SHORT: перетин вниз АБО обидві лінії падають
        if is_short_signal:
            if not (h_cross_down or h_lines_falling):
                return None

        # =====================================================
        # 7. ПОПЕРЕДНІЙ ПЕРЕТИН 15m У ТОМУ Ж НАПРЯМКУ
        # =====================================================
        # Шукаємо найближчий попередній перетин
        # у тому самому напрямку, не включаючи поточний.
        first_cross_idx = None

        search_start = max(1, current_idx - 30)

        for i in range(current_idx - 1, search_start - 1, -1):

            previous_up = (
                k_15m[i - 1] <= d_15m[i - 1]
                and k_15m[i] > d_15m[i]
            )

            previous_down = (
                k_15m[i - 1] >= d_15m[i - 1]
                and k_15m[i] < d_15m[i]
            )

            if is_long_signal and previous_up:
                first_cross_idx = i
                break

            if is_short_signal and previous_down:
                first_cross_idx = i
                break

        # Якщо попереднього перетину немає — STOP
        if first_cross_idx is None:
            return None

        # =====================================================
        # 8. ПЕРЕВІРКА ЕКСТРЕМУ J
        # =====================================================
        # J може досягнути екстремуму:
        # - до першого перетину;
        # - між першим і другим перетином.
        #
        # Але не після поточного перетину.
        #
        # Перевіряємо історію до поточного перетину,
        # у межах останніх 30 значень KDJ.

        j_search_start = max(0, current_idx - 30)

        j_values_before_current = j_15m[
            j_search_start:current_idx
        ]

        if not j_values_before_current:
            return None

        if is_long_signal:
            extreme_j_found = any(
                value < 10
                for value in j_values_before_current
            )

        else:
            extreme_j_found = any(
                value > 90
                for value in j_values_before_current
            )

        # Якщо J не був в екстремумі — STOP
        if not extreme_j_found:
            return None

        # =====================================================
        # 9. ATR НА 1h
        # =====================================================
        atr_th = calculate_atr(
            candles_1h,
            period=14
        )

        if atr_th <= 0:
            return None

        current_price_for_atr = candles_1h[-1]["close"]

        # Фільтр мінімальної волатильності
        if atr_th < current_price_for_atr * 0.004:
            return None
            
        # =====================================================
        # 9.5. ФІЛЬТР КОРЕЛЯЦІЇ З BITCOIN (1h)
        # =====================================================
        if symbol != "BTC-USDT":
            btc_1h = await get_btc_1h_candles(session)
            
            # Беремо останні 24 закриті годинні свічки (доба)
            period_corr = 24
            if len(btc_1h) >= period_corr and len(candles_1h) >= period_corr:
                # Вирівнюємо свічки точно за часом (timestamp), щоб не було зсувів
                btc_map = {candle_timestamp_ms(c): float(c["close"]) for c in btc_1h[-period_corr:]}
                coin_map = {c["time"]: c["close"] for c in candles_1h[-period_corr:]}
                
                common_times = sorted(set(btc_map.keys()) & set(coin_map.keys()))
                
                if len(common_times) >= 15:
                    btc_closes = [btc_map[t] for t in common_times]
                    coin_closes = [coin_map[t] for t in common_times]
                    
                    corr = calculate_correlation(btc_closes, coin_closes)
                    
                    # Відсікаємо монети, які сильно повторюють рух BTC (поріг >= 0.45)
                    # Значення від -1.0 до +0.45 означають слабку, відсутню або зворотну кореляцію
                    if corr >= 0.45:
                        return None
                        
        # =====================================================
        # 10. ПОТОЧНА ЦІНА
        # =====================================================
        ticker_url = (
            f"{BINGX_BASE_URL}"
            f"/openApi/swap/v2/quote/ticker"
        )

        current_price = None

        async with session.get(
            ticker_url,
            params={"symbol": symbol},
            timeout=5
        ) as ticker_resp:

            if ticker_resp.status == 200:
                ticker_data = await ticker_resp.json()

                if (
                    ticker_data.get("code") == 0
                    and ticker_data.get("data")
                ):
                    current_price = float(
                        ticker_data["data"]["lastPrice"]
                    )

        if current_price is None or current_price <= 0:
            current_price = candles_1h[-1]["close"]

        # =====================================================
        # 11. ФОРМУВАННЯ LONG
        # =====================================================
        if is_long_signal:

            signal_name = (
                "LONG (1h Confirmation + 15m Volume & KDJ)"
            )

            stop_loss = round(
                current_price - atr_th * 2.0,
                8
            )

            tp1 = round(
                current_price + atr_th * 2.5,
                8
            )

            tp2 = round(
                tp1 + atr_th * 2.0,
                8
            )

            tp3 = round(
                tp2 + atr_th * 2.0,
                8
            )

            risk_per_coin = current_price - stop_loss

        # =====================================================
        # 12. ФОРМУВАННЯ SHORT
        # =====================================================
        elif is_short_signal:

            signal_name = (
                "SHORT (1h Confirmation + 15m Volume & KDJ)"
            )

            stop_loss = round(
                current_price + atr_th * 2.0,
                8
            )

            tp1 = round(
                current_price - atr_th * 2.5,
                8
            )

            tp2 = round(
                tp1 - atr_th * 2.0,
                8
            )

            tp3 = round(
                tp2 - atr_th * 2.0,
                8
            )

            risk_per_coin = stop_loss - current_price

        else:
            return None

        # =====================================================
        # 13. РОЗРАХУНОК ПОЗИЦІЇ
        # =====================================================
        if risk_per_coin <= 0:
            return None

        pos_qty = RISK_USD / risk_per_coin

        notional_usd = pos_qty * current_price

        margin_usd = notional_usd / LEVERAGE

        # Cooldown встановлюємо тільки після готового сигналу
        signal_cooldowns[symbol] = time.time()

        print(
            f"[SIGNAL] {symbol} | "
            f"{'LONG' if is_long_signal else 'SHORT'} | "
            f"Volume OK | 15m Cross OK | "
            f"1h Confirmed | Previous Cross OK | "
            f"J Extreme OK",
            flush=True
        )

        return {
            "symbol": symbol,
            "signal": signal_name,
            "entry": current_price,
            "stop_loss": stop_loss,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "pos_qty": pos_qty,
            "notional_usd": notional_usd,
            "margin_usd": margin_usd
        }

    except Exception as e:
        print(
            f"[ERROR in {symbol}] {e}",
            flush=True
        )
        return None
        
async def scan_5min_loop(session, symbols):
    print("[INFO] Початок сканування ринку (1h KDJ Crossover + 15m Об'єм)...", flush=True)
    batch_size = 20
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        tasks = [analyze_signal(session, sym) for sym in batch]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for setup in results:
                if isinstance(setup, dict):
                    msg = (
                        f"🎯 **Знайдено сигнал ({setup['signal']})**\n"
                        f"🪙 Монета: `{setup['symbol']}`\n\n"
                        f"🟢 Вхід (орієнтир): `{setup['entry']:.8f}`\n"
                        f"🔴 Стоп-лос: `{setup['stop_loss']:.8f}`\n"
                        f"🎯 TP1: `{setup['tp1']:.8f}` | TP2: `{setup['tp2']:.8f}` | TP3: `{setup['tp3']:.8f}`\n\n"
                        f"🧮 **Розрахунок позиції (Депозит $20):**\n"
                        f"📦 Кількість: `{setup['pos_qty']:.4f}`\n"
                        f"💵 Обсяг позиції: `{setup['notional_usd']:.2f} USD`\n"
                        f"🏦 Маржа (10x): `{setup['margin_usd']:.2f} USD`\n"
                        f"🛡️ Ризик на стоп: `$0.60` (3%)\n\n"
                        f"⏳ Очікування ручного відкриття..."
                    )
                    await send_telegram(msg)
        except Exception as e:
            print(f"[BATCH ERROR] {e}", flush=True)
        await asyncio.sleep(0.3)
    print("[INFO] Сканування ринку завершено.", flush=True)

# --- ГОЛОВНИЙ ДЕМОН ---
# Константа мінімального добового об'єму (в USD)
MIN_24H_VOLUME_USD = 400_000

async def main_bot_loop():
    await send_telegram("🚀 Торговий бот успішно запущено!\n• Щоденний звіт по закритих свіпах активний\n• Перевірка KDJ + Об'єм (24h Vol >= $400k) кожні 5 хв активна.")
    
    last_daily_report_date = None

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                # 1. Запитуємо тікери з цінами та 24h об'ємами
                async with session.get(f"{BINGX_BASE_URL}/openApi/swap/v2/quote/ticker", timeout=10) as resp:
                    if resp.status != 200:
                        await asyncio.sleep(60)
                        continue
                    res = await resp.json()

                tickers_data = res.get("data", []) if isinstance(res, dict) else []
                
                # Формуємо множину монет, чий добовий об'єм >= $400,000
                valid_volume_symbols = set()
                for t in tickers_data:
                    if isinstance(t, dict):
                        sym = t.get("symbol", "")
                        # Перевіряємо об'єм у USDT (quoteVolume або volume * price)
                        vol_quote = float(t.get("quoteVolume", 0) or t.get("volume", 0))
                        if vol_quote >= MIN_24H_VOLUME_USD:
                            valid_volume_symbols.add(sym)

                # 2. Отримуємо загальний список контракту
                async with session.get(f"{BINGX_BASE_URL}/openApi/swap/v2/quote/contracts", timeout=10) as resp:
                    if resp.status != 200:
                        await asyncio.sleep(60)
                        continue
                    res_c = await resp.json()

                contracts_data = res_c if isinstance(res_c, list) else res_c.get("data", [])
                
                exclude_patterns = ("NCF", "NCS", "NCC", "-USDC")
                
                # 3. Фільтруємо монети: прибираємо сміттєві патерни ТА відсікаємо ті, де 24h volume < $400k
                symbols = [
                    item["symbol"] for item in contracts_data 
                    if isinstance(item, dict) and "symbol" in item 
                    and not any(p in item["symbol"] for p in exclude_patterns)
                    and item["symbol"] in valid_volume_symbols
                ]

                print(f"[INFO] Відфільтровано монет з об'ємом >= $400K: {len(symbols)}", flush=True)

                now_utc = datetime.now(timezone.utc)
                today_str = now_utc.strftime("%Y-%m-%d")

                if last_daily_report_date != today_str and now_utc.hour >= 0 and now_utc.minute >= 5:
                    await job_daily_sweep_report(session, symbols)
                    last_daily_report_date = today_str

                await scan_5min_loop(session, symbols)

        except Exception as e:
            import traceback
            print(f"[ERROR] Помилка в основному циклі: {e}\n{traceback.format_exc()}", flush=True)

        await asyncio.sleep(300)
        

async def self_ping_loop():
    while True:
        await asyncio.sleep(240)
        url = os.getenv("RENDER_URL")
        if not url:
            continue
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=10) as resp:
                    pass
            except Exception as e:
                print(f"[LOG WARN] Самопінг не вдався: {e}", flush=True)

async def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    asyncio.create_task(self_ping_loop())
    await main_bot_loop()

if __name__ == "__main__":
    asyncio.run(main())
                
