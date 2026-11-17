import os
import time
import asyncio
import re
import aiohttp
import math
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
BINGX_BASE_URL = "https://open-api.bingx.com"

LEVERAGE = 10
DEPOSIT_USD = 20.0
RISK_USD = 0.60

# Налаштування нової стратегії
RANGE_LOOKBACK_1H = 72
ENTRY_LOOKBACK_15M = 48

MIN_RANGE_TOUCHES = 2
MIN_RETESTS = 3

VOLUME_MULTIPLIER = 1.8

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
        print("[TELEGRAM] Не задано токен або chat_id", flush=True)
        return

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    # Розбиваємо повідомлення на частини до 3500 символів
    chunks = []
    current = ""

    for line in text.split("\n"):
        candidate = current + ("\n" if current else "") + line

        if len(candidate) > 3500 and current:
            chunks.append(current)
            current = line
        else:
            current = candidate

    if current:
        chunks.append(current)

    async with aiohttp.ClientSession() as session:
        for chunk in chunks:
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "parse_mode": "Markdown"
            }

            try:
                async with session.post(
                    url,
                    json=payload,
                    timeout=10
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        print(
                            f"[TELEGRAM ERROR] {resp.status}: {error_text}",
                            flush=True
                        )

            except Exception as e:
                print(
                    f"[TELEGRAM ERROR] {e}",
                    flush=True
                )


# Глобальне обмеження запитів KLINES BingX
KLINES_SEMAPHORE = asyncio.Semaphore(3)
KLINES_REQUEST_LOCK = asyncio.Lock()

KLINES_LAST_REQUEST = 0.0
KLINES_MIN_INTERVAL = 0.25

# Час завершення блокування BingX
KLINES_BLOCKED_UNTIL = 0.0

# Резервна пауза — 5 хвилин
KLINES_FALLBACK_COOLDOWN = 300
# Кеш свічок для кожної монети та таймфрейму
CANDLE_CACHE = {}

# Окремі блокування для оновлення кешу кожної монети
CANDLE_CACHE_LOCKS = {}

# Невелика затримка після закриття свічки,
# щоб BingX встиг оновити дані
CANDLE_CACHE_GRACE_MS = 5000

async def get_klines(session, symbol, interval, limit=100):
    global KLINES_LAST_REQUEST
    global KLINES_BLOCKED_UNTIL

    url = f"{BINGX_BASE_URL}/openApi/swap/v3/quote/klines"

    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }

    try:
        async with KLINES_SEMAPHORE:

            # Перевіряємо глобальне блокування
            async with KLINES_REQUEST_LOCK:

                now = time.monotonic()

                if now < KLINES_BLOCKED_UNTIL:
                    remaining = KLINES_BLOCKED_UNTIL - now

                    print(
                        f"[KLINES COOLDOWN] {symbol} {interval}: "
                        f"запит пропущено, залишилось "
                        f"{remaining:.0f} сек.",
                        flush=True
                    )
                    return []

                # Дотримуємося паузи між запитами
                elapsed = now - KLINES_LAST_REQUEST
                wait_time = KLINES_MIN_INTERVAL - elapsed

                if wait_time > 0:
                    await asyncio.sleep(wait_time)

                # Повторна перевірка після очікування
                now = time.monotonic()

                if now < KLINES_BLOCKED_UNTIL:
                    return []

                KLINES_LAST_REQUEST = time.monotonic()

            async with session.get(
                url,
                params=params,
                timeout=10
            ) as resp:

                if resp.status != 200:
                    error_text = await resp.text()

                    print(
                        f"[KLINES HTTP ERROR] {symbol} {interval}: "
                        f"HTTP {resp.status} — {error_text[:300]}",
                        flush=True
                    )

                    return []

                data = await resp.json()

                if not isinstance(data, dict):
                    print(
                        f"[KLINES ERROR] {symbol} {interval}: "
                        "неочікуваний формат відповіді",
                        flush=True
                    )
                    return []

                error_code = str(data.get("code", "0"))

                if error_code == "100410":

                    error_msg = str(
                        data.get("msg", "Unknown error")
                    )

                    # Шукаємо точний timestamp розблокування
                    match = re.search(
                        r"(\d{13})",
                        error_msg
                    )

                    if match:
                        unblock_timestamp = int(match.group(1))
                        remaining = max(
                            0,
                            unblock_timestamp / 1000 - time.time()
                        )
                    else:
                        remaining = KLINES_FALLBACK_COOLDOWN

                    async with KLINES_REQUEST_LOCK:
                        KLINES_BLOCKED_UNTIL = max(
                            KLINES_BLOCKED_UNTIL,
                            time.monotonic() + remaining
                        )

                    print(
                        f"[KLINES GLOBAL BLOCK] BingX 100410. "
                        f"Усі нові KLINES-запити зупинено "
                        f"приблизно на {remaining:.0f} сек. "
                        f"Причина: {error_msg}",
                        flush=True
                    )

                    return []

                if error_code != "0":
                    print(
                        f"[KLINES API ERROR] {symbol} {interval}: "
                        f"{error_code} — "
                        f"{data.get('msg', 'Unknown error')}",
                        flush=True
                    )
                    return []

                klines = data.get("data", [])

                if not isinstance(klines, list):
                    print(
                        f"[KLINES ERROR] {symbol} {interval}: "
                        "data не є списком",
                        flush=True
                    )
                    return []

                return klines

    except asyncio.TimeoutError:
        print(
            f"[KLINES TIMEOUT] {symbol} {interval}",
            flush=True
        )
        return []

    except Exception as e:
        print(
            f"[KLINES EXCEPTION] {symbol} {interval}: {e}",
            flush=True
        )
        return []

async def get_cached_klines(session, symbol, interval, limit=100):
    interval_ms_map = {
        "15m": 15 * 60 * 1000,
        "1h": 60 * 60 * 1000
    }

    interval_ms = interval_ms_map.get(interval)

    if interval_ms is None:
        return await get_klines(session, symbol, interval, limit)

    now_ms = int(time.time() * 1000)

    # Номер останнього повністю закритого інтервалу
    bucket = (
        now_ms - CANDLE_CACHE_GRACE_MS
    ) // interval_ms

    key = (symbol, interval, limit)

    cached = CANDLE_CACHE.get(key)

    # Якщо кеш актуальний — API не викликаємо
    if cached and cached["bucket"] == bucket:
        return cached["data"]

    # Створюємо окреме блокування для цієї монети
    lock = CANDLE_CACHE_LOCKS.setdefault(
        key,
        asyncio.Lock()
    )

    async with lock:

        # Повторна перевірка після очікування блокування
        cached = CANDLE_CACHE.get(key)

        if cached and cached["bucket"] == bucket:
            return cached["data"]

        # Отримуємо свіжі свічки з BingX
        klines = await get_klines(
            session,
            symbol,
            interval,
            limit
        )

        if not klines:
            return []

        # Зберігаємо результат у кеші
        CANDLE_CACHE[key] = {
            "bucket": bucket,
            "data": klines
        }

        return klines
        
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
    if len(prices_a) != len(prices_b) or len(prices_a) < 11:
        return 0.0

    try:
        prices_a = [float(x) for x in prices_a]
        prices_b = [float(x) for x in prices_b]

        if not all(
            math.isfinite(x) and x > 0
            for x in prices_a + prices_b
        ):
            return 0.0

        # Рахуємо відсоткові зміни між сусідніми свічками
        returns_a = [
            (prices_a[i] / prices_a[i - 1] - 1) * 100
            for i in range(1, len(prices_a))
        ]

        returns_b = [
            (prices_b[i] / prices_b[i - 1] - 1) * 100
            for i in range(1, len(prices_b))
        ]

        n = len(returns_a)

        mean_a = sum(returns_a) / n
        mean_b = sum(returns_b) / n

        num = sum(
            (returns_a[i] - mean_a) *
            (returns_b[i] - mean_b)
            for i in range(n)
        )

        den_a = sum(
            (x - mean_a) ** 2 for x in returns_a
        )

        den_b = sum(
            (x - mean_b) ** 2 for x in returns_b
        )

        if den_a <= 0 or den_b <= 0:
            return 0.0

        corr = num / math.sqrt(den_a * den_b)

        return max(-1.0, min(1.0, corr))

    except (TypeError, ValueError, OverflowError):
        return 0.0

    
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

    try:
        ts = int(float(ts))
    except (TypeError, ValueError, OverflowError):
        return 0

    # Якщо час прийшов у секундах, переводимо в мілісекунди
    if 0 < ts < 10_000_000_000:
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
BTC_CACHE_LOCK = asyncio.Lock()

