import os
import time
import math
import asyncio
import threading
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, HTTPServer

import aiohttp

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
BINGX_BASE_URL = "https://open-api.bingx.com"

TIMEFRAME = "1h"
INTERVAL_MS = 60 * 60 * 1000
VOLUME_SPIKE_MIN = 1.5
MIN_24H_QUOTE_VOLUME_USDT = 300000
SWING_MIN_BARS_AGO = 8
SWING_MAX_BARS_AGO = 24
CORRELATION_PERIOD = 24
CANDLE_CLOSE_GRACE_SECONDS = 5
MAX_CONCURRENT_REQUESTS = 5
REQUEST_TIMEOUT_SECONDS = 15
PORT = int(os.getenv("PORT", "10000"))
KYIV_TZ = ZoneInfo("Europe/Kyiv")
SELF_PING_INTERVAL_SECONDS = 240
EMA20_SCAN_MINUTE = 5
EMA20_VOL_LOOKBACK = 20
EMA20_MIN_REL_VOL = 0.7
EMA20_MAX_REL_VOL = 1.5
EMA20_MAX_SPIKE_MULT = 2.0
EMA20_MIN_BELOW_PCT = 0.1
EMA20_MAX_BELOW_PCT = 2.5
EMA20_MAX_RECENT_PUMP_PCT = 5.0
MAX_BTC_CORRELATION = 0.7
EMA50_PERIOD = 50
EMA50_CORRELATION_MIN = -0.50
EMA50_CORRELATION_MAX = 0.50
EXCLUDED_PREFIXES = ("NCF", "NCS", "NCC")
BLACKLIST = set()

def is_excluded_symbol(symbol: str) -> bool:
    sym = symbol.upper().strip()
    return sym in BLACKLIST or sym.startswith(EXCLUDED_PREFIXES)
    
