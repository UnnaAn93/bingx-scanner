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

async def get_usdt_symbols(session):
    data = await bingx_get(session, "/openApi/swap/v2/quote/contracts")
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("Не вдалося отримати список контрактів BingX")
    symbols = set()
    for item in data:
        if isinstance(item, dict):
            symbol = str(item.get("symbol", "")).strip().upper()
            if symbol.endswith("-USDT"):
                symbols.add(symbol)
    return sorted(symbols)

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

        if not symbol.endswith("-USDT") or raw_volume is None:
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
    Рівні беруться зі свічок від 8 до 24 барів до неї.
    """
    if len(candles) < SWING_MAX_BARS_AGO + 1:
        return "Найближчого хай/лоу: немає"

    signal = candles[-1]
    # Історичні бари -24 ... -8 включно; сигнальна свічка не входить.
    reference = candles[-SWING_MAX_BARS_AGO:-(SWING_MIN_BARS_AGO - 1)]
    if not reference:
        return "Найближчого хай/лоу: немає"

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

async def analyze_symbol(session, symbol, btc_candles):
    try:
        candles = await get_closed_1h_candles(session, symbol, limit=40)
        if len(candles) < SWING_MAX_BARS_AGO + 1:
            return None

        ratio = volume_ratio(candles)
        if ratio is None or ratio < VOLUME_SPIKE_MIN:
            return None

        sweep_text = detect_sweep(candles)
        correlation = calculate_btc_correlation(candles, btc_candles)
        corr_text = f"{correlation:.2f}" if correlation is not None else "н/д"

        return (
            "Сплеск об'єму\n\n"
            f"Монета: {symbol}\n\n"
            f"Обсяг: ×{ratio:.1f}\n\n"
            f"{sweep_text}\n\n"
            f"Кореляція: {corr_text}"
        )
    except Exception as exc:
        logger.exception("[ANALYZE ERROR] %s", symbol)
        return {"__error__": f"{symbol}: {exc}"}

async def scan_market(session):
    logger.info("[SCAN START] %s", datetime.now(KYIV_TZ).isoformat())
    try:
        symbols = await get_usdt_symbols(session)
    except Exception as exc:
        logger.exception("[SYMBOLS ERROR]")
        await notify_error(session, "Не вдалося отримати список BingX-пар", exc)
        return

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
        if quote_volumes.get(symbol, 0) >= MIN_24H_QUOTE_VOLUME_USDT
    ]

    logger.info(
        "[24H VOLUME FILTER] Мінімум: 300000 USDT | До фільтра: %s | Після фільтра: %s",
        before_filter, len(symbols)
    )

try:
    btc_candles = await get_closed_1h_candles(session, "BTC-USDT", 40)
    
    except Exception as exc:
        logger.exception("[BTC ERROR]")
        await notify_error(session, "Не вдалося отримати свічки BTC", exc)
        btc_candles = []

    symbols_to_scan = [s for s in symbols if s != "BTC-USDT"]
    results = await asyncio.gather(
        *(analyze_symbol(session, s, btc_candles) for s in symbols_to_scan),
        return_exceptions=True
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

def seconds_until_next_scan():
    now = datetime.now(KYIV_TZ)
    next_hour = now.replace(minute=1, second=0, microsecond=0)
    if now >= next_hour:
        next_hour += timedelta(hours=1)
    return max(1.0, (next_hour - now).total_seconds())

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
                delay = seconds_until_next_scan()
                next_time = datetime.now(KYIV_TZ) + timedelta(seconds=delay)
                logger.info("[WAIT] Наступне сканування: %s", next_time.strftime("%Y-%m-%d %H:%M:%S %Z"))
                await asyncio.sleep(delay)
                try:
                    await scan_market(session)
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