async def get_btc_1h_candles(session):
    async with BTC_CACHE_LOCK:
        now = time.time()

        raw_btc = await get_cached_klines(
            session,
            "BTC-USDT",
            "1h",
            50
        )

        if not raw_btc:
            # Використовуємо попередні дані, якщо їм не більше 30 хвилин
            if (
                btc_cache["candles"]
                and now - btc_cache["time"] <= 1800
            ):
                print(
                    "[BTC CACHE] Використовуємо попередні дані BTC",
                    flush=True
                )
                return btc_cache["candles"]

            print(
                "[BTC CACHE ERROR] Немає актуальних даних BTC",
                flush=True
            )
            return []

        closed_btc = prepare_candles(
            raw_btc,
            60 * 60 * 1000
        )

        if len(closed_btc) < 24:
            print(
                "[BTC CACHE ERROR] Недостатньо закритих свічок BTC",
                flush=True
            )

            if (
                btc_cache["candles"]
                and now - btc_cache["time"] <= 1800
            ):
                return btc_cache["candles"]

            return []

        btc_cache["candles"] = closed_btc
        btc_cache["time"] = now

        return closed_btc
        
def prepare_candles(raw_candles, interval_ms):
    closed = get_closed_candles(raw_candles, interval_ms)

    result = []

    for c in closed:
        try:
            result.append({
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": float(
                    c.get("volume", c.get("Volume", c.get("vol", 0)))
                ),
                "time": candle_timestamp_ms(c)
            })
        except (TypeError, ValueError, KeyError):
            continue

    return result

def calculate_trade_levels(direction, entry, stop, tp1, tp2, tp3):
    risk_per_coin = abs(entry - stop)

    if risk_per_coin <= 0:
        return None

    # Максимальний ризик $0.60
    qty_by_risk = RISK_USD / risk_per_coin

    # Не використовуємо більше доступної маржі депозиту
    max_notional = DEPOSIT_USD * LEVERAGE
    qty_by_margin = max_notional / entry

    qty = min(qty_by_risk, qty_by_margin)

    if qty <= 0:
        return None

    notional = qty * entry
    margin = notional / LEVERAGE
    actual_risk = qty * risk_per_coin

    return {
        "pos_qty": qty,
        "notional_usd": notional,
        "margin_usd": margin,
        "actual_risk": actual_risk,
        "stop_loss": stop,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3
    }


def detect_range_sweep(candles_1h, candles_15m, report=None, symbol=None):
    """
    Пошук боковика на 1H.
    LONG: прокол нижньої межі та повернення всередину.
    SHORT: прокол верхньої межі та повернення всередину.
    """

    name = symbol or "Невідома монета"

    def log(category, reason):
        add_report_item(report, category, f"{name} — {reason}")

    if len(candles_1h) < RANGE_LOOKBACK_1H:
        log("other_rejections", "недостатньо свічок 1H для RANGE SWEEP")
        return None

    if len(candles_15m) < ENTRY_LOOKBACK_15M:
        log("other_rejections", "недостатньо свічок 15M для RANGE SWEEP")
        return None

    # Діапазон визначаємо за попередніми годинними свічками.
    # Останню годинну свічку не включаємо.
    range_candles = candles_1h[-RANGE_LOOKBACK_1H-1:-1]

    range_high = max(c["high"] for c in range_candles)
    range_low = min(c["low"] for c in range_candles)

    if range_high <= range_low:
        log("no_sweep", "некоректний діапазон 1H")
        return None

    range_size = range_high - range_low

    # Не беремо надто широкий діапазон
    avg_price = sum(c["close"] for c in range_candles) / len(range_candles)

    if range_size / avg_price > 0.08:
        log("no_sweep", "діапазон 1H занадто широкий")
        return None

    # Перевіряємо, чи ціна торкалася обох меж
    tolerance = range_size * 0.10

    low_touches = sum(
        1 for c in range_candles
        if c["low"] <= range_low + tolerance
    )

    high_touches = sum(
        1 for c in range_candles
        if c["high"] >= range_high - tolerance
    )

    if low_touches < MIN_RANGE_TOUCHES:
        log("no_sweep", "недостатньо торкань нижньої межі діапазону")
        return None

    if high_touches < MIN_RANGE_TOUCHES:
        log("no_sweep", "недостатньо торкань верхньої межі діапазону")
        return None

    recent = candles_15m[-ENTRY_LOOKBACK_15M:]

    long_sweep_seen = False
    short_sweep_seen = False

    # LONG: свіп нижньої межі
    for i in range(len(recent) - 1, max(-1, len(recent) - 9), -1):
        c = recent[i]

        if c["low"] < range_low and c["close"] > range_low:
            long_sweep_seen = True

            # Підтвердження: наступна закрита свічка утримує рівень
            if i + 1 < len(recent):
                confirm = recent[i + 1]

                if confirm["close"] > range_low:
                    stop = min(
                        x["low"] for x in recent[max(0, i-2):i+2]
                    )

                    return {
                        "strategy": "RANGE SWEEP",
                        "direction": "LONG",
                        "entry": confirm["close"],
                        "stop": stop - range_size * 0.03,
                        "tp1": (range_low + range_high) / 2,
                        "tp2": range_high,
                        "tp3": range_high + range_size * 0.5
                    }

    # SHORT: свіп верхньої межі
    for i in range(len(recent) - 1, max(-1, len(recent) - 9), -1):
        c = recent[i]

        if c["high"] > range_high and c["close"] < range_high:
            short_sweep_seen = True

            if i + 1 < len(recent):
                confirm = recent[i + 1]

                if confirm["close"] < range_high:
                    stop = max(
                        x["high"] for x in recent[max(0, i-2):i+2]
                    )

                    return {
                        "strategy": "RANGE SWEEP",
                        "direction": "SHORT",
                        "entry": confirm["close"],
                        "stop": stop + range_size * 0.03,
                        "tp1": (range_low + range_high) / 2,
                        "tp2": range_low,
                        "tp3": range_low - range_size * 0.5
                    }

    if long_sweep_seen or short_sweep_seen:
        log("waiting_confirmation", "RANGE SWEEP: свіп є, але немає підтвердження на 15M")
    else:
        log("no_sweep", "діапазон є, але свіпу межі на 15M немає")

    return None
    