RENDER_URL = os.getenv("RENDER_URL", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("volume_scanner")

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Volume scanner is running")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return

def run_health_server():
    HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()

async def send_telegram(session, message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Не задано TELEGRAM_BOT_TOKEN або TELEGRAM_CHAT_ID")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with session.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            body = await response.text()
            if response.status != 200:
                logger.error("Telegram HTTP %s: %s", response.status, body)
                return False
            logger.info("[TELEGRAM SENT] %s", message.replace("\\n", " | "))
            return True
    except Exception:
        logger.exception("Помилка відправлення повідомлення в Telegram")
        return False

async def notify_error(session, title, details):
    # Telegram-повідомлення про помилку; повний traceback залишається в логах Render.
    short_details = str(details).strip()
    if len(short_details) > 2500:
        short_details = short_details[:2500] + "..."
    await send_telegram(session, f"ПОМИЛКА БОТА: {title}\\n\\n{short_details}")

async def self_ping_loop(session):
    if not RENDER_URL:
        logger.warning("RENDER_URL не задано — самопінг вимкнений")
        return
    while True:
        await asyncio.sleep(SELF_PING_INTERVAL_SECONDS)
        try:
            async with session.get(RENDER_URL, timeout=10) as response:
                if response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status}")
                logger.info("[SELF-PING] Успішно, HTTP %s", response.status)
        except Exception as exc:
            logger.exception("[SELF-PING ERROR]")
            await notify_error(session, "Самопінг не вдався", exc)

request_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

async def bingx_get(session, endpoint, params=None):
    url = f"{BINGX_BASE_URL}{endpoint}"
    async with request_semaphore:
        async with session.get(
            url, params=params or {}, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            response.raise_for_status()
            result = await response.json()
    if not isinstance(result, dict):
        raise ValueError("BingX повернув неочікуваний формат відповіді")
    if str(result.get("code", "0")) != "0":
        raise RuntimeError(
            f"BingX API error {result.get('code')}: {result.get('msg')}"
        )
    return result.get("data")

def _disp_name(item, symbol):
    keys = ("displayName", "name", "alias")
    for k in keys:
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().upper()
    return symbol

async def get_usdt_symbols(session):
    data = await bingx_get(
        session, "/openApi/swap/v2/quote/contracts"
    )
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError(
            "Не вдалося отримати список контрактів BingX"
        )
    out = {}
    for item in data:
        if isinstance(item, dict):
            sym = str(
                item.get("symbol", "")
            ).strip().upper()
            if sym.endswith("-USDT") and not is_excluded_symbol(sym):
                out[sym] = _disp_name(item, sym)
    return out

async def get_24h_quote_volumes(session):
    data = await bingx_get(
        session, "/openApi/swap/v2/quote/ticker"
    )

    if isinstance(data, dict):
        data = [data]

    if not isinstance(data, list):
        raise ValueError("Не вдалося отримати 24h ticker BingX")

    volumes = {}

    for item in data:
        if not isinstance(item, dict):
            continue

        symbol = str(item.get("symbol", "")).strip().upper()
        raw_volume = item.get("quoteVolume")

        if not symbol.endswith("-USDT") or is_excluded_symbol(symbol) or raw_volume is None:
            continue

        try:
            volume = float(raw_volume)
        except (TypeError, ValueError):
            continue

        volumes[symbol] = volume

    return volumes
    
def parse_candle(raw):
    """Підтримка об'єктного та масивного формату klines."""
    try:
        if isinstance(raw, dict):
            timestamp = (raw.get("time") or raw.get("timestamp")
                         or raw.get("openTime") or raw.get("t"))
            op = raw.get("open", raw.get("o"))
            high = raw.get("high", raw.get("h"))
            low = raw.get("low", raw.get("l"))
            close = raw.get("close", raw.get("c"))
            volume = (raw.get("volume") or raw.get("vol")
                      or raw.get("Volume") or raw.get("v"))
        elif isinstance(raw, (list, tuple)) and len(raw) >= 6:
            timestamp, op, close, high, low, volume = raw[:6]
        else:
            return None

        timestamp = int(float(timestamp))
        if timestamp < 10_000_000_000:
            timestamp *= 1000

        candle = {
            "time": timestamp,
            "open": float(op),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": float(volume),
        }
        if not all(math.isfinite(candle[k]) for k in
                   ("open", "high", "low", "close", "volume")):
            return None
        if candle["volume"] < 0:
            return None
        return candle
    except (TypeError, ValueError, OverflowError):
        return None

async def get_closed_1h_candles(session, symbol, limit=40):
    data = await bingx_get(
        session,
        "/openApi/swap/v2/quote/klines",
        {"symbol": symbol, "interval": TIMEFRAME, "limit": limit}
    )
    if not isinstance(data, list):
        return []

    now_ms = int(time.time() * 1000)
    unique = {}
    for raw in data:
        candle = parse_candle(raw)
        if candle and candle["time"] + INTERVAL_MS <= now_ms:
            unique[candle["time"]] = candle
    return [unique[t] for t in sorted(unique)]

def volume_ratio(candles):
    if len(candles) < 2:
        return None
    previous = candles[-2]["volume"]
    current = candles[-1]["volume"]
    if previous <= 0:
        return None
    return current / previous

def pearson_correlation(a, b):
    if len(a) != len(b) or len(a) < 10:
        return None
    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    numerator = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    sum_a = sum((x - mean_a) ** 2 for x in a)
    sum_b = sum((y - mean_b) ** 2 for y in b)
    denominator = math.sqrt(sum_a * sum_b)
    return numerator / denominator if denominator else None

def calculate_btc_correlation(coin_candles, btc_candles):
    coin = {c["time"]: c["close"] for c in coin_candles}
    btc = {c["time"]: c["close"] for c in btc_candles}
    times = sorted(set(coin) & set(btc))
    if len(times) < CORRELATION_PERIOD + 1:
        return None
    times = times[-(CORRELATION_PERIOD + 1):]
    coin_returns, btc_returns = [], []
    for old, new in zip(times, times[1:]):
        cp, cn = coin[old], coin[new]
        bp, bn = btc[old], btc[new]
        if min(cp, cn, bp, bn) <= 0:
            continue
        coin_returns.append((cn / cp - 1) * 100)
        btc_returns.append((bn / bp - 1) * 100)
    return pearson_correlation(coin_returns, btc_returns)


def detect_sweep(candles):
    """
    Перевіряється тільки остання закрита свічка.
    Рівні беруться з 24 попередніх закритих свічок,
    без сигнальної свічки.
    """
    if len(candles) < 25:
        return "Найближчого хай/лоу: немає"

    signal = candles[-1]
    reference = candles[-25:-1]

    lowest = min(c["low"] for c in reference)
    highest = max(c["high"] for c in reference)

    swept_low = signal["low"] < lowest and signal["close"] > lowest
    swept_high = signal["high"] > highest and signal["close"] < highest

    if swept_low and swept_high:
        return "Свіп: верхнього хай та нижнього лоу"
    if swept_low:
        return "Свіп: нижнього лоу"
    if swept_high:
        return "Свіп: верхнього хай"

    return "Найближчого хай/лоу: немає"


def calc_ema(values, period=20):
    if not values:
        return []
    k = 2 / (period + 1)
    ema = [values[0]]
    for price in values[1:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema


def check_ema20_approach(candles):
    need = max(30, EMA20_VOL_LOOKBACK + 5)
    if len(candles) < need:
        return None

    closes = [c["close"] for c in candles]
    lows = [c["low"] for c in candles]
    vols = [c["volume"] for c in candles]

    ema20 = calc_ema(closes, 20)
    last_close = closes[-1]
    last_ema20 = ema20[-1]
    if last_ema20 <= 0 or last_close >= last_ema20:
        return None

    # 1. Ціна ще під EMA20 і підходить знизу
    below_pct = ((last_ema20 - last_close) / last_ema20) * 100
    if not (EMA20_MIN_BELOW_PCT <= below_pct <= EMA20_MAX_BELOW_PCT):
        return None

    prev_low_min = min(lows[-5:-3])
    recent_low_min = min(lows[-3:])
    if recent_low_min < prev_low_min * 0.992:
        return None

    # 2. Рівний відносний об'єм без різких спайків
    avg_vol = sum(vols[-(EMA20_VOL_LOOKBACK + 1):-1]) / EMA20_VOL_LOOKBACK
    if avg_vol <= 0:
        return None

    rel_vol = vols[-1] / avg_vol
    max_recent_rel_vol = max(vols[-4:]) / avg_vol
    if not (EMA20_MIN_REL_VOL <= rel_vol <= EMA20_MAX_REL_VOL):
        return None
    if max_recent_rel_vol > EMA20_MAX_SPIKE_MULT:
        return None

    # 3. Немає перегрітого стрибка за останні 4 закриті свічки
    base_4 = closes[-5]
    jump_4_pct = ((last_close - base_4) / base_4) * 100 if base_4 > 0 else 0
    if jump_4_pct > EMA20_MAX_RECENT_PUMP_PCT:
        return None

    return {
        "price": last_close,
        "ema20": last_ema20,
        "below_pct": below_pct,
        "rel_vol": rel_vol,
        "jump_4c_pct": jump_4_pct,
    }
    

async def analyze_symbol(
    session, symbol, disp_name, btc_candles
):
    try:
        candles = await get_closed_1h_candles(session, symbol, limit=40)
        if len(candles) < SWING_MAX_BARS_AGO + 1:
            return None

        ratio = volume_ratio(candles)
        if ratio is None or ratio < VOLUME_SPIKE_MIN:
            return None
            
        sweep_text = detect_sweep(candles)
        if sweep_text == "Свіп: верхнього хай":
            sweep_text = "🟢 " + sweep_text
        elif sweep_text == "Свіп: нижнього лоу":
            sweep_text = "🔴 " + sweep_text
        elif sweep_text == "Свіп: верхнього хай та нижнього лоу":
            sweep_text = "🟢🔴 " + sweep_text
        else:
            sweep_text = "⚪ " + sweep_text
        correlation = calculate_btc_correlation(candles, btc_candles)
        if correlation is not None and correlation > MAX_BTC_CORRELATION:
            return None
        corr_text = f"{correlation:.2f}" if correlation is not None else "н/д"

        s_norm = symbol.replace("-", "").upper()
        d_norm = disp_name.replace("-", "").upper()
        if d_norm and d_norm != s_norm:
            label = f"{disp_name} ({symbol})"
        else:
            label = symbol

        return (
            "Сплеск об'єму\n\n"
            f"Монета: {label}\n\n"
            f"Обсяг: ×{ratio:.1f}\n\n"
            f"{sweep_text}\n\n"
            f"Кореляція: {corr_text}"
        )

    except Exception as exc:
        logger.exception("[ANALYZE ERROR] %s", symbol)
        return {"__error__": f"{symbol}: {exc}"}

async def analyze_ema20_symbol(session, symbol, disp_name, btc_candles):
    try:
        candles = await get_closed_1h_candles(session, symbol, limit=60)
        setup = check_ema20_approach(candles)
        if not setup:
            return None

        sweep_text = detect_sweep(candles)
        correlation = calculate_btc_correlation(candles, btc_candles)
        if correlation is not None and correlation > MAX_BTC_CORRELATION:
            return None
        corr_text = f"{correlation:.2f}" if correlation is not None else "н/д"

        s_norm = symbol.replace("-", "").upper()
        d_norm = disp_name.replace("-", "").upper() if disp_name else s_norm
        label = f"{disp_name} ({symbol})" if d_norm and d_norm != s_norm else symbol

        return (
            "Підтискання під EMA20 (1h)\n"
            f"Монета: {label}\n"
            f"Ціна: {setup['price']} | EMA20: {setup['ema20']:.6f}\n"
            f"Нижче EMA20: {setup['below_pct']:.2f}% | RelVol: {setup['rel_vol']:.2f}\n"
            f"Зміна за 4 свічки: {setup['jump_4c_pct']:+.2f}%\n"
            f"{sweep_text}\n"
            f"Кореляція: {corr_text}"
        )
    except Exception as exc:
        logger.exception("[EMA20 ANALYZE ERROR] %s", symbol)
        return {"__error__": f"{symbol}: {exc}"}

async def analyze_ema50_symbol(session, symbol, disp_name, btc_candles):
    try:
        candles = await get_closed_1h_candles(session, symbol, limit=100)

        if len(candles) < EMA50_PERIOD + 1:
            return None

        closes = [c["close"] for c in candles]
        ema50 = calc_ema(closes, EMA50_PERIOD)

        # Дві останні ЗАКРИТІ свічки
        prev_close = closes[-2]
        last_close = closes[-1]

        prev_ema50 = ema50[-2]
        last_ema50 = ema50[-1]

        signal = None

        # LONG: ціна перетнула EMA50 знизу вгору
        if prev_close < prev_ema50 and last_close > last_ema50:
            signal = "long"

        # SHORT: ціна перетнула EMA50 згори вниз
        elif prev_close > prev_ema50 and last_close < last_ema50:
            signal = "short"

        if signal is None:
            return None

        # Перевірка кореляції з BTC
        correlation = calculate_btc_correlation(candles, btc_candles)

        if correlation is None:
            return None

        if not (EMA50_CORRELATION_MIN <= correlation <= EMA50_CORRELATION_MAX):
            return None

        s_norm = symbol.replace("-", "").upper()
        d_norm = disp_name.replace("-", "").upper() if disp_name else s_norm
        label = (
            f"{disp_name} ({symbol})"
            if d_norm and d_norm != s_norm
            else symbol
        )

        if signal == "long":
            direction = "🟢 ПРОБІЙ EMA50 ВГОРУ"
        else:
            direction = "🔴 ПРОБІЙ EMA50 ВНИЗ"

        return (
            f"{direction} (1H)\n"
            f"Монета: {label}\n"
            f"Ціна закриття: {last_close}\n"
            f"EMA50: {last_ema50:.8f}\n"
            f"Кореляція з BTC: {correlation:.2f}"
        )

    except Exception as exc:
        logger.exception("[EMA50 ANALYZE ERROR] %s", symbol)
        return {"__error__": f"{symbol}: {exc}"}

async def scan_ema50_breakouts(session):
    logger.info("[EMA50 SCAN START]")

    try:
        sym_map = await get_usdt_symbols(session)
        symbols = [
            s for s in sorted(sym_map.keys())
            if s != "BTC-USDT" and not is_excluded_symbol(s)
        ]

        btc_candles = await get_closed_1h_candles(
            session, "BTC-USDT", limit=100
        )

        if len(btc_candles) < CORRELATION_PERIOD + 1:
            logger.error("[EMA50] Недостатньо свічок BTC для кореляції")
            return

    except Exception as exc:
        logger.exception("[EMA50 SCAN INIT ERROR]")
        await notify_error(session, "Помилка запуску сканера EMA50", exc)
        return

    results = await asyncio.gather(
        *(
            analyze_ema50_symbol(
                session,
                symbol,
                sym_map.get(symbol, symbol),
                btc_candles,
            )
            for symbol in symbols
        ),
        return_exceptions=True,
    )

    sent = 0
    errors = 0

    for result in results:
        if isinstance(result, Exception):
            errors += 1
            logger.error("[EMA50 TASK ERROR] %s", result)

        elif isinstance(result, dict) and "__error__" in result:
            errors += 1
            logger.error("[EMA50 SYMBOL ERROR] %s", result["__error__"])

        elif result:
            await send_telegram(session, result)
            sent += 1

    logger.info(
        "[EMA50 SCAN FINISH] Перевірено: %s | Повідомлень: %s | Помилок: %s",
        len(symbols),
        sent,
        errors,
    )
        
async def scan_market(session, mode="spike"):
    logger.info("[SCAN START] mode=%s | %s", mode, datetime.now(KYIV_TZ).isoformat())
    try:
        sym_map = await get_usdt_symbols(session)
    except Exception as exc:
        logger.exception("[SYMBOLS ERROR]")
        await notify_error(
            session,
            "Не вдалося отримати список BingX-пар",
            exc,
        )
        return

    symbols = sorted(sym_map.keys())
    logger.info("[SYMBOLS] Усього USDT-пар: %s", len(symbols))

    try:
        quote_volumes = await get_24h_quote_volumes(session)
    except Exception as exc:
        logger.exception("[24H VOLUME ERROR]")
        await notify_error(session, "Не вдалося отримати 24h обсяги BingX", exc)
        return

    before_filter = len(symbols)
    symbols = [
        symbol for symbol in symbols
        if not is_excluded_symbol(symbol) and quote_volumes.get(symbol, 0) >= MIN_24H_QUOTE_VOLUME_USDT
    ]

    logger.info(
        "[24H VOLUME FILTER] Мінімум %s USDT | До фільтра: %s | Після фільтра: %s",
        MIN_24H_QUOTE_VOLUME_USDT,
        before_filter,
        len(symbols),
    )

    try:
        btc_candles = await get_closed_1h_candles(session, "BTC-USDT", limit=60)
    except Exception as exc:
        logger.exception("[BTC ERROR]")
        await notify_error(session, "Не вдалося отримати свічки BTC", exc)
        btc_candles = []

    symbols_to_scan = [s for s in symbols if s != "BTC-USDT"]
    worker = analyze_ema20_symbol if mode == "ema20" else analyze_symbol
    results = await asyncio.gather(
        *(
            worker(session, s, sym_map.get(s, s), btc_candles)
            for s in symbols_to_scan
        ),
        return_exceptions=True,
    )

    sent, errors = 0, 0
    for result in results:
        if isinstance(result, Exception):
            errors += 1
            logger.error("[TASK ERROR] %s", result, exc_info=(type(result), result, result.__traceback__))
        elif isinstance(result, dict) and "__error__" in result:
            errors += 1
            logger.error("[SYMBOL ERROR] %s", result["__error__"])
        elif result:
            await send_telegram(session, result)
            sent += 1

    logger.info("[SCAN FINISH] Перевірено: %s | Повідомлень: %s | Помилок: %s", len(symbols_to_scan), sent, errors)
    if errors:
        await notify_error(session, "Помилки під час сканування", f"Кількість помилок: {errors}. Деталі дивись у логах Render.")
        
def seconds_until_next_slot(slots=(1, EMA20_SCAN_MINUTE)):
    now = datetime.now(KYIV_TZ)
    candidates = []
    for m in slots:
        dt = now.replace(minute=m, second=0, microsecond=0)
        if dt <= now:
            dt += timedelta(hours=1)
        candidates.append((dt, m))
    next_dt, slot_minute = min(candidates, key=lambda x: x[0])
    return max(1.0, (next_dt - now).total_seconds()), slot_minute

async def main():
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_REQUESTS)
    async with aiohttp.ClientSession(
        timeout=timeout, connector=connector
    ) as session:
        logger.info("[BOT START] Новий сканер сплесків об'єму")
        logger.info("[CONFIG] Тільки пари з -USDT")
        logger.info("[CONFIG] Сплеск об'єму від ×%s", VOLUME_SPIKE_MIN)
        logger.info("[CONFIG] Таймфрейм 1H; лише закриті свічки")
        logger.info("[CONFIG] Свіп перевіряється лише на останній закритій свічці")
        logger.info("[CONFIG] Екстремуми: 8–24 свічки тому")
        logger.info("[CONFIG] Самопінг кожні %s секунд", SELF_PING_INTERVAL_SECONDS)
        logger.info("[CONFIG] Сканування щогодини о HH:01 за Europe/Kyiv")

        await send_telegram(
            session,
            "Бот BingX запущено\\nСамопінг: кожні 4 хвилини\\nСканування: щогодини о HH:01 (Київ)"
        )
        ping_task = asyncio.create_task(self_ping_loop(session), name="self_ping")

        try:
            while True:
                delay, slot_minute = seconds_until_next_slot((1, EMA20_SCAN_MINUTE))
                next_time = datetime.now(KYIV_TZ) + timedelta(seconds=delay)
                mode = "ema20" if slot_minute == EMA20_SCAN_MINUTE else "spike"
                logger.info(
                    "[WAIT] Наступне сканування (%s, :%02d): %s",
                    mode,
                    slot_minute,
                    next_time.strftime("%Y-%m-%d %H:%M:%S %Z"),
                )
                await asyncio.sleep(delay)
                try:
                    await scan_market(session, mode=mode)
                except Exception as exc:
                    logger.exception("[SCAN FATAL ERROR]")
                    await notify_error(session, "Критична помилка циклу сканування", exc)

        finally:
            ping_task.cancel()
            await asyncio.gather(ping_task, return_exceptions=True)

if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("[BOT STOP] Зупинено вручну")
    except Exception as exc:
        logger.exception("[BOT CRASH]")
        # Якщо основний цикл упав, створюємо окрему коротку сесію для аварійного повідомлення.
        async def report_crash():
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                await notify_error(session, "Бот аварійно завершив роботу", exc)
        try:
            asyncio.run(report_crash())
        except Exception:
            logger.exception("[CRASH NOTIFICATION ERROR]")
        raise
