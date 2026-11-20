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
POSITION_BUDGET_USD = 3.0
RISK_USD = 0.60

# Налаштування нової стратегії
RANGE_LOOKBACK_1H = 72
ENTRY_LOOKBACK_15M = 48
VOLUME_SPIKE_MULTIPLIER = 4.0
VOLUME_ALERT_LAST_PAIR = {}


MIN_24H_VOLUME_USD = 400_000

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
KLINES_SEMAPHORE = asyncio.Semaphore(1)
KLINES_REQUEST_LOCK = asyncio.Lock()

KLINES_LAST_REQUEST = 0.0
KLINES_MIN_INTERVAL = 0.5

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

KLINES_STATS_WINDOW_START = time.monotonic()
KLINES_STATS_SENT = {}
KLINES_STATS_HTTP = {}
KLINES_STATS_API = {}
KLINES_STATS_100410 = 0
KLINES_BLOCK_REASON = "Причина ще не зафіксована"


async def get_klines(session, symbol, interval, limit=100):
    global KLINES_LAST_REQUEST
    global KLINES_BLOCKED_UNTIL
    global KLINES_STATS_WINDOW_START
    global KLINES_STATS_SENT
    global KLINES_STATS_HTTP
    global KLINES_STATS_API
    global KLINES_STATS_100410
    global KLINES_BLOCK_REASON

    url = f"{BINGX_BASE_URL}/openApi/swap/v2/quote/klines"

    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }

    try:
        async with KLINES_SEMAPHORE:

            async with KLINES_REQUEST_LOCK:

                now = time.monotonic()

                if now < KLINES_BLOCKED_UNTIL:
                    remaining = KLINES_BLOCKED_UNTIL - now

                    print(
                        f"[KLINES COOLDOWN] {symbol} {interval}: "
                        f"залишилось {remaining:.0f} сек. | "
                        f"Причина: {KLINES_BLOCK_REASON}",
                        flush=True
                    )
                    return []

                elapsed = now - KLINES_LAST_REQUEST
                wait_time = KLINES_MIN_INTERVAL - elapsed

                if wait_time > 0:
                    await asyncio.sleep(wait_time)

                now = time.monotonic()

                if now < KLINES_BLOCKED_UNTIL:
                    remaining = KLINES_BLOCKED_UNTIL - now

                    print(
                        f"[KLINES COOLDOWN AFTER WAIT] "
                        f"{symbol} {interval}: "
                        f"залишилось {remaining:.0f} сек. | "
                        f"Причина: {KLINES_BLOCK_REASON}",
                        flush=True
                    )
                    return []

                KLINES_LAST_REQUEST = time.monotonic()

            # Облік тільки реально відправлених HTTP-запитів
            KLINES_STATS_SENT[interval] = (
                KLINES_STATS_SENT.get(interval, 0) + 1
            )

            # Щохвилини друкуємо статистику
            stats_now = time.monotonic()

            if stats_now - KLINES_STATS_WINDOW_START >= 60:

                print(
                    f"[KLINES STATS] Остання хвилина | "
                    f"Відправлено: {KLINES_STATS_SENT} | "
                    f"HTTP: {KLINES_STATS_HTTP} | "
                    f"API: {KLINES_STATS_API} | "
                    f"100410: {KLINES_STATS_100410}",
                    flush=True
                )

                KLINES_STATS_WINDOW_START = stats_now
                KLINES_STATS_SENT = {}
                KLINES_STATS_HTTP = {}
                KLINES_STATS_API = {}
                KLINES_STATS_100410 = 0

            async with session.get(
                url,
                params=params,
                timeout=10
            ) as resp:

                KLINES_STATS_HTTP[str(resp.status)] = (
                    KLINES_STATS_HTTP.get(str(resp.status), 0) + 1
                )

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

                KLINES_STATS_API[error_code] = (
                    KLINES_STATS_API.get(error_code, 0) + 1
                )

                if error_code == "100410":

                    KLINES_STATS_100410 += 1

                    error_msg = str(
                        data.get("msg", "Unknown error")
                    )

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

                        KLINES_BLOCK_REASON = (
                            f"100410 | {symbol} | {interval} | "
                            f"{error_msg}"
                        )

                    print(
                        f"[KLINES GLOBAL BLOCK] "
                        f"Час: {time.strftime('%H:%M:%S')} | "
                        f"Монета: {symbol} | "
                        f"Інтервал: {interval} | "
                        f"Тривалість: {remaining:.0f} сек. | "
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

async def check_volume_spike_alert(session, symbol):
    try:
        raw = await get_cached_klines(
            session,
            symbol,
            "15m",
            100
        )

        if not raw:
            return

        candles = prepare_candles(
            raw,
            15 * 60 * 1000
        )

        if len(candles) < 2:
            return

        previous = candles[-2]
        latest = candles[-1]

        pair = (
            previous["time"],
            latest["time"]
        )

        # Не надсилати повторно для тієї самої пари свічок
        if VOLUME_ALERT_LAST_PAIR.get(symbol) == pair:
            return

        VOLUME_ALERT_LAST_PAIR[symbol] = pair

        previous_volume = previous["volume"]
        latest_volume = latest["volume"]

        if previous_volume <= 0:
            return

        spike = latest_volume / previous_volume

        if spike >= VOLUME_SPIKE_MULTIPLIER:

            correlation_text = "н/д"

            try:
                raw_1h = await get_cached_klines(
                    session,
                    symbol,
                    "1h",
                    100
                )

                if raw_1h:
                    coin_1h = prepare_candles(
                        raw_1h,
                        60 * 60 * 1000
                    )

                    btc_1h = await get_btc_1h_candles(session)

                    if coin_1h and btc_1h:
                        btc_map = {
                            x["time"]: float(x["close"])
                            for x in btc_1h
                        }

                        coin_map = {
                            x["time"]: float(x["close"])
                            for x in coin_1h
                        }

                        # Останні 24 спільні закриті свічки
                        common = sorted(
                            set(btc_map) & set(coin_map)
                        )[-24:]

                        if len(common) >= 15:
                            corr = calculate_correlation(
                                [btc_map[t] for t in common],
                                [coin_map[t] for t in common]
                            )

                            correlation_text = f"{corr:.2f}"

            except Exception as e:
                print(
                    f"[VOLUME CORRELATION ERROR] {symbol}: {e}",
                    flush=True
                )

            await send_telegram(
                f"{symbol} — Сплеск: ×{spike:.2f} "
                f"({correlation_text})"
            )

    except Exception as e:
        print(
            f"[VOLUME ALERT ERROR] {symbol}: {e}",
            flush=True
    )

def calculate_trade_levels(direction, entry, stop, tp1, tp2, tp3):
    """Розрахунок позиції: депозит $20, базове плече 10x,
    бажане використання не більше $3 власних коштів на одну позицію
    та ризик до стопа не більше $0.60.
    Якщо на базовому плечі потрібно понад $3, пропонується потрібне плече.
    """
    try:
        entry = float(entry)
        stop = float(stop)
        tp1, tp2, tp3 = float(tp1), float(tp2), float(tp3)

        if direction not in ("LONG", "SHORT"):
            return None
        if not all(math.isfinite(x) and x > 0 for x in (entry, stop, tp1, tp2, tp3)):
            return None

        risk_per_coin = entry - stop if direction == "LONG" else stop - entry
        if risk_per_coin <= 0:
            return None

        # Розмір позиції визначається насамперед допустимим ризиком.
        qty = RISK_USD / risk_per_coin
        if not math.isfinite(qty) or qty <= 0:
            return None

        notional = qty * entry
        if not math.isfinite(notional) or notional <= 0:
            return None

        # На базовому плечі 10x перевіряємо, скільки власних коштів потрібно.
        base_margin = notional / LEVERAGE
        recommended_leverage = None
        effective_leverage = LEVERAGE

        if base_margin > POSITION_BUDGET_USD:
            # Мінімальне ціле плече, за якого вкладення у позицію <= $3.
            effective_leverage = max(
                LEVERAGE,
                int(math.ceil(notional / POSITION_BUDGET_USD))
            )
            recommended_leverage = effective_leverage

        margin = notional / effective_leverage
        actual_risk = qty * risk_per_coin

        # Не використовуємо більше $3 на позицію; загальний депозит — $20.
        if (
            margin > POSITION_BUDGET_USD + 1e-9
            or margin > DEPOSIT_USD + 1e-9
            or actual_risk > RISK_USD + 1e-9
        ):
            return None

        return {
            "pos_qty": qty,
            "notional_usd": notional,
            "margin_usd": margin,
            "actual_risk": actual_risk,
            "recommended_leverage": recommended_leverage,
            "effective_leverage": effective_leverage,
            "stop_loss": stop,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3
        }
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None

def detect_range_sweep(candles_1h, candles_15m, report=None, symbol=None):
    """RANGE SWEEP: 1H боковик за фрактальними межами, 15M свіп і повернення."""
    name = symbol or "Невідома монета"

    def log(category, reason):
        add_report_item(report, category, f"{name} — {reason}")

    ATR_PERIOD = 14
    EMA_PERIOD = 50
    EMA_SLOPE_CANDLES = 5
    MIN_EMA_SLOPE_ATR = 0.10
    FRACTAL_RADIUS = 2
    MIN_CANDLES_AFTER_RANGE = 10
    STOP_ATR_BUFFER = 0.30

    # prepare_candles повертає закриті свічки; останній елемент 1H
    # додатково не використовуємо, як і в початковій реалізації.
    hourly = sorted(candles_1h, key=lambda c: c.get("time", 0))[:-1]
    fifteen = sorted(candles_15m, key=lambda c: c.get("time", 0))

    if len(hourly) < 60:
        log("other_rejections", "RANGE SWEEP: недостатньо закритих свічок 1H")
        return None
    if len(fifteen) < 3:
        log("other_rejections", "RANGE SWEEP: недостатньо закритих свічок 15M")
        return None

    atr_1h = calculate_atr(hourly, ATR_PERIOD)
    atr_15m = calculate_atr(fifteen, ATR_PERIOD)
    if atr_1h <= 0 or atr_15m <= 0:
        log("other_rejections", "RANGE SWEEP: некоректний ATR")
        return None

    highs = [float(c["high"]) for c in hourly]
    lows = [float(c["low"]) for c in hourly]
    current_close = float(hourly[-1]["close"])

    fractal_highs = []
    fractal_lows = []
    for i in range(FRACTAL_RADIUS, len(hourly) - FRACTAL_RADIUS):
        left_highs = highs[i - FRACTAL_RADIUS:i]
        right_highs = highs[i + 1:i + FRACTAL_RADIUS + 1]
        left_lows = lows[i - FRACTAL_RADIUS:i]
        right_lows = lows[i + 1:i + FRACTAL_RADIUS + 1]

        if highs[i] > max(left_highs) and highs[i] > max(right_highs):
            fractal_highs.append({"index": i, "price": highs[i]})
        if lows[i] < min(left_lows) and lows[i] < min(right_lows):
            fractal_lows.append({"index": i, "price": lows[i]})

    # Визначаємо напрямок ПОПЕРЕДНЬОГО тренду за EMA50 на 1H.
    # Для кожної можливої першої межі оцінюємо нахил EMA за 5 закритих свічок
    # до цієї межі, нормалізований на ATR. Це не дає боковику самому
    # визначати напрямок тренду.
    closes = [float(c["close"]) for c in hourly]
    ema_values = []
    multiplier = 2.0 / (EMA_PERIOD + 1)
    ema = closes[0]
    for close in closes:
        ema = close * multiplier + ema * (1.0 - multiplier)
        ema_values.append(ema)

    def trend_before_pivot(index):
        if index < EMA_PERIOD + EMA_SLOPE_CANDLES:
            return None

        atr_at_pivot = calculate_atr(hourly[:index + 1], ATR_PERIOD)
        if atr_at_pivot <= 0:
            return None

        ema_slope_atr = (
            ema_values[index] - ema_values[index - EMA_SLOPE_CANDLES]
        ) / atr_at_pivot

        if ema_slope_atr >= MIN_EMA_SLOPE_ATR:
            return "UP"
        if ema_slope_atr <= -MIN_EMA_SLOPE_ATR:
            return "DOWN"
        return None

    candidates = []

    # Після висхідного тренду: спочатку максимум, потім мінімум.
    for high_pivot in reversed(fractal_highs):
        if trend_before_pivot(high_pivot["index"]) != "UP":
            continue

        following_lows = [
            p for p in fractal_lows
            if p["index"] > high_pivot["index"]
        ]
        if following_lows:
            low_pivot = min(following_lows, key=lambda p: p["price"])
            candidates.append({
                "high": high_pivot["price"],
                "low": low_pivot["price"],
                "end_index": low_pivot["index"],
                "trend": "UP"
            })

    # Після низхідного тренду: спочатку мінімум, потім максимум.
    for low_pivot in reversed(fractal_lows):
        if trend_before_pivot(low_pivot["index"]) != "DOWN":
            continue

        following_highs = [
            p for p in fractal_highs
            if p["index"] > low_pivot["index"]
        ]
        if following_highs:
            high_pivot = max(following_highs, key=lambda p: p["price"])
            candidates.append({
                "high": high_pivot["price"],
                "low": low_pivot["price"],
                "end_index": high_pivot["index"],
                "trend": "DOWN"
            })

    selected_range = None
    for candidate in sorted(candidates, key=lambda x: x["end_index"], reverse=True):
        range_high = candidate["high"]
        range_low = candidate["low"]
        range_size = range_high - range_low
        candles_after_range = len(hourly) - 1 - candidate["end_index"]

        if range_size <= 0:
            continue
        if candles_after_range < MIN_CANDLES_AFTER_RANGE:
            continue
        if not (range_low < current_close < range_high):
            continue

        selected_range = candidate
        break

    if selected_range is None:
        log("no_sweep", "RANGE SWEEP: не знайдено сформованого боковика з ціною всередині меж")
        return None

    range_high = selected_range["high"]
    range_low = selected_range["low"]
    range_size = range_high - range_low

    # Остання закрита свічка 15M робить свіп, попередня закрита — підтверджує повернення.
    sweep = fifteen[-2]
    confirm = fifteen[-1]
    sweep_high = float(sweep["high"])
    sweep_low = float(sweep["low"])
    sweep_close = float(sweep["close"])
    confirm_open = float(confirm["open"])
    confirm_close = float(confirm["close"])

    # LONG: свіп нижньої межі, повернення та бичаче підтвердження.
    long_sweep = sweep_low < range_low and sweep_close > range_low

    if long_sweep:
        print(
            f"[RANGE SWEEP DEBUG] LONG | "
            f"sweep_time={sweep.get('time')} | "
            f"confirm_time={confirm.get('time')} | "
            f"range_low={range_low} | range_high={range_high} | "
            f"sweep_low={sweep_low} | sweep_close={sweep_close} | "
            f"confirm_open={confirm_open} | confirm_close={confirm_close}",
            flush=True
        )

    long_confirm = (
        confirm_close > confirm_open
        and range_low < confirm_close < range_high
    )

    if long_sweep and long_confirm:
        entry = confirm_close
        stop = sweep_low - atr_15m * STOP_ATR_BUFFER
        if stop >= entry:
            log("invalid_stop", "RANGE SWEEP LONG: некоректний стоп")
            return None
        tp1 = entry + atr_15m * 2.5
        tp2 = tp1 + atr_15m * 2.0
        tp3 = tp2 + atr_15m * 2.0
        log("signals", f"RANGE SWEEP LONG: боковик {range_size / atr_1h:.2f} ATR")
        return {
            "strategy": "RANGE SWEEP", "direction": "LONG", "entry": entry,
            "stop": stop, "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "range_high": range_high, "range_low": range_low, "range_warnings": []
        }

    # SHORT: свіп верхньої межі, повернення та ведмеже підтвердження.
    short_sweep = sweep_high > range_high and sweep_close < range_high

    if short_sweep:
        print(
            f"[RANGE SWEEP DEBUG] SHORT | "
            f"sweep_time={sweep.get('time')} | "
            f"confirm_time={confirm.get('time')} | "
            f"range_low={range_low} | range_high={range_high} | "
            f"sweep_high={sweep_high} | sweep_close={sweep_close} | "
            f"confirm_open={confirm_open} | confirm_close={confirm_close}",
            flush=True
        )

    short_confirm = (
        confirm_close < confirm_open
        and range_low < confirm_close < range_high
    )

    if short_sweep and short_confirm:
        entry = confirm_close
        stop = sweep_high + atr_15m * STOP_ATR_BUFFER
        if stop <= entry:
            log("invalid_stop", "RANGE SWEEP SHORT: некоректний стоп")
            return None
        tp1 = entry - atr_15m * 2.5
        tp2 = tp1 - atr_15m * 2.0
        tp3 = tp2 - atr_15m * 2.0
        log("signals", f"RANGE SWEEP SHORT: боковик {range_size / atr_1h:.2f} ATR")
        return {
            "strategy": "RANGE SWEEP", "direction": "SHORT", "entry": entry,
            "stop": stop, "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "range_high": range_high, "range_low": range_low, "range_warnings": []
        }

    log("no_sweep", "RANGE SWEEP: боковик знайдено, але немає підтвердженого свіпу потрібної межі на 15M")
    return None
    
def detect_impulse_retest(candles_1h, candles_15m, report=None, symbol=None):
    """1H імпульс -> відкат/зупинка -> 16 свічок боковика 15M -> свіп і повернення."""
    name = symbol or "Невідома монета"

    def log(category, reason):
        add_report_item(report, category, f"{name} — {reason}")

    MIN_IMPULSE_ATR = 8.0
    ATR_PERIOD = 14
    MIN_CONSOLIDATION_CANDLES = 16
    MAX_IMPULSE_CANDLES = 8
    MAX_HOURLY_LOOKBACK = 36
    STOP_ATR_BUFFER = 0.30

    hourly = sorted(candles_1h, key=lambda c: c.get("time", 0))
    fifteen = sorted(candles_15m, key=lambda c: c.get("time", 0))

    if (len(hourly) < ATR_PERIOD + MAX_IMPULSE_CANDLES + 1
            or len(fifteen) < MIN_CONSOLIDATION_CANDLES + 1):
        log("other_rejections", "недостатньо свічок для IMPULSE RETEST")
        return None

    atr_15m = calculate_atr(fifteen, ATR_PERIOD)
    if atr_15m <= 0:
        log("other_rejections", "IMPULSE RETEST: некоректний ATR 15M")
        return None

    recent_hourly = hourly[-MAX_HOURLY_LOOKBACK:]
    impulses = []

    # Пошук імпульсів на 1H.
    for end_idx in range(len(recent_hourly) - 1, ATR_PERIOD, -1):
        for length in range(1, MAX_IMPULSE_CANDLES + 1):
            start_idx = end_idx - length + 1
            # calculate_atr потребує period + 1 свічок для 14 значень TR.
            if start_idx < ATR_PERIOD + 1:
                continue

            leg = recent_hourly[start_idx:end_idx + 1]
            prior = recent_hourly[start_idx - (ATR_PERIOD + 1):start_idx]
            atr_1h = calculate_atr(prior, ATR_PERIOD)
            if atr_1h <= 0:
                continue

            start_price = float(leg[0]["open"])
            end_high = max(float(c["high"]) for c in leg)
            end_low = min(float(c["low"]) for c in leg)
            end_close = float(leg[-1]["close"])
            move_up = end_high - start_price
            move_down = start_price - end_low

            if move_up >= MIN_IMPULSE_ATR * atr_1h and end_close > start_price:
                impulses.append({
                    "direction": "LONG", "start": start_price, "extreme": end_high,
                    "size": move_up, "atr": atr_1h, "time": leg[-1]["time"],
                    "end_idx": end_idx
                })
            if move_down >= MIN_IMPULSE_ATR * atr_1h and end_close < start_price:
                impulses.append({
                    "direction": "SHORT", "start": start_price, "extreme": end_low,
                    "size": move_down, "atr": atr_1h, "time": leg[-1]["time"],
                    "end_idx": end_idx
                })

    if not impulses:
        log("no_impulse_retest", "немає імпульсу 1H щонайменше 8 ATR")
        return None

    impulses.sort(key=lambda x: x["end_idx"], reverse=True)
    saw_post_impulse = False
    saw_consolidation = False
    waiting_for_confirmation = False

    for impulse in impulses:
        impulse_direction = impulse["direction"]
        post = [c for c in fifteen if c.get("time", 0) > impulse["time"]]
        if len(post) < MIN_CONSOLIDATION_CANDLES + 1:
            continue
        saw_post_impulse = True

        # 16 свічок формують межі боковика; наступна свічка перевіряється на свіп.
        for sweep_idx in range(len(post) - 1, len(post) - 2, -1):
            box = post[sweep_idx - MIN_CONSOLIDATION_CANDLES:sweep_idx]
            sweep = post[sweep_idx]
            if len(box) != MIN_CONSOLIDATION_CANDLES:
                continue

            box_high = max(float(c["high"]) for c in box)
            box_low = min(float(c["low"]) for c in box)
            if box_high <= box_low:
                continue
            saw_consolidation = True

            # Спочатку має завершитися відкат >=60%, потім окремо формується
            # 16-свічковий боковик і лише після нього шукаємо свіп.
            box_start_idx = sweep_idx - MIN_CONSOLIDATION_CANDLES
            pre_box = post[:box_start_idx]
            if not pre_box:
                continue

            if impulse_direction == "LONG":
                pullback_low = min(float(c["low"]) for c in pre_box)
                pullback_pct = (impulse["extreme"] - pullback_low) / impulse["size"]
                continuation = pullback_pct >= 0.60
                near_extreme = box_low >= impulse["start"] + impulse["size"] * 0.60

                if continuation:
                    # Продовження LONG: свіп нижньої межі й закриття назад у боковику.
                    if not (float(sweep["low"]) < box_low
                            and box_low < float(sweep["close"]) < box_high):
                        waiting_for_confirmation = True
                        continue
                    direction_signal = "LONG"
                    scenario = 1
                elif near_extreme:
                    # Поглинання імпульсу: свіп верхньої межі й повернення всередину.
                    if not (float(sweep["high"]) > box_high
                            and box_low < float(sweep["close"]) < box_high):
                        waiting_for_confirmation = True
                        continue
                    direction_signal = "SHORT"
                    scenario = 2
                else:
                    continue

            else:
                pullback_high = max(float(c["high"]) for c in pre_box)
                pullback_pct = (pullback_high - impulse["extreme"]) / impulse["size"]
                continuation = pullback_pct >= 0.60
                near_extreme = box_high <= impulse["start"] - impulse["size"] * 0.60

                if continuation:
                    # Продовження SHORT: свіп верхньої межі й закриття назад у боковику.
                    if not (float(sweep["high"]) > box_high
                            and box_low < float(sweep["close"]) < box_high):
                        waiting_for_confirmation = True
                        continue
                    direction_signal = "SHORT"
                    scenario = 1
                elif near_extreme:
                    # Поглинання імпульсу: свіп нижньої межі й повернення всередину.
                    if not (float(sweep["low"]) < box_low
                            and box_low < float(sweep["close"]) < box_high):
                        waiting_for_confirmation = True
                        continue
                    direction_signal = "LONG"
                    scenario = 2
                else:
                    continue

            # Вхід — закриття саме свічки свіпу після повернення в боковик.
            entry = float(sweep["close"])
            if direction_signal == "LONG":
                stop = float(sweep["low"]) - atr_15m * STOP_ATR_BUFFER
                risk = entry - stop
                if risk <= 0:
                    log("invalid_stop", "IMPULSE RETEST LONG: некоректний стоп за свіпом")
                    continue
                tp1 = entry + atr_15m * 2.5
                tp2 = tp1 + atr_15m * 2.0
                tp3 = tp2 + atr_15m * 2.0
            else:
                stop = float(sweep["high"]) + atr_15m * STOP_ATR_BUFFER
                risk = stop - entry
                if risk <= 0:
                    log("invalid_stop", "IMPULSE RETEST SHORT: некоректний стоп за свіпом")
                    continue
                tp1 = entry - atr_15m * 2.5
                tp2 = tp1 - atr_15m * 2.0
                tp3 = tp2 - atr_15m * 2.0

            log("signals", f"IMPULSE RETEST {direction_signal}, сценарій {scenario}")
            return {
                "strategy": "IMPULSE RETEST", "direction": direction_signal,
                "entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
                "tp3": tp3, "scenario": scenario
            }

    if not saw_post_impulse:
        log("waiting_confirmation", "після імпульсу ще немає 16 свічок боковика та свічки для свіпу")
    elif not saw_consolidation:
        log("waiting_confirmation", "ще не сформовано 16 свічок боковика 15M")
    elif waiting_for_confirmation:
        log("waiting_confirmation", "боковик є, очікується свіп потрібної межі та повернення")
    else:
        log("waiting_confirmation", "боковик не відповідає умовам продовження або поглинання імпульсу")

    return None


def detect_rounding(candles_1h, candles_15m, report=None, symbol=None):
    """
    ROUNDING — пошук заокруглення на 1H та входу на 15M.

    1H:
        10 свічок попереднього руху
        1 центральна свічка-екстремум
        2 закриті свічки підтвердження

    15M — два варіанти входу:
        1. Ранній відскок від локального екстремуму.
        2. Свіп рівня та повернення закриттям назад.

    SL:
        За найнижчу / найвищу точку всієї 13-свічкової структури 1H + 0.3 ATR(15M).

    TP:
        TP1 = 2.5 ATR
        TP2 = 4.5 ATR
        TP3 = 6.5 ATR

    Мінімальне співвідношення прибутку до ризику до TP1 = 1.3.
    """

    name = symbol or "Невідома монета"

    def log(category, reason):
        add_report_item(report, category, f"{name} — {reason}")

    # -------------------------------------------------
    # 1. ПЕРЕВІРКА КІЛЬКОСТІ СВІЧОК
    # -------------------------------------------------

    if len(candles_1h) < 13:
        log("other_rejections", "ROUNDING: недостатньо свічок 1H")
        return None

    if len(candles_15m) < 15:
        log("other_rejections", "ROUNDING: недостатньо свічок 15M для ATR")
        return None

    # Дані prepare_candles вже містять тільки закриті свічки.
    # Додатково сортуємо за часом.
    candles_1h = sorted(candles_1h, key=lambda x: x["time"])
    candles_15m = sorted(candles_15m, key=lambda x: x["time"])

    # -------------------------------------------------
    # 2. ПОШУК ЗАОКРУГЛЕННЯ НА 1H
    # -------------------------------------------------

    hourly = candles_1h[-13:]

    left = hourly[:10]
    center = hourly[10]
    right = hourly[11:]

    center_low = float(center["low"])
    center_high = float(center["high"])
    # Стоп для ROUNDING має бути за повним екстремумом усієї 13-свічкової структури 1H.
    structure_low = min(float(c["low"]) for c in hourly)
    structure_high = max(float(c["high"]) for c in hourly)

    left_lows = [float(x["low"]) for x in left]
    right_lows = [float(x["low"]) for x in right]

    left_highs = [float(x["high"]) for x in left]
    right_highs = [float(x["high"]) for x in right]

    # Попередній спад + центральне дно + початок відновлення.
    long_shape = (
        float(left[0]["close"]) > float(left[-1]["close"])
        and center_low < min(left_lows)
        and center_low < min(right_lows)
        and float(right[-1]["close"]) > float(right[0]["close"])
    )

    # Попереднє зростання + центральна вершина + початок зниження.
    short_shape = (
        float(left[0]["close"]) < float(left[-1]["close"])
        and center_high > max(left_highs)
        and center_high > max(right_highs)
        and float(right[-1]["close"]) < float(right[0]["close"])
    )

    if not long_shape and not short_shape:
        log("no_rounding", "немає підтвердженого заокруглення на 1H")
        return None

    # -------------------------------------------------
    # 3. ATR НА 15M
    # -------------------------------------------------

    atr = calculate_atr(candles_15m, 14)

    if atr <= 0:
        log("other_rejections", "ROUNDING: некоректний ATR на 15M")
        return None

    recent = candles_15m[-8:]

    last = recent[-1]
    prev = recent[-2]

    entry = float(last["close"])
    last_open = float(last["open"])
    last_high = float(last["high"])
    last_low = float(last["low"])

    # -------------------------------------------------
    # 4. СПІЛЬНИЙ РОЗРАХУНОК SL / TP / RR
    # -------------------------------------------------

    def build_setup(direction, stop_level, strategy, entry_type):

        if direction == "LONG":

            stop = stop_level - atr * 0.3

            if stop >= entry:
                log("invalid_stop", "ROUNDING LONG: стоп не нижче входу")
                return None

            risk = entry - stop

            tp1 = entry + atr * 2.5
            tp2 = tp1 + atr * 2.0
            tp3 = tp2 + atr * 2.0

            reward = tp1 - entry

        else:

            stop = stop_level + atr * 0.3

            if stop <= entry:
                log("invalid_stop", "ROUNDING SHORT: стоп не вище входу")
                return None

            risk = stop - entry

            tp1 = entry - atr * 2.5
            tp2 = tp1 - atr * 2.0
            tp3 = tp2 - atr * 2.0

            reward = entry - tp1

        if risk <= 0 or reward <= 0:
            log("invalid_stop", "ROUNDING: некоректний ризик або тейк")
            return None

        rr = reward / risk

        if rr < 1.3:
            log(
                "invalid_tp",
                f"ROUNDING {direction}: RR до TP1 = 1:{rr:.2f}, потрібно мінімум 1:1.3"
            )
            return None

        return {
            "strategy": strategy,
            "direction": direction,
            "entry_type": entry_type,
            "entry": entry,
            "stop": stop,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "rr": rr
        }

    # -------------------------------------------------
    # 5. LONG — ЗАОКРУГЛЕНЕ ДНО
    # -------------------------------------------------

    if long_shape:

        # ВАРІАНТ 1:
        # Ранній вхід від локального мінімуму.
        # Ціна перестала оновлювати мінімум і почала відскок.

        pivot_idx = None

        for i in range(len(recent) - 2, 0, -1):

            current_low = float(recent[i]["low"])
            previous_low = float(recent[i - 1]["low"])
            next_low = float(recent[i + 1]["low"])

            if current_low <= previous_low and current_low <= next_low:
                pivot_idx = i
                break

        if pivot_idx is not None:

            pivot_low = float(recent[pivot_idx]["low"])

            early_bounce = (
                last_low > pivot_low
                and float(last["close"]) > last_open
                and float(last["close"]) > float(prev["close"])
                and entry - pivot_low <= atr * 2.0
            )

            if early_bounce:

                setup = build_setup(
                    direction="LONG",
                    stop_level=structure_low,
                    strategy="ROUNDING BOTTOM EARLY",
                    entry_type="EARLY"
                )

                if setup is not None:
                    return setup

        # ВАРІАНТ 2:
        # Свіп нижньої межі та повернення закриттям у діапазон.

        base = recent[-7:-1]

        support = min(float(x["low"]) for x in base)

        sweep_recovery = (
            last_low < support
            and entry > support
            and entry > last_open
        )

        if sweep_recovery:

            setup = build_setup(
                direction="LONG",
                stop_level=structure_low,
                strategy="ROUNDING BOTTOM EARLY",
                entry_type="SWEEP"
            )

            if setup is not None:
                return setup

        log(
            "waiting_confirmation",
            "ROUNDING LONG: заокруглене дно на 1H є, але на 15M немає раннього відскоку або свіпу з поверненням"
        )

    # -------------------------------------------------
    # 6. SHORT — ЗАОКРУГЛЕНА ВЕРШИНА
    # -------------------------------------------------

    if short_shape:

        # ВАРІАНТ 1:
        # Ранній вхід від локального максимуму.
        # Ціна перестала оновлювати максимум і почала зниження.

        pivot_idx = None

        for i in range(len(recent) - 2, 0, -1):

            current_high = float(recent[i]["high"])
            previous_high = float(recent[i - 1]["high"])
            next_high = float(recent[i + 1]["high"])

            if current_high >= previous_high and current_high >= next_high:
                pivot_idx = i
                break

        if pivot_idx is not None:

            pivot_high = float(recent[pivot_idx]["high"])

            early_reversal = (
                last_high < pivot_high
                and float(last["close"]) < last_open
                and float(last["close"]) < float(prev["close"])
                and pivot_high - entry <= atr * 2.0
            )

            if early_reversal:

                setup = build_setup(
                    direction="SHORT",
                    stop_level=structure_high,
                    strategy="ROUNDING TOP EARLY",
                    entry_type="EARLY"
                )

                if setup is not None:
                    return setup

        # ВАРІАНТ 2:
        # Свіп верхньої межі та повернення закриттям у діапазон.

        base = recent[-7:-1]

        resistance = max(float(x["high"]) for x in base)

        sweep_recovery = (
            last_high > resistance
            and entry < resistance
            and entry < last_open
        )

        if sweep_recovery:

            setup = build_setup(
                direction="SHORT",
                stop_level=structure_high,
                strategy="ROUNDING TOP EARLY",
                entry_type="SWEEP"
            )

            if setup is not None:
                return setup

        log(
            "waiting_confirmation",
            "ROUNDING SHORT: заокруглена вершина на 1H є, але на 15M немає раннього зниження або свіпу з поверненням"
        )

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

            tp1 = entry + atr * 2.5
            tp2 = tp1 + atr * 2
            tp3 = tp2 + atr * 2

            return {
                "strategy": "DOUBLE BOTTOM — STRONG BREAKOUT",
                "direction": "LONG",
                "entry": entry,
                "stop": stop,
                "tp1": tp1,
                "tp2": tp2,
                "tp3": tp3
            }, "strong_breakout"

        else:
            if level - entry > max_extension:
                return None, "waiting"

            stop = max(pivot, second_extreme) + atr * 0.30
            risk = stop - entry

            if risk <= 0:
                return None, "invalid_stop"

            tp1 = entry - atr * 2.5
            tp2 = tp1 - atr * 2
            tp3 = tp2 - atr * 2

            return {
                "strategy": "DOUBLE TOP — STRONG BREAKOUT",
                "direction": "SHORT",
                "entry": entry,
                "stop": stop,
                "tp1": tp1,
                "tp2": tp2,
                "tp3": tp3
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

        tp1 = entry + atr * 2.5
        tp2 = tp1 + atr * 2
        tp3 = tp2 + atr * 2

        return {
            "strategy": "DOUBLE BOTTOM — RETEST",
            "direction": "LONG",
            "entry": entry,
            "stop": stop,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3
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

        tp1 = entry - atr * 2.5
        tp2 = tp1 - atr * 2
        tp3 = tp2 - atr * 2

        return {
            "strategy": "DOUBLE TOP — RETEST",
            "direction": "SHORT",
            "entry": entry,
            "stop": stop,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3
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


async def get_daily_sweep_status(session, symbol):
    """
    Перевіряє свіп за двома останніми закритими добовими свічками.
    Не впливає на торговий сигнал.
    """
    try:
        klines = await get_klines(session, symbol, "1d", 5)

        if not klines or len(klines) < 3:
            return "не вдалося перевірити"

        klines = sorted(
            klines,
            key=lambda x: int(
                x.get("time", x.get("openTime", 0))
            )
        )

        # Позавчорашня та остання повністю закрита доба
        c_prev = klines[-3]
        c_closed = klines[-2]

        high_prev = float(c_prev["high"])
        low_prev = float(c_prev["low"])

        high_closed = float(c_closed["high"])
        low_closed = float(c_closed["low"])
        close_closed = float(c_closed["close"])

        is_low_sweep = (
            low_closed < low_prev
            and close_closed > low_prev
        )

        is_high_sweep = (
            high_closed > high_prev
            and close_closed < high_prev
        )

        if is_low_sweep and is_high_sweep:
            return "зверху і знизу"

        if is_low_sweep:
            return "знизу"

        if is_high_sweep:
            return "зверху"

        return "ні"

    except Exception as e:
        print(
            f"[DAILY SWEEP ERROR] {symbol}: {e}",
            flush=True
        )
        return "не вдалося перевірити"


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
        # ЕТАП 1. ЗАВАНТАЖУЄМО 15M СВІЧКИ
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

        # Останні 5 закритих свічок 15M
        recent = candles_15m[-5:]

        if len(recent) < 5:
            log(
                "other_rejections",
                "недостатньо свічок для перевірки об'єму"
            )
            return None

        # Порівнюємо об'єми сусідніх свічок
        volumes = [c["volume"] for c in recent]

        volume_spike = False

        for i in range(4):
            current_volume = volumes[i]
            next_volume = volumes[i + 1]

            higher = max(current_volume, next_volume)
            lower = min(current_volume, next_volume)

            if higher > 0 and (
                lower == 0 or higher >= lower * 1.5
            ):
                volume_spike = True
                break

        if not volume_spike:
            log(
                "low_volume",
                "не виявлено сплеску об'єму серед 5 свічок 15M"
            )
            return None

        # =====================================================
        # ЕТАП 3. ЗАВАНТАЖУЄМО 1H СВІЧКИ
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
        # ЕТАП 6. ПЕРЕВІРКА СТОПА ТА TAKE PROFIT
        # =====================================================

        for setup in setups:

            direction = setup.get("direction")

            # Ціна входу береться безпосередньо із сигналу
            try:
                entry = float(setup["entry"])
            except (KeyError, TypeError, ValueError):
                log(
                    "invalid_entry",
                    f"{setup.get('strategy', 'UNKNOWN')}: "
                    "відсутня ціна входу в сигналі"
                )
                continue

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

                # ATR(14) на таймфреймі 15M
                atr_15m = calculate_atr(candles_15m, 14)

                if atr_15m <= 0:
                    log(
                        "invalid_tp",
                        f"{setup['strategy']}: некоректний ATR 15M"
                    )
                    continue

                # TP1 = 2.5 ATR
                # TP2 = TP1 + 2 ATR
                # TP3 = TP2 + 2 ATR

                if direction == "LONG":

                    tp1 = entry + atr_15m * 2.5
                    tp2 = tp1 + atr_15m * 2
                    tp3 = tp2 + atr_15m * 2

                else:

                    tp1 = entry - atr_15m * 2.5
                    tp2 = tp1 - atr_15m * 2
                    tp3 = tp2 - atr_15m * 2

                # Перевірка мінімального RR 1:1.3
                rr = abs(tp1 - entry) / risk

                if rr < 1.3:
                    log(
                        "invalid_tp",
                        f"{setup['strategy']}: RR нижче 1:1.3"
                    )
                    continue

                # Розрахунок розміру позиції та торгових рівнів
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
                        f"{setup['strategy']}: "
                        "не вдалося розрахувати торгові рівні"
                    )
                    continue

                # Додаткова інформація про добовий свіп
                daily_sweep = await get_daily_sweep_status(
                    session,
                    symbol
                )

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
                    "daily_sweep": daily_sweep,
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
        ("🔎 НЕМАЄ СВІПУ МЕЖ БОКОВИКА", "no_sweep"),
        ("🔎 НЕМАЄ ЗАОКРУГЛЕННЯ", "no_rounding"),
        ("🔎 НЕМАЄ ІМПУЛЬСУ — ВІДКАТУ — БОКОВИКА", "no_impulse_retest"),
        ("🔎 НЕМАЄ ПОДВІЙНОГО ДНА / ВЕРШИНИ", "no_double_pattern"),
        ("⏳ Є СТРУКТУРА, АЛЕ НЕМАЄ ПІДТВЕРДЖЕННЯ", "waiting_confirmation"),
        ("🛑 НЕКОРЕКТНИЙ СТОП-ЛОС", "invalid_stop"),
        ("🎯 НЕКОРЕКТНІ ТЕЙК-ПРОФІТИ", "invalid_tp"),
        ("⚠️ ІНШІ ВІДХИЛЕННЯ", "other_rejections"),
        ("❌ ПОМИЛКИ", "errors")
        ]

    # Переклад типових причин українською
    translations = {
        "діапазон 1h занадто широкий":
            "Діапазон 1H занадто широкий",

        "немає сильного імпульсу на 1h":
            "Немає сильного імпульсу на 1H",

        "немає підтвердженого подвійного дна або вершини на 1h":
            "Немає підтвердженого подвійного дна або вершини на 1H",

        "немає нижчого максимуму на 15m":
            "Немає нижчого максимуму на 15M",

        "немає локального дна на 15m":
            "Немає локального дна на 15M",

        "ціна вже відійшла від вершини більш ніж на 2 atr":
            "Ціна відійшла від вершини більш ніж на 2 ATR",

        "хибний пробій, ціна повернулася під рівень":
            "Хибний пробій — ціна повернулася під рівень",

        "немає зони j або перетину":
            "Немає зони J або необхідного перетину",

        "умови не збіглися (немає зони j або перетину)":
            "Не виконані умови входу: немає зони J або необхідного перетину",

        "об'єм нижче необхідного рівня":
            "Об'єм нижче необхідного рівня"
    }

    lines = ["📋 ЗВІТ СКАНУВАННЯ РИНКУ"]

    for title, key in sections:

        items = report.get(key, [])

        lines.append("")
        lines.append(f"{title} ({len(items)}):")

        if not items:
            lines.append("• Немає")
            continue

        # Сигнали залишаємо окремими записами
        if key == "signals":

            for item in items:
                lines.append(f"• {item}")

            continue

        # Групування монет за причинами
        grouped = {}

        for item in items:

            item = str(item).strip()

            if not item:
                continue

            if " — " in item:
                symbol, reason = item.split(" — ", 1)
                symbol = symbol.strip()
                reason = reason.strip()
            else:
                symbol = item
                reason = "Причину не вказано"

            # Переклад причини
            reason_lower = reason.lower()

            for original, translated in translations.items():
                if original in reason_lower:
                    reason = translated
                    break

            # Додатковий переклад англомовних назв стратегій
            display_replacements = (
                ("ROUNDING BOTTOM EARLY", "Заокруглене дно"),
                ("ROUNDING TOP EARLY", "Заокруглена вершина"),
                ("DOUBLE BOTTOM — STRONG BREAKOUT", "Подвійне дно — сильний пробій"),
                ("DOUBLE TOP — STRONG BREAKOUT", "Подвійна вершина — сильний пробій"),
                ("DOUBLE BOTTOM — RETEST", "Подвійне дно — ретест"),
                ("DOUBLE TOP — RETEST", "Подвійна вершина — ретест"),
                ("detect_range_sweep", "пошук свіпу межі боковика"),
                ("detect_impulse_retest", "пошук імпульсу, відкату та боковика"),
                ("detect_rounding", "пошук заокруглення"),
                ("detect_double_bottom_top", "пошук подвійного дна / вершини"),
                ("UNKNOWN", "невідома стратегія"),
                ("ROUNDING LONG:", "Заокруглення ЛОНГ:"),
                ("ROUNDING SHORT:", "Заокруглення ШОРТ:"),
                ("DOUBLE BOTTOM LONG:", "Подвійне дно (ЛОНГ):"),
                ("DOUBLE TOP SHORT:", "Подвійна вершина (ШОРТ):"),
                ("DOUBLE BOTTOM/TOP", "подвійного дна / вершини"),
                ("RANGE SWEEP", "свіп межі боковика"),
                ("IMPULSE RETEST", "імпульс — відкат — боковик"),
                ("ROUNDING", "заокруглення"),
                ("cooldown", "період очікування"),
                ("ATR", "середній істинний діапазон (ATR)"),
            )

            for original, translated in display_replacements:
                reason = reason.replace(original, translated)

            if reason not in grouped:
                grouped[reason] = []

            if symbol not in grouped[reason]:
                grouped[reason].append(symbol)

        # Спочатку виводимо причини та кількість
        for reason, symbols in grouped.items():

            lines.append("")
            lines.append(
                f"Причина: {reason} ({len(symbols)} шт.)"
            )

            # Кожна монета окремим рядком
            for symbol in symbols:
                lines.append(f"• {symbol}")

    return "\n".join(lines)
    
# Захист від одночасного запуску декількох сканувань
SCAN_LOCK = asyncio.Lock()

def get_strategy_name_uk(strategy):
    strategy_names = {
        "RANGE SWEEP": "Свіп межі боковика",
        "IMPULSE RETEST": "Імпульс — відкат — боковик",
        "ROUNDING BOTTOM EARLY": "Заокруглене дно",
        "ROUNDING TOP EARLY": "Заокруглена вершина",
        "DOUBLE BOTTOM — STRONG BREAKOUT": "Подвійне дно — сильний пробій",
        "DOUBLE TOP — STRONG BREAKOUT": "Подвійна вершина — сильний пробій",
        "DOUBLE BOTTOM — RETEST": "Подвійне дно — ретест",
        "DOUBLE TOP — RETEST": "Подвійна вершина — ретест"
    }

    return strategy_names.get(strategy, strategy)
    
# =====================================================
# ОСНОВНЕ СКАНУВАННЯ РИНКУ
# Добовий об'єм уже відфільтрований у main_bot_loop()
# =====================================================

async def scan_5min_loop(session, symbols):

    # Не запускаємо нове сканування, якщо попереднє ще працює.
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

        # Монети вже відфільтровані за добовим оборотом.
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

        # -------------------------------------------------
        # 1. СКАНУВАННЯ ПАКЕТАМИ
        # -------------------------------------------------

        batch_size = 10
        total_symbols = len(filtered_symbols)

        print(
            f"[INFO] До аналізу: {total_symbols} монет",
            flush=True
        )

        for i in range(0, total_symbols, batch_size):

            batch = filtered_symbols[i:i + batch_size]
            # Окрема перевірка сплеску об'єму 15M
            await asyncio.gather(
                *[
                    check_volume_spike_alert(session, sym)
                    for sym in batch
                ],
                return_exceptions=True
            )

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

                    # Помилка аналізу конкретної монети.
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

                    # Немає сигналу.
                    if not isinstance(setup, dict):
                        continue

                    # -------------------------------------------------
                    # 2. РЕЄСТРАЦІЯ СИГНАЛУ
                    # -------------------------------------------------

                    add_report_item(
                        report,
                        "signals",
                        f"{setup['symbol']} — "
                        f"{setup['direction']} — "
                        f"{get_strategy_name_uk(setup['strategy'])}"
                    )

                    # -------------------------------------------------
                    # 3. ПОПЕРЕДЖЕННЯ ПРО ЯКІСТЬ БОКОВИКА
                    # -------------------------------------------------

                    range_warning_text = ""

                    if setup.get("strategy") == "RANGE SWEEP":

                        warnings = setup.get("range_warnings", [])

                        if warnings:

                            range_warning_text = (
                                "⚠️ ОСОБЛИВОСТІ БОКОВИКА:\n"
                                + "\n".join(
                                    f"• {warning}"
                                    for warning in warnings
                                )
                                + "\n\n"
                            )

                    # -------------------------------------------------
                    # 4. РЕКОМЕНДОВАНЕ ПЛЕЧЕ
                    # -------------------------------------------------

                    leverage_note = ""

                    if setup.get("recommended_leverage"):

                        leverage_note = (
                            f"⚙️ Рекомендоване плече: "
                            f"`{setup['recommended_leverage']}x`\n"
                        )

                    # -------------------------------------------------
                    # 5. ФОРМУВАННЯ ПОВІДОМЛЕННЯ
                    # -------------------------------------------------

                    msg = (
                        f"🎯 **НОВИЙ СИГНАЛ**\n"
                        f"🪙 Монета: `{setup['symbol']}`\n"
                        f"📊 Стратегія: "
                        f"`{get_strategy_name_uk(setup['strategy'])}`\n"
                        f"📈 Напрямок: `{setup['direction']}`\n\n"

                        f"{range_warning_text}"

                        f"🔍 Свіп 1D: "
                        f"`{setup.get('daily_sweep', 'не вдалося перевірити')}`\n\n"

                        f"🟢 Вхід: `{setup['entry']:.8f}`\n"
                        f"🔴 Стоп: `{setup['stop_loss']:.8f}`\n\n"

                        f"🎯 TP1: `{setup['tp1']:.8f}` — закрити 50%\n"
                        f"🎯 TP2: `{setup['tp2']:.8f}` — закрити 25%\n"
                        f"🎯 TP3: `{setup['tp3']:.8f}` — закрити 25%\n\n"

                        f"📦 Кількість: `{setup['pos_qty']:.6f}`\n"
                        f"💵 Обсяг позиції: "
                        f"`{setup['notional_usd']:.2f} USD`\n"
                        f"💰 Власні кошти на позицію: `{setup['margin_usd']:.2f} USD`\n"

                        f"{leverage_note}"

                        f"🛡️ Ризик на стоп: "
                        f"`${setup['actual_risk']:.2f}`\n\n"

                        f"⏳ Очікування ручного відкриття."
                    )

                    # Відправляємо повідомлення про сигнал.
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

            # Пауза між пакетами.
            await asyncio.sleep(1)

        # -------------------------------------------------
        # 6. ЗАГАЛЬНИЙ ЗВІТ СКАНУВАННЯ
        # -------------------------------------------------

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
        "• Активні стратегії: RANGE SWEEP, IMPULSE RETEST, ROUNDING, DOUBLE BOTTOM/TOP.\n"
        "• Об'єм 15M: сплеск від 1.5x між сусідніми з 5 останніх закритих свічок.\n"
        "• Фільтр обороту 24h: не менше $400k. Сканування кожні 5 хв."
    )

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

                    if not isinstance(res, dict):
                        print(
                            f"[TICKER ERROR] "
                            f"Неочікуваний формат: {type(res).__name__} | "
                            f"Відповідь: {str(res)[:500]}",
                            flush=True
                        )
                        cycle_ok = False

                    else:
                        api_code = str(res.get("code", "відсутній"))
                        api_msg = res.get("msg", "відсутнє")
                        tickers_data = res.get("data", [])

                        if isinstance(tickers_data, dict):
                            tickers_data = [tickers_data]

                        if (
                            api_code != "0"
                            or not isinstance(tickers_data, list)
                            or not tickers_data
                        ):
                            print(
                                f"[TICKER ERROR] "
                                f"API code: {api_code} | "
                                f"Повідомлення: {api_msg} | "
                                f"Тип data: {type(res.get('data')).__name__} | "
                                f"Відповідь: {str(res)[:500]}",
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
                