def detect_impulse_retest(candles_1h, candles_15m, report=None, symbol=None):
    """
    Рахує окремі ретести, а не кількість свічок у зоні.

    LONG:
    1. Є сильна висхідна імпульсна свічка.
    2. Ціна повертається до зони імпульсу.
    3. Після ретесту ціна повинна відійти від зони.
    4. Лише після цього наступне повернення рахується новим ретестом.

    SHORT — дзеркально.
    """

    name = symbol or "Невідома монета"

    def log(category, reason):
        add_report_item(report, category, f"{name} — {reason}")

    if len(candles_1h) < 20 or len(candles_15m) < 20:
        log("other_rejections", "недостатньо свічок для IMPULSE RETEST")
        return None

    hourly = candles_1h[-20:]
    recent = candles_15m[-48:]

    avg_range = sum(
        x["high"] - x["low"] for x in hourly[:-5]
    ) / max(1, len(hourly[:-5]))

    if avg_range <= 0:
        log("other_rejections", "некоректний середній діапазон свічок 1H")
        return None

    impulse_found = False
    after_impulse_found = False
    retest_found = False
    confirmation_missing = False
    invalid_risk = False

    # Перевіряємо останні 5 годинних свічок
    for i in range(len(hourly) - 1, len(hourly) - 6, -1):
        impulse = hourly[i]

        body = abs(impulse["close"] - impulse["open"])
        candle_range = impulse["high"] - impulse["low"]

        if candle_range <= 0:
            continue

        # Перевіряємо, чи є сильний імпульс
        is_bullish_impulse = (
            impulse["close"] > impulse["open"]
            and body >= avg_range * 1.5
            and body / candle_range >= 0.60
        )

        is_bearish_impulse = (
            impulse["close"] < impulse["open"]
            and body >= avg_range * 1.5
            and body / candle_range >= 0.60
        )

        if not is_bullish_impulse and not is_bearish_impulse:
            continue

        impulse_found = True

        # Беремо тільки 15m свічки, які сформувалися
        # ПІСЛЯ імпульсної 1H свічки
        impulse_time = impulse["time"]

        after_impulse = [
            x for x in recent
            if x["time"] > impulse_time
        ]

        if len(after_impulse) < 5:
            continue

        after_impulse_found = True

        # LONG: висхідний імпульс
        if is_bullish_impulse:
            zone_low = impulse["open"]
            zone_high = impulse["open"] + body * 0.35
            reset_level = zone_high + avg_range * 0.10

            retests = 0
            armed = True
            last_retest_index = None

            for j, candle in enumerate(after_impulse):
                if not armed:
                    if candle["close"] > reset_level:
                        armed = True
                    continue

                touched = (
                    candle["low"] <= zone_high
                    and candle["high"] >= zone_low
                )

                if touched:
                    retests += 1
                    last_retest_index = j
                    armed = False

            last = after_impulse[-1]

            if retests >= MIN_RETESTS:
                retest_found = True

            if (
                retests >= MIN_RETESTS
                and last_retest_index is not None
                and last["close"] > zone_high
            ):
                touched_candles = [
                    x for x in after_impulse
                    if x["low"] <= zone_high
                    and x["high"] >= zone_low
                ]

                stop = min(x["low"] for x in touched_candles)
                stop -= avg_range * 0.10

                entry = last["close"]
                risk = entry - stop

                if risk <= 0:
                    invalid_risk = True
                    continue

                return {
                    "strategy": "IMPULSE RETEST",
                    "direction": "LONG",
                    "entry": entry,
                    "stop": stop,
                    "tp1": entry + risk,
                    "tp2": entry + risk * 2,
                    "tp3": entry + risk * 3,
                    "retests": retests
                }

            if retests >= MIN_RETESTS:
                confirmation_missing = True

        # SHORT: низхідний імпульс
        if is_bearish_impulse:
            zone_high = impulse["open"]
            zone_low = impulse["open"] - body * 0.35
            reset_level = zone_low - avg_range * 0.10

            retests = 0
            armed = True
            last_retest_index = None

            for j, candle in enumerate(after_impulse):
                if not armed:
                    if candle["close"] < reset_level:
                        armed = True
                    continue

                touched = (
                    candle["high"] >= zone_low
                    and candle["low"] <= zone_high
                )

                if touched:
                    retests += 1
                    last_retest_index = j
                    armed = False

            last = after_impulse[-1]

            if retests >= MIN_RETESTS:
                retest_found = True

            if (
                retests >= MIN_RETESTS
                and last_retest_index is not None
                and last["close"] < zone_low
            ):
                touched_candles = [
                    x for x in after_impulse
                    if x["high"] >= zone_low
                    and x["low"] <= zone_high
                ]

                stop = max(x["high"] for x in touched_candles)
                stop += avg_range * 0.10

                entry = last["close"]
                risk = stop - entry

                if risk <= 0:
                    invalid_risk = True
                    continue

                return {
                    "strategy": "IMPULSE RETEST",
                    "direction": "SHORT",
                    "entry": entry,
                    "stop": stop,
                    "tp1": entry - risk,
                    "tp2": entry - risk * 2,
                    "tp3": entry - risk * 3,
                    "retests": retests
                }

            if retests >= MIN_RETESTS:
                confirmation_missing = True

    # Діагностика, якщо сигнал не знайдено
    if invalid_risk:
        log("invalid_stop", "IMPULSE RETEST: некоректний ризик або стоп")
    elif not impulse_found:
        log("no_impulse_retest", "немає сильного імпульсу на 1H")
    elif not after_impulse_found:
        log("waiting_confirmation", "імпульс 1H є, але недостатньо свічок 15M після нього")
    elif not retest_found:
        log("waiting_confirmation", "імпульс є, але немає потрібної кількості ретестів")
    elif confirmation_missing:
        log("waiting_confirmation", "ретести є, але немає підтвердження продовження руху на 15M")
    else:
        log("waiting_confirmation", "умови IMPULSE RETEST ще не виконані")

    return None


