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


def detect_range_sweep(candles_1h, candles_15m):
    """
    Пошук боковика на 1H.
    LONG: прокол нижньої межі та повернення всередину.
    SHORT: прокол верхньої межі та повернення всередину.
    """

    if len(candles_1h) < RANGE_LOOKBACK_1H:
        return None

    if len(candles_15m) < ENTRY_LOOKBACK_15M:
        return None

    # Діапазон визначаємо за попередніми годинними свічками.
    # Останню годинну свічку не включаємо.
    range_candles = candles_1h[-RANGE_LOOKBACK_1H-1:-1]

    range_high = max(c["high"] for c in range_candles)
    range_low = min(c["low"] for c in range_candles)

    if range_high <= range_low:
        return None

    range_size = range_high - range_low

    # Не беремо надто широкий діапазон
    avg_price = sum(c["close"] for c in range_candles) / len(range_candles)

    if range_size / avg_price > 0.08:
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
        return None

    if high_touches < MIN_RANGE_TOUCHES:
        return None

    recent = candles_15m[-ENTRY_LOOKBACK_15M:]

    # LONG: свіп нижньої межі
    for i in range(len(recent) - 1, max(-1, len(recent) - 9), -1):
        c = recent[i]

        if c["low"] < range_low and c["close"] > range_low:
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

    return None


def detect_impulse_retest(candles_1h, candles_15m):
    """
    Пошук сильного імпульсу та повторних тестів зони.
    """

    if len(candles_1h) < 20 or len(candles_15m) < 20:
        return None

    hourly = candles_1h[-20:]
    recent = candles_15m[-20:]

    avg_range = sum(
        c["high"] - c["low"] for c in hourly[:-5]
    ) / max(1, len(hourly[:-5]))

    if avg_range <= 0:
        return None

    # Шукаємо імпульс на 1H серед останніх п'яти свічок
    for i in range(len(hourly) - 1, len(hourly) - 6, -1):
        c = hourly[i]
        body = abs(c["close"] - c["open"])
        candle_range = c["high"] - c["low"]

        if candle_range <= 0:
            continue

        # LONG імпульс
        if (
            c["close"] > c["open"]
            and body >= avg_range * 1.5
            and body / candle_range >= 0.60
        ):
            zone_low = c["open"]
            zone_high = c["open"] + body * 0.35

            tests = [
                x for x in recent
                if x["low"] <= zone_high
                and x["high"] >= zone_low
            ]

            if len(tests) >= MIN_RETESTS:
                last = recent[-1]

                if last["close"] > zone_high:
                    stop = min(x["low"] for x in tests)

                    return {
                        "strategy": "IMPULSE RETEST",
                        "direction": "LONG",
                        "entry": last["close"],
                        "stop": stop - avg_range * 0.10,
                        "tp1": last["close"] + avg_range,
                        "tp2": last["close"] + avg_range * 2,
                        "tp3": last["close"] + avg_range * 3
                    }

        # SHORT імпульс
        if (
            c["close"] < c["open"]
            and body >= avg_range * 1.5
            and body / candle_range >= 0.60
        ):
            zone_high = c["open"]
            zone_low = c["open"] - body * 0.35

            tests = [
                x for x in recent
                if x["high"] >= zone_low
                and x["low"] <= zone_high
            ]

            if len(tests) >= MIN_RETESTS:
                last = recent[-1]

                if last["close"] < zone_low:
                    stop = max(x["high"] for x in tests)

                    return {
                        "strategy": "IMPULSE RETEST",
                        "direction": "SHORT",
                        "entry": last["close"],
                        "stop": stop + avg_range * 0.10,
                        "tp1": last["close"] - avg_range,
                        "tp2": last["close"] - avg_range * 2,
                        "tp3": last["close"] - avg_range * 3
                    }

    return None