def detect_rounding(candles_1h, candles_15m, report=None, symbol=None):
    """
    Ранній пошук заокруглення на 1H.
    Вхід біля основи після підтвердження розвороту на 15M.

    LONG:
    1. На 1H формується заокруглене дно.
    2. На 15M з'являється локальне дно.
    3. Формується вищий мінімум.
    4. Закрита свічка пробиває локальний максимум відскоку.

    SHORT — дзеркальна логіка.
    """

    name = symbol or "Невідома монета"

    def log(category, reason):
        add_report_item(report, category, f"{name} — {reason}")

    if len(candles_1h) < 30 or len(candles_15m) < 12:
        log("other_rejections", "недостатньо свічок для ROUNDING")
        return None

    c = candles_1h[-30:]

    left = c[:10]
    middle = c[10:20]
    right = c[20:]

    left_low = min(x["low"] for x in left)
    middle_low = min(x["low"] for x in middle)
    right_low = min(x["low"] for x in right)

    left_high = max(x["high"] for x in left)
    middle_high = max(x["high"] for x in middle)
    right_high = max(x["high"] for x in right)

    atr_1h = calculate_atr(candles_1h, 14)
    atr_15m = calculate_atr(candles_15m, 14)

    if atr_1h <= 0 or atr_15m <= 0:
        log("other_rejections", "некоректний ATR для ROUNDING")
        return None

    recent = candles_15m[-12:]
    last = recent[-1]
    prev = recent[-2]

    long_shape = (
        middle_low < left_low
        and middle_low < right_low
        and right_low > middle_low
    )

    short_shape = (
        middle_high > left_high
        and middle_high > right_high
        and right_high < middle_high
    )

    long_pivot_found = False
    short_pivot_found = False

    long_higher_low_found = False
    short_lower_high_found = False

    long_breakout = False
    short_breakout = False

    long_not_extended = False
    short_not_extended = False

    # LONG — ROUNDING BOTTOM
    if long_shape:
        pivot_idx = None

        for i in range(len(recent) - 3, 1, -1):
            if (
                recent[i]["low"] <= recent[i - 1]["low"]
                and recent[i]["low"] <= recent[i + 1]["low"]
            ):
                pivot_idx = i
                break

        if pivot_idx is not None:
            long_pivot_found = True

            pivot_low = recent[pivot_idx]["low"]

            rebound_high = max(
                x["high"] for x in recent[pivot_idx + 1:-1]
            )

            higher_low_found = any(
                recent[i]["low"] > pivot_low
                and recent[i]["low"] < rebound_high
                for i in range(pivot_idx + 2, len(recent) - 1)
            )

            long_higher_low_found = higher_low_found

            breakout = (
                last["close"] > rebound_high
                and last["close"] > last["open"]
            )

            long_breakout = breakout

            not_extended = (
                last["close"] - pivot_low <= atr_15m * 2.0
            )

            long_not_extended = not_extended

            if higher_low_found and breakout and not_extended:
                stop = pivot_low - atr_15m * 0.3
                risk = last["close"] - stop

                if risk <= 0:
                    log("invalid_stop", "ROUNDING LONG: некоректний ризик або стоп")
                    return None

                return {
                    "strategy": "ROUNDING BOTTOM EARLY",
                    "direction": "LONG",
                    "entry": last["close"],
                    "stop": stop,
                    "tp1": last["close"] + risk,
                    "tp2": last["close"] + 2 * risk,
                    "tp3": last["close"] + 3 * risk
                }

    # SHORT — ROUNDING TOP
    if short_shape:
        pivot_idx = None

        for i in range(len(recent) - 3, 1, -1):
            if (
                recent[i]["high"] >= recent[i - 1]["high"]
                and recent[i]["high"] >= recent[i + 1]["high"]
            ):
                pivot_idx = i
                break

        if pivot_idx is not None:
            short_pivot_found = True

            pivot_high = recent[pivot_idx]["high"]

            rebound_low = min(
                x["low"] for x in recent[pivot_idx + 1:-1]
            )

            lower_high_found = any(
                recent[i]["high"] < pivot_high
                and recent[i]["high"] > rebound_low
                for i in range(pivot_idx + 2, len(recent) - 1)
            )

            short_lower_high_found = lower_high_found

            breakout = (
                last["close"] < rebound_low
                and last["close"] < last["open"]
            )

            short_breakout = breakout

            not_extended = (
                pivot_high - last["close"] <= atr_15m * 2.0
            )

            short_not_extended = not_extended

            if lower_high_found and breakout and not_extended:
                stop = pivot_high + atr_15m * 0.3
                risk = stop - last["close"]

                if risk <= 0:
                    log("invalid_stop", "ROUNDING SHORT: некоректний ризик або стоп")
                    return None

                return {
                    "strategy": "ROUNDING TOP EARLY",
                    "direction": "SHORT",
                    "entry": last["close"],
                    "stop": stop,
                    "tp1": last["close"] - risk,
                    "tp2": last["close"] - 2 * risk,
                    "tp3": last["close"] - 3 * risk
                }

    # Діагностика LONG
    if long_shape:
        if not long_pivot_found:
            log("waiting_confirmation", "ROUNDING LONG: заокруглене дно на 1H є, але локального дна на 15M немає")
        elif not long_higher_low_found:
            log("waiting_confirmation", "ROUNDING LONG: локальне дно є, але немає вищого мінімуму")
        elif not long_breakout:
            log("waiting_confirmation", "ROUNDING LONG: структура є, але немає пробою локального максимуму")
        elif not long_not_extended:
            log("waiting_confirmation", "ROUNDING LONG: ціна вже відійшла від дна більш ніж на 2 ATR")

    # Діагностика SHORT
    if short_shape:
        if not short_pivot_found:
            log("waiting_confirmation", "ROUNDING SHORT: заокруглена вершина на 1H є, але локального максимуму на 15M немає")
        elif not short_lower_high_found:
            log("waiting_confirmation", "ROUNDING SHORT: локальна вершина є, але немає нижчого максимуму")
        elif not short_breakout:
            log("waiting_confirmation", "ROUNDING SHORT: структура є, але немає пробою локального мінімуму")
        elif not short_not_extended:
            log("waiting_confirmation", "ROUNDING SHORT: ціна вже відійшла від вершини більш ніж на 2 ATR")

    if not long_shape and not short_shape:
        log("no_rounding", "немає заокругленого дна або вершини на 1H")

    return None


def evaluate_double_breakout(
    candles,
    level,
    pivot,
    second_extreme,
    atr,
    avg_volume,
    direction
):
    """
    Визначає сценарій входу після пробою локального рівня.

    Повертає:
    (setup, status)

    status:
    strong_breakout
    retest
    waiting
    false_breakout
    no_breakout
    invalid_stop
    """

    buffer = atr * 0.05
    max_extension = atr * 1.5

    last_index = len(candles) - 1
    last = candles[-1]

    active_breakout = False
    breakout_index = None
    breakout_seen = False
    invalidated = False

    # Відновлюємо послідовність пробою за історією свічок
    for i, candle in enumerate(candles):
        if direction == "LONG":
            crossed = candle["close"] > level + buffer
            returned = candle["close"] <= level
        else:
            crossed = candle["close"] < level - buffer
            returned = candle["close"] >= level

        if not active_breakout and crossed:
            active_breakout = True
            breakout_index = i
            breakout_seen = True
            invalidated = False

        elif active_breakout and returned:
            active_breakout = False
            breakout_index = None
            invalidated = True

    if not active_breakout:
        if breakout_seen and invalidated:
            return None, "false_breakout"

        return None, "no_breakout"

    # Сценарій 1: пробій відбувся на останній свічці
    if breakout_index == last_index:
        body = (
            last["close"] - last["open"]
            if direction == "LONG"
            else last["open"] - last["close"]
        )

        strong = (
            body >= atr * 0.60
            and avg_volume > 0
            and last["volume"] >= avg_volume * 1.30
        )

        if not strong:
            return None, "waiting"

        entry = last["close"]

        if direction == "LONG":
            if entry - level > max_extension:
                return None, "waiting"

            stop = min(pivot, second_extreme) - atr * 0.30
            risk = entry - stop

            if risk <= 0:
                return None, "invalid_stop"

            return {
                "strategy": "DOUBLE BOTTOM — STRONG BREAKOUT",
                "direction": "LONG",
                "entry": entry,
                "stop": stop,
                "tp1": entry + risk,
                "tp2": entry + risk * 2,
                "tp3": entry + risk * 3
            }, "strong_breakout"

        else:
            if level - entry > max_extension:
                return None, "waiting"

            stop = max(pivot, second_extreme) + atr * 0.30
            risk = stop - entry

            if risk <= 0:
                return None, "invalid_stop"

            return {
                "strategy": "DOUBLE TOP — STRONG BREAKOUT",
                "direction": "SHORT",
                "entry": entry,
                "stop": stop,
                "tp1": entry - risk,
                "tp2": entry - risk * 2,
                "tp3": entry - risk * 3
            }, "strong_breakout"

    # Сценарій 2: пробій був раніше, зараз перевіряємо ретест
    if direction == "LONG":
        retest = (
            last["low"] <= level + buffer
            and last["close"] > level
            and last["close"] > last["open"]
        )

        if not retest:
            return None, "waiting"

        entry = last["close"]

        if entry - level > max_extension:
            return None, "waiting"

        stop = min(pivot, second_extreme) - atr * 0.30
        risk = entry - stop

        if risk <= 0:
            return None, "invalid_stop"

        return {
            "strategy": "DOUBLE BOTTOM — RETEST",
            "direction": "LONG",
            "entry": entry,
            "stop": stop,
            "tp1": entry + risk,
            "tp2": entry + risk * 2,
            "tp3": entry + risk * 3
        }, "retest"

    else:
        retest = (
            last["high"] >= level - buffer
            and last["close"] < level
            and last["close"] < last["open"]
        )

        if not retest:
            return None, "waiting"

        entry = last["close"]

        if level - entry > max_extension:
            return None, "waiting"

        stop = max(pivot, second_extreme) + atr * 0.30
        risk = stop - entry

        if risk <= 0:
            return None, "invalid_stop"

        return {
            "strategy": "DOUBLE TOP — RETEST",
            "direction": "SHORT",
            "entry": entry,
            "stop": stop,
            "tp1": entry - risk,
            "tp2": entry - risk * 2,
            "tp3": entry - risk * 3
        }, "retest"
    

def detect_double_bottom_top(candles_1h, candles_15m, report=None, symbol=None):
    """
    DOUBLE BOTTOM / DOUBLE TOP

    1H:
    - Знаходимо два близькі мінімуми або максимуми.
    - Між ними має бути виразний відскок / відкат.

    15M LONG:
    - Локальне дно після другого мінімуму.
    - Відскок.
    - Вищий мінімум.
    - Пробій локального максимуму.

    15M SHORT:
    - Локальна вершина після другого максимуму.
    - Відкат.
    - Нижчий максимум.
    - Пробій локального мінімуму.

    Вхід:
    - Сильний пробій: ранній вхід після закриття свічки.
    - Слабкий пробій: очікування ретесту.
    - Підтверджений ретест: вхід.
    - Хибний пробій: відмова від входу.
    """

    name = symbol or "Невідома монета"

    def log(category, reason):
        add_report_item(report, category, f"{name} — {reason}")

    if len(candles_1h) < 30 or len(candles_15m) < 20:
        log("other_rejections", "недостатньо свічок для DOUBLE BOTTOM/TOP")
        return None

    # Початкові параметри стратегії
    hourly = candles_1h[-40:]
    recent_15m = candles_15m[-48:]

    atr_1h = calculate_atr(candles_1h, 14)
    atr_15m = calculate_atr(candles_15m, 14)

    if atr_1h <= 0 or atr_15m <= 0:
        log("other_rejections", "некоректний ATR для DOUBLE BOTTOM/TOP")
        return None

    LEVEL_TOLERANCE = atr_1h * 0.5
    MIN_BOUNCE = atr_1h * 1.0

    MIN_SEPARATION = 4
    MAX_SEPARATION = 24

    # Пошук локальних мінімумів та максимумів на 1H
    lows = []
    highs = []

    for i in range(1, len(hourly) - 1):
        if (
            hourly[i]["low"] <= hourly[i - 1]["low"]
            and hourly[i]["low"] <= hourly[i + 1]["low"]
        ):
            lows.append(i)

        if (
            hourly[i]["high"] >= hourly[i - 1]["high"]
            and hourly[i]["high"] >= hourly[i + 1]["high"]
        ):
            highs.append(i)

    # Шукаємо останню придатну пару мінімумів
    bottom_pair = None

    for second_idx in reversed(lows):
        if second_idx < len(hourly) - 12:
            continue

        for first_idx in reversed(lows):
            if first_idx >= second_idx:
                continue

            separation = second_idx - first_idx

            if not MIN_SEPARATION <= separation <= MAX_SEPARATION:
                continue

            first_low = hourly[first_idx]["low"]
            second_low = hourly[second_idx]["low"]

            if abs(first_low - second_low) > LEVEL_TOLERANCE:
                continue

            neckline = max(
                x["high"]
                for x in hourly[first_idx + 1:second_idx]
            )

            if neckline - max(first_low, second_low) < MIN_BOUNCE:
                continue

            bottom_pair = (
                first_idx,
                second_idx,
                first_low,
                second_low,
                neckline
            )
            break

        if bottom_pair is not None:
            break

    # Шукаємо останню придатну пару максимумів
    top_pair = None

    for second_idx in reversed(highs):
        if second_idx < len(hourly) - 12:
            continue

        for first_idx in reversed(highs):
            if first_idx >= second_idx:
                continue

            separation = second_idx - first_idx

            if not MIN_SEPARATION <= separation <= MAX_SEPARATION:
                continue

            first_high = hourly[first_idx]["high"]
            second_high = hourly[second_idx]["high"]

            if abs(first_high - second_high) > LEVEL_TOLERANCE:
                continue

            neckline = min(
                x["low"]
                for x in hourly[first_idx + 1:second_idx]
            )

            if min(first_high, second_high) - neckline < MIN_BOUNCE:
                continue

            top_pair = (
                first_idx,
                second_idx,
                first_high,
                second_high,
                neckline
            )
            break

        if top_pair is not None:
            break

    if bottom_pair is None and top_pair is None:
        log(
            "no_double_pattern",
            "немає підтвердженого подвійного дна або вершини на 1H"
        )
        return None

    # Середній об'єм попередніх 15M свічок
    volume_values = [
        x["volume"] for x in candles_15m[-21:-1]
    ]

    avg_volume = (
        sum(volume_values) / len(volume_values)
        if volume_values else 0
    )

    # --------------------------------------------------
    # LONG — DOUBLE BOTTOM
    # --------------------------------------------------

    if bottom_pair is not None:
        _, second_idx, first_low, second_low, _ = bottom_pair

        second_time = hourly[second_idx]["time"]

        after_pattern = [
            x for x in recent_15m
            if x["time"] > second_time
        ]

        if len(after_pattern) < 6:
            log(
                "waiting_confirmation",
                "DOUBLE BOTTOM LONG: недостатньо свічок 15M після другого дна"
            )
        else:
            pivot_idx = None

            # Шукаємо останнє локальне дно на 15M
            for i in range(len(after_pattern) - 2, 0, -1):
                if (
                    after_pattern[i]["low"] <= after_pattern[i - 1]["low"]
                    and after_pattern[i]["low"] <= after_pattern[i + 1]["low"]
                ):
                    pivot_idx = i
                    break

            if pivot_idx is None or pivot_idx + 3 >= len(after_pattern):
                log(
                    "waiting_confirmation",
                    "DOUBLE BOTTOM LONG: немає локального дна на 15M"
                )
            else:
                pivot_low = after_pattern[pivot_idx]["low"]

                # Знаходимо максимум відскоку після локального дна
                rebound_idx = max(
                    range(pivot_idx + 1, len(after_pattern) - 1),
                    key=lambda i: after_pattern[i]["high"]
                )

                rebound_high = after_pattern[rebound_idx]["high"]

                # Перевіряємо формування вищого мінімуму
                higher_low_found = any(
                    after_pattern[i]["low"] > pivot_low
                    and after_pattern[i]["low"] < rebound_high
                    for i in range(rebound_idx + 1, len(after_pattern) - 1)
                )

                if not higher_low_found:
                    log(
                        "waiting_confirmation",
                        "DOUBLE BOTTOM LONG: немає вищого мінімуму на 15M"
                    )
                else:
                    setup, status = evaluate_double_breakout(
                        candles=after_pattern,
                        level=rebound_high,
                        pivot=pivot_low,
                        second_extreme=second_low,
                        atr=atr_15m,
                        avg_volume=avg_volume,
                        direction="LONG"
                    )

                    if setup is not None:
                        return setup

                    if status == "false_breakout":
                        log(
                            "waiting_confirmation",
                            "DOUBLE BOTTOM LONG: хибний пробій, ціна повернулася під рівень"
                        )

                    elif status == "waiting":
                        log(
                            "waiting_confirmation",
                            "DOUBLE BOTTOM LONG: слабкий пробій або очікування ретесту"
                        )

                    elif status == "invalid_stop":
                        log(
                            "invalid_stop",
                            "DOUBLE BOTTOM LONG: некоректний стоп"
                        )

                    else:
                        log(
                            "waiting_confirmation",
                            "DOUBLE BOTTOM LONG: очікуємо пробій локального максимуму"
                        )

    # --------------------------------------------------
    # SHORT — DOUBLE TOP
    # --------------------------------------------------

    if top_pair is not None:
        _, second_idx, first_high, second_high, _ = top_pair

        second_time = hourly[second_idx]["time"]

        after_pattern = [
            x for x in recent_15m
            if x["time"] > second_time
        ]

        if len(after_pattern) < 6:
            log(
                "waiting_confirmation",
                "DOUBLE TOP SHORT: недостатньо свічок 15M після другої вершини"
            )
        else:
            pivot_idx = None

            # Шукаємо останню локальну вершину на 15M
            for i in range(len(after_pattern) - 2, 0, -1):
                if (
                    after_pattern[i]["high"] >= after_pattern[i - 1]["high"]
                    and after_pattern[i]["high"] >= after_pattern[i + 1]["high"]
                ):
                    pivot_idx = i
                    break

            if pivot_idx is None or pivot_idx + 3 >= len(after_pattern):
                log(
                    "waiting_confirmation",
                    "DOUBLE TOP SHORT: немає локальної вершини на 15M"
                )
            else:
                pivot_high = after_pattern[pivot_idx]["high"]

                # Знаходимо мінімум відкату після локальної вершини
                rebound_idx = min(
                    range(pivot_idx + 1, len(after_pattern) - 1),
                    key=lambda i: after_pattern[i]["low"]
                )

                rebound_low = after_pattern[rebound_idx]["low"]

                # Перевіряємо формування нижчого максимуму
                lower_high_found = any(
                    after_pattern[i]["high"] < pivot_high
                    and after_pattern[i]["high"] > rebound_low
                    for i in range(rebound_idx + 1, len(after_pattern) - 1)
                )

                if not lower_high_found:
                    log(
                        "waiting_confirmation",
                        "DOUBLE TOP SHORT: немає нижчого максимуму на 15M"
                    )
                else:
                    setup, status = evaluate_double_breakout(
                        candles=after_pattern,
                        level=rebound_low,
                        pivot=pivot_high,
                        second_extreme=second_high,
                        atr=atr_15m,
                        avg_volume=avg_volume,
                        direction="SHORT"
                    )

                    if setup is not None:
                        return setup

                    if status == "false_breakout":
                        log(
                            "waiting_confirmation",
                            "DOUBLE TOP SHORT: хибний пробій, ціна повернулася над рівень"
                        )

                    elif status == "waiting":
                        log(
                            "waiting_confirmation",
                            "DOUBLE TOP SHORT: слабкий пробій або очікування ретесту"
                        )

                    elif status == "invalid_stop":
                        log(
                            "invalid_stop",
                            "DOUBLE TOP SHORT: некоректний стоп"
                        )

                    else:
                        log(
                            "waiting_confirmation",
                            "DOUBLE TOP SHORT: очікуємо пробій локального мінімуму"
                        )

    return None