def detect_rounding(candles_1h, candles_15m):
    """
    Спрощений пошук заокруглення на 1H.
    LONG: формування вищих мінімумів і пробій локального максимуму.
    SHORT: нижчі максимуми і пробій локального мінімуму.
    """

    if len(candles_1h) < 30 or len(candles_15m) < 3:
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

    last = candles_15m[-1]

    # Заокруглене дно
    if (
        middle_low < left_low
        and middle_low < right_low
        and right_low > middle_low
    ):
        neckline = max(x["high"] for x in c[10:25])

        if last["close"] > neckline:
            stop = min(x["low"] for x in c[10:])

            return {
                "strategy": "ROUNDING BOTTOM",
                "direction": "LONG",
                "entry": last["close"],
                "stop": stop,
                "tp1": last["close"] + (last["close"] - stop),
                "tp2": last["close"] + 2 * (last["close"] - stop),
                "tp3": last["close"] + 3 * (last["close"] - stop)
            }

    # Заокруглена вершина
    if (
        middle_high > left_high
        and middle_high > right_high
        and right_high < middle_high
    ):
        neckline = min(x["low"] for x in c[10:25])

        if last["close"] < neckline:
            stop = max(x["high"] for x in c[10:])

            return {
                "strategy": "ROUNDING TOP",
                "direction": "SHORT",
                "entry": last["close"],
                "stop": stop,
                "tp1": last["close"] - (stop - last["close"]),
                "tp2": last["close"] - 2 * (stop - last["close"]),
                "tp3": last["close"] - 3 * (stop - last["close"])
            }

    return None


async def analyze_signal(session, symbol):
    try:
        now = time.time()

        if (
            symbol in signal_cooldowns
            and now - signal_cooldowns[symbol] < COOLDOWN_PERIOD
        ):
            return None

        if symbol == "BTC-USDT":
            return None

        # Завантажуємо свічки один раз для всіх трьох стратегій
        raw_1h, raw_15m = await asyncio.gather(
            get_klines(session, symbol, "1h", 100),
            get_klines(session, symbol, "15m", 100)
        )

        if not raw_1h or not raw_15m:
            return None

        candles_1h = prepare_candles(raw_1h, 60 * 60 * 1000)
        candles_15m = prepare_candles(raw_15m, 15 * 60 * 1000)

        if len(candles_1h) < 30 or len(candles_15m) < 48:
            return None

        # Фільтр об'єму: останні 3 свічки проти попередніх 20
        historical = candles_15m[-23:-3]
        recent = candles_15m[-3:]

        avg_volume = sum(x["volume"] for x in historical) / len(historical)
        recent_volume = sum(x["volume"] for x in recent)

        if avg_volume <= 0:
            return None

        if recent_volume < avg_volume * 3 * VOLUME_MULTIPLIER:
            return None

        # Перевірка кореляції з BTC
        btc_1h = await get_btc_1h_candles(session)

        if len(btc_1h) >= 24:
            btc_map = {
                candle_timestamp_ms(x): float(x["close"])
                for x in btc_1h[-24:]
            }
            coin_map = {
                x["time"]: x["close"]
                for x in candles_1h[-24:]
            }

            common = sorted(set(btc_map) & set(coin_map))

            if len(common) >= 15:
                corr = calculate_correlation(
                    [btc_map[t] for t in common],
                    [coin_map[t] for t in common]
                )

                if corr >= 0.60:
                    return None

        # Перевіряємо всі три стратегії
        setups = [
            detect_range_sweep(candles_1h, candles_15m),
            detect_impulse_retest(candles_1h, candles_15m),
            detect_rounding(candles_1h, candles_15m)
        ]

        # Беремо перший знайдений варіант
        setup = next((x for x in setups if x is not None), None)

        if setup is None:
            return None

        direction = setup["direction"]

        # Перевіряємо актуальну ціну
        ticker_url = (
            f"{BINGX_BASE_URL}/openApi/swap/v2/quote/ticker"
        )

        async with session.get(
            ticker_url,
            params={"symbol": symbol},
            timeout=5
        ) as resp:
            if resp.status != 200:
                return None

            ticker_data = await resp.json()

        if ticker_data.get("code") != 0:
            return None

        ticker = ticker_data.get("data")

        if isinstance(ticker, list):
            ticker = ticker[0] if ticker else None

        if not ticker or not ticker.get("lastPrice"):
            return None

        entry = float(ticker["lastPrice"])

        # Стоп має бути з правильного боку від входу
        stop = setup["stop"]

        if direction == "LONG" and stop >= entry:
            return None

        if direction == "SHORT" and stop <= entry:
            return None

        # Цілі мають бути в напрямку прибутку
        tp1 = setup["tp1"]
        tp2 = setup["tp2"]
        tp3 = setup["tp3"]

        if direction == "LONG":
            if not (entry < tp1 < tp2 < tp3):
                return None
        else:
            if not (entry > tp1 > tp2 > tp3):
                return None

        levels = calculate_trade_levels(
            direction, entry, stop, tp1, tp2, tp3
        )

        if levels is None:
            return None

        signal_cooldowns[symbol] = time.time()

        print(
            f"[SIGNAL] {symbol} {direction} "
            f"{setup['strategy']}",
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

    except Exception as e:
        print(f"[ERROR in {symbol}] {e}", flush=True)
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
                