async def analyze_signal(session, symbol, report=None):
    try:
        now = time.time()

        def log(category, reason):
            add_report_item(
                report,
                category,
                f"{symbol} — {reason}"
            )

        # Перевірка cooldown
        if (
            symbol in signal_cooldowns
            and now - signal_cooldowns[symbol] < COOLDOWN_PERIOD
        ):
            log("cooldown", "пропущено через cooldown")
            return None

        if symbol == "BTC-USDT":
            return None

        # =====================================================
        # ЕТАП 1. СПОЧАТКУ ЗАВАНТАЖУЄМО ТІЛЬКИ 15m СВІЧКИ
        # =====================================================

        raw_15m = await get_cached_klines(
            session,
            symbol,
            "15m",
            100
        )

        if not raw_15m:
            log("other_rejections", "не вдалося отримати 15m свічки")
            return None

        candles_15m = prepare_candles(
            raw_15m,
            15 * 60 * 1000
        )

        if len(candles_15m) < 48:
            log(
                "other_rejections",
                "недостатньо підготовлених 15m свічок"
            )
            return None

        # =====================================================
        # ЕТАП 2. ПЕРЕВІРКА СПЛЕСКУ ОБ'ЄМУ
        # =====================================================

        # Останні 3 свічки проти попередніх 20
        historical = candles_15m[-23:-3]
        recent = candles_15m[-3:]

        if not historical or not recent:
            log(
                "other_rejections",
                "немає даних для перевірки об'єму"
            )
            return None

        avg_volume = (
            sum(x["volume"] for x in historical) / len(historical)
        )

        recent_volume = sum(
            x["volume"] for x in recent
        )

        if avg_volume <= 0:
            log("low_volume", "некоректний середній об'єм")
            return None

        if recent_volume < avg_volume * 3 * VOLUME_MULTIPLIER:
            log("low_volume", "об'єм нижче необхідного рівня")
            return None

        # =====================================================
        # ЕТАП 3. ТІЛЬКИ ТЕПЕР ЗАВАНТАЖУЄМО 1h СВІЧКИ
        # =====================================================

        raw_1h = await get_cached_klines(
            session,
            symbol,
            "1h",
            100
        )

        if not raw_1h:
            log("other_rejections", "не вдалося отримати 1h свічки")
            return None

        candles_1h = prepare_candles(
            raw_1h,
            60 * 60 * 1000
        )

        if len(candles_1h) < 30:
            log(
                "other_rejections",
                "недостатньо підготовлених 1h свічок"
            )
            return None

        # =====================================================
        # ЕТАП 4. ПЕРЕВІРКА КОРЕЛЯЦІЇ З BTC
        # =====================================================

        btc_1h = await get_btc_1h_candles(session)

        if len(btc_1h) >= 24:

            btc_map = {
                candle_timestamp_ms(x): float(x["close"])
                for x in btc_1h
            }

            coin_map = {
                x["time"]: float(x["close"])
                for x in candles_1h
            }

            # Зіставляємо свічки за однаковим часом
            common = sorted(set(btc_map) & set(coin_map))

            # Беремо останні 24 спільні свічки
            common = common[-24:]

            if len(common) >= 15:

                corr = calculate_correlation(
                    [btc_map[t] for t in common],
                    [coin_map[t] for t in common]
                )

                # Відсікаємо тільки сильну позитивну кореляцію
                if corr >= 0.60:
                    log(
                        "high_correlation",
                        f"кореляція з BTC {corr:.2f}"
                    )
                    return None

        # =====================================================
        # ЕТАП 5. ПЕРЕВІРКА ЧОТИРЬОХ СТРАТЕГІЙ
        # =====================================================

        detectors = [
            detect_range_sweep,
            detect_impulse_retest,
            detect_rounding,
            detect_double_bottom_top
        ]

        setups = []

        for detector in detectors:
            try:
                setup = detector(
                    candles_1h,
                    candles_15m,
                    report,
                    symbol
                )

                if setup is not None:
                    setups.append(setup)

            except Exception as e:
                log(
                    "errors",
                    f"{detector.__name__}: {e}"
                )

                print(
                    f"[STRATEGY ERROR] {symbol} "
                    f"{detector.__name__}: {e}",
                    flush=True
                )

                continue

        if not setups:
            return None

        # =====================================================
        # ЕТАП 6. ОТРИМУЄМО АКТУАЛЬНУ ЦІНУ
        # =====================================================

        ticker_url = (
            f"{BINGX_BASE_URL}/openApi/swap/v2/quote/ticker"
        )

        async with session.get(
            ticker_url,
            params={"symbol": symbol},
            timeout=5
        ) as resp:

            if resp.status != 200:
                log(
                    "other_rejections",
                    "помилка отримання актуальної ціни"
                )
                return None

            ticker_data = await resp.json()

        if ticker_data.get("code") != 0:
            log(
                "other_rejections",
                "BingX не повернув актуальну ціну"
            )
            return None

        ticker = ticker_data.get("data")

        if isinstance(ticker, list):
            ticker = ticker[0] if ticker else None

        if not ticker or not ticker.get("lastPrice"):
            log(
                "other_rejections",
                "відсутня актуальна ціна"
            )
            return None

        entry = float(ticker["lastPrice"])

        # =====================================================
        # ЕТАП 7. ПЕРЕВІРКА СТОПА ТА TAKE PROFIT
        # =====================================================

        for setup in setups:

            direction = setup.get("direction")

            if direction not in ("LONG", "SHORT"):
                log(
                    "other_rejections",
                    f"{setup.get('strategy', 'UNKNOWN')}: невідомий напрямок"
                )
                continue

            try:
                stop = float(setup["stop"])

                # Стоп повинен бути з правильного боку від входу
                if direction == "LONG":

                    if stop >= entry:
                        log(
                            "invalid_stop",
                            f"{setup['strategy']}: стоп не нижче входу"
                        )
                        continue

                    risk = entry - stop

                else:

                    if stop <= entry:
                        log(
                            "invalid_stop",
                            f"{setup['strategy']}: стоп не вище входу"
                        )
                        continue

                    risk = stop - entry

                if risk <= 0:
                    log(
                        "invalid_stop",
                        f"{setup['strategy']}: некоректний ризик"
                    )
                    continue

                # Перераховуємо цілі від актуальної ціни входу
                if direction == "LONG":

                    tp1 = entry + risk
                    tp2 = entry + risk * 2
                    tp3 = entry + risk * 3

                else:

                    tp1 = entry - risk
                    tp2 = entry - risk * 2
                    tp3 = entry - risk * 3

                # Перевіряємо правильність цілей
                if direction == "LONG":

                    if not (entry < tp1 < tp2 < tp3):
                        log(
                            "invalid_tp",
                            f"{setup['strategy']}: неправильний порядок TP"
                        )
                        continue

                else:

                    if not (entry > tp1 > tp2 > tp3):
                        log(
                            "invalid_tp",
                            f"{setup['strategy']}: неправильний порядок TP"
                        )
                        continue

                levels = calculate_trade_levels(
                    direction,
                    entry,
                    stop,
                    tp1,
                    tp2,
                    tp3
                )

                if levels is None:
                    log(
                        "invalid_tp",
                        f"{setup['strategy']}: не вдалося розрахувати торгові рівні"
                    )
                    continue

                # Успішно сформований сигнал
                signal_cooldowns[symbol] = time.time()

                print(
                    f"[SIGNAL] {symbol} {direction} "
                    f"{setup['strategy']} "
                    f"Entry: {entry} Stop: {stop}",
                    flush=True
                )

                return {
                    "symbol": symbol,
                    "signal": f"{direction} — {setup['strategy']}",
                    "strategy": setup["strategy"],
                    "direction": direction,
                    "entry": entry,
                    **levels
                }

            except (
                KeyError,
                TypeError,
                ValueError,
                ZeroDivisionError
            ) as e:

                log(
                    "errors",
                    f"{setup.get('strategy', 'UNKNOWN')}: {e}"
                )

                print(
                    f"[SETUP ERROR] {symbol} "
                    f"{setup.get('strategy', 'UNKNOWN')}: {e}",
                    flush=True
                )
                continue

        return None

    except Exception as e:

        add_report_item(
            report,
            "errors",
            f"{symbol} — загальна помилка: {e}"
        )

        print(
            f"[ERROR in {symbol}] {e}",
            flush=True
        )

        return None
        
def create_scan_report():
    return {
        "high_correlation": [],
        "low_volume": [],
        "no_sweep": [],
        "no_rounding": [],
        "no_impulse_retest": [],
        "waiting_confirmation": [],
        "invalid_stop": [],
        "invalid_tp": [],
        "signals": [],
        "errors": [],
        "cooldown": [],
        "other_rejections": [],
        "no_double_pattern": [],
    }


def add_report_item(report, category, message):
    if report is None or category not in report:
        return

    if message not in report[category]:
        report[category].append(message)


def format_scan_report(report):
    sections = [
        ("🎯 ТОРГОВІ СИГНАЛИ", "signals"),
        ("📊 ВИСОКА КОРЕЛЯЦІЯ З BTC", "high_correlation"),
        ("📉 НЕДОСТАТНІЙ ОБ'ЄМ", "low_volume"),
        ("🔎 НЕМАЄ RANGE SWEEP", "no_sweep"),
        ("🔎 НЕМАЄ ROUNDING", "no_rounding"),
        ("🔎 НЕМАЄ IMPULSE RETEST", "no_impulse_retest"),
        ("🔎 НЕМАЄ DOUBLE BOTTOM / TOP", "no_double_pattern"),
        ("⏳ Є СТРУКТУРА, АЛЕ НЕМАЄ ПІДТВЕРДЖЕННЯ", "waiting_confirmation"),
        ("🛑 НЕКОРЕКТНИЙ STOP LOSS", "invalid_stop"),
        ("🎯 НЕКОРЕКТНІ TAKE PROFIT", "invalid_tp"),
        ("⚠️ ІНШІ ВІДХИЛЕННЯ", "other_rejections"),
        ("🔁 ПРОПУЩЕНО ЧЕРЕЗ COOLDOWN", "cooldown"),
        ("❌ ПОМИЛКИ", "errors")
    ]

    lines = ["📋 ЗВІТ СКАНУВАННЯ РИНКУ"]

    for title, key in sections:
        items = report.get(key, [])

        lines.append("")
        lines.append(f"{title} ({len(items)}):")

        if items:
            lines.extend(f"• {item}" for item in items)
        else:
            lines.append("• Немає")

    return "\n".join(lines)
    
# Захист від одночасного запуску декількох сканувань
SCAN_LOCK = asyncio.Lock()

# =====================================================
# ОСНОВНЕ СКАНУВАННЯ РИНКУ
# Добовий об'єм уже відфільтрований у main_bot_loop()
# =====================================================

async def scan_5min_loop(session, symbols):

    # Не запускаємо нове сканування, якщо попереднє ще працює
    if SCAN_LOCK.locked():
        print(
            "[INFO] Попереднє сканування ще триває. Новий запуск пропущено.",
            flush=True
        )
        return

    async with SCAN_LOCK:

        print(
            "[INFO] Початок сканування ринку...",
            flush=True
        )

        report = create_scan_report()

        # symbols уже містить тільки монети з 24h оборотом >= $400 000
        filtered_symbols = list(symbols)

        print(
            f"[INFO] Монет після фільтра добового обороту: "
            f"{len(filtered_symbols)}",
            flush=True
        )

        if not filtered_symbols:
            add_report_item(
                report,
                "other_rejections",
                "Немає монет, які пройшли фільтр добового обороту."
            )

            await send_telegram(format_scan_report(report))

            print(
                "[INFO] Немає монет для сканування.",
                flush=True
            )
            return

        # Аналізуємо монети пакетами
        batch_size = 10
        total_symbols = len(filtered_symbols)

        print(
            f"[INFO] До аналізу: {total_symbols} монет",
            flush=True
        )

        for i in range(0, total_symbols, batch_size):

            batch = filtered_symbols[i:i + batch_size]

            print(
                f"[INFO] Пакет {i // batch_size + 1}: "
                f"{len(batch)} монет",
                flush=True
            )

            tasks = [
                analyze_signal(session, sym, report)
                for sym in batch
            ]

            try:
                results = await asyncio.gather(
                    *tasks,
                    return_exceptions=True
                )

                for sym, setup in zip(batch, results):

                    if isinstance(setup, Exception):

                        add_report_item(
                            report,
                            "errors",
                            f"{sym} — {str(setup)}"
                        )

                        print(
                            f"[SCAN ERROR] {sym}: {setup}",
                            flush=True
                        )
                        continue

                    if not isinstance(setup, dict):
                        continue

                    add_report_item(
                        report,
                        "signals",
                        f"{setup['symbol']} — "
                        f"{setup['direction']} — "
                        f"{setup['strategy']}"
                    )

                    msg = (
                        f"🎯 **НОВИЙ СИГНАЛ**\n"
                        f"🪙 Монета: `{setup['symbol']}`\n"
                        f"📊 Стратегія: `{setup['strategy']}`\n"
                        f"📈 Напрямок: `{setup['direction']}`\n\n"
                        f"🟢 Вхід: `{setup['entry']:.8f}`\n"
                        f"🔴 Стоп: `{setup['stop_loss']:.8f}`\n\n"
                        f"🎯 TP1: `{setup['tp1']:.8f}` — закрити 50%\n"
                        f"🎯 TP2: `{setup['tp2']:.8f}` — закрити 25%\n"
                        f"🎯 TP3: `{setup['tp3']:.8f}` — закрити 25%\n\n"
                        f"📦 Кількість: `{setup['pos_qty']:.6f}`\n"
                        f"💵 Обсяг позиції: "
                        f"`{setup['notional_usd']:.2f} USD`\n"
                        f"🏦 Маржа: `{setup['margin_usd']:.2f} USD`\n"
                        f"🛡️ Ризик на стоп: "
                        f"`${setup['actual_risk']:.2f}`\n\n"
                        f"⏳ Очікування ручного відкриття."
                    )

                    await send_telegram(msg)

            except Exception as e:

                print(
                    f"[BATCH ERROR] {e}",
                    flush=True
                )

                add_report_item(
                    report,
                    "errors",
                    f"Помилка пакета {i // batch_size + 1}: {e}"
                )

            # Пауза між пакетами
            await asyncio.sleep(1)

        # Формуємо загальний звіт
        report_msg = format_scan_report(report)

        await send_telegram(report_msg)

        print(
            "[INFO] Сканування ринку завершено.",
            flush=True
        )

        print(
            f"[INFO] Знайдено сигналів: {len(report['signals'])}",
            flush=True
        )


async def main_bot_loop():

    await send_telegram(
        "🚀 Торговий бот успішно запущено!\n"
        "• Щоденний звіт по закритих свіпах активний\n"
        "• Перевірка KDJ + Об'єм "
        "(24h Vol >= $400k) кожні 5 хв активна."
    )

    last_daily_report_date = None
    scan_interval = 300

    while True:

        cycle_start = time.monotonic()

        try:
            async with aiohttp.ClientSession() as session:

                cycle_ok = True

                # 1. Отримуємо всі тікери BingX
                ticker_url = (
                    f"{BINGX_BASE_URL}/openApi/swap/v2/quote/ticker"
                )

                async with session.get(
                    ticker_url,
                    timeout=10
                ) as resp:

                    if resp.status != 200:
                        print(
                            f"[TICKER HTTP ERROR] HTTP {resp.status}",
                            flush=True
                        )
                        cycle_ok = False

                    else:
                        res = await resp.json()

                # 2. Перевіряємо отримані тікери
                if cycle_ok:

                    tickers_data = (
                        res.get("data", [])
                        if isinstance(res, dict)
                        else []
                    )

                    if isinstance(tickers_data, dict):
                        tickers_data = [tickers_data]

                    if (
                        not isinstance(tickers_data, list)
                        or not tickers_data
                    ):
                        print(
                            "[TICKER ERROR] Порожня або некоректна відповідь",
                            flush=True
                        )
                        cycle_ok = False

                # 3. Фільтруємо монети за добовим оборотом
                if cycle_ok:

                    valid_volume_symbols = set()

                    for t in tickers_data:

                        if not isinstance(t, dict):
                            continue

                        sym = t.get("symbol", "")

                        if not sym:
                            continue

                        try:
                            quote_volume = float(
                                t.get("quoteVolume") or 0
                            )

                            # Якщо quoteVolume відсутній,
                            # розраховуємо оборот через ціну
                            if quote_volume <= 0:

                                base_volume = float(
                                    t.get("volume") or 0
                                )

                                price = float(
                                    t.get("lastPrice")
                                    or t.get("price")
                                    or 0
                                )

                                quote_volume = base_volume * price

                            if quote_volume >= MIN_24H_VOLUME_USD:
                                valid_volume_symbols.add(sym)

                        except (
                            TypeError,
                            ValueError,
                            OverflowError
                        ):
                            continue

                # 4. Отримуємо список доступних контрактів
                if cycle_ok:

                    contracts_url = (
                        f"{BINGX_BASE_URL}/openApi/swap/v2/quote/contracts"
                    )

                    async with session.get(
                        contracts_url,
                        timeout=10
                    ) as resp:

                        if resp.status != 200:
                            print(
                                f"[CONTRACTS HTTP ERROR] HTTP {resp.status}",
                                flush=True
                            )
                            cycle_ok = False

                        else:
                            res_c = await resp.json()

                # 5. Перевіряємо відповідь контрактів
                if cycle_ok:

                    if isinstance(res_c, list):
                        contracts_data = res_c

                    elif isinstance(res_c, dict):
                        contracts_data = res_c.get("data", [])

                    else:
                        contracts_data = []

                    if (
                        not isinstance(contracts_data, list)
                        or not contracts_data
                    ):
                        print(
                            "[CONTRACTS ERROR] Порожня або некоректна відповідь",
                            flush=True
                        )
                        cycle_ok = False

                # 6. Формуємо список монет для сканування
                if cycle_ok:

                    exclude_patterns = (
                        "NCF",
                        "NCS",
                        "NCC",
                        "-USDC"
                    )

                    symbols = [
                        item["symbol"]
                        for item in contracts_data
                        if (
                            isinstance(item, dict)
                            and item.get("symbol")
                            and not any(
                                p in item["symbol"]
                                for p in exclude_patterns
                            )
                            and item["symbol"] in valid_volume_symbols
                        )
                    ]

                    print(
                        f"[INFO] Відфільтровано монет "
                        f"з об'ємом >= $400K: {len(symbols)}",
                        flush=True
                    )

                    # 7. Перевірка щоденного звіту
                    now_utc = datetime.now(timezone.utc)
                    today_str = now_utc.strftime("%Y-%m-%d")

                    if (
                        last_daily_report_date != today_str
                        and now_utc.minute >= 5
                    ):
                        await job_daily_sweep_report(
                            session,
                            symbols
                        )

                        last_daily_report_date = today_str

                    # 8. Запускаємо сканування ринку
                    await scan_5min_loop(
                        session,
                        symbols
                    )

                else:
                    print(
                        "[INFO] Цикл пропущено через помилку API. "
                        "Наступна спроба за розкладом.",
                        flush=True
                    )

        except Exception as e:

            import traceback

            print(
                f"[ERROR] Помилка в основному циклі: {e}\n"
                f"{traceback.format_exc()}",
                flush=True
            )

        # 9. Дотримуємося інтервалу між початками циклів
        elapsed = time.monotonic() - cycle_start
        sleep_time = max(0, scan_interval - elapsed)

        print(
            f"[INFO] Наступний цикл через {sleep_time:.1f} секунд",
            flush=True
        )

        await asyncio.sleep(sleep_time)
        
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
                
