import os
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
        if not candles or len(candles) < n: 
            return [50.0], [50.0], [50.0]
        
        closes = [float(x['close']) for x in candles]
        lows = [float(x['low']) for x in candles]
        highs = [float(x['high']) for x in candles]
        
        rsv_list = []
        for i in range(len(candles)):
            if i < n - 1:
                continue
            sub_lows = lows[i - n + 1 : i + 1]
            sub_highs = highs[i - n + 1 : i + 1]
            l_val = min(sub_lows)
            h_val = max(sub_highs)
            c_val = closes[i]
            
            if h_val - l_val == 0:
                rsv = 50.0
            else:
                rsv = (c_val - l_val) / (h_val - l_val) * 100.0
            rsv_list.append(rsv)
            
        if not rsv_list:
            return [50.0], [50.0], [50.0]

        k_list, d_list, j_list = [], [], []
        k = rsv_list[0]
        d = rsv_list[0]
        
        for rsv in rsv_list:
            k = (2.0 / 3.0) * k + (1.0 / 3.0) * rsv
            d = (2.0 / 3.0) * d + (1.0 / 3.0) * k
            j = 3.0 * k - 2.0 * d
            k_list.append(k)
            d_list.append(d)
            j_list.append(j)
            
        return k_list, d_list, j_list
    except Exception:
        return [50.0], [50.0], [50.0]
        

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
    
    
# --- 2. КОЖНІ 5 ХВ: ОБ'ЄМ НА 15m (>= 2.0x) + ПЕРЕТИН KDJ НА ЗАКРИТИХ ГОДИННИХ СВІЧКАХ (1h) ---
async def analyze_signal(session, symbol):
    try:
        async def fetch_data():
            raw_1h = await get_klines(session, symbol, '1h', 200)
            raw_15m = await get_klines(session, symbol, '15m', 60)
            return raw_1h, raw_15m
        
        raw_1h, raw_15m = await asyncio.wait_for(fetch_data(), timeout=6.0)
        
        if len(raw_1h) < 30 or len(raw_15m) < 40:
            return None

        candles_1h_closed = [{'high': float(c['high']), 'low': float(c['low']), 'close': float(c['close']), 'volume': float(c['volume'])} for c in raw_1h[:-1]]
        candles_1h_closed = [{'time': k['time'], 'open': float(k['open']), 'high': float(k['high']), 'low': float(k['low']), 'close': float(k['close']), 'volume': float(k['volume'])} for k in raw_1h[:-1]]
        candles_15m_closed = [{'time': k['time'], 'open': float(k['open']), 'high': float(k['high']), 'low': float(k['low']), 'close': float(k['close']), 'volume': float(k['volume'])} for k in raw_15m[:-1]]
    
        # --- 1. СПЛЕСК ОБ'ЄМУ НА 15м (>= 2.0x) ---
        if len(candles_15m_closed) < 35:
            return None, False, "Недостатньо даних"

        avg_vol_15m = sum(float(c['volume']) for c in candles_15m_closed[-25:-5]) / 20
        if avg_vol_15m == 0:
            return None, False, "Нульовий об'єм"

        has_vol_spike = any(float(c['volume']) >= avg_vol_15m * 2.0 for c in candles_15m_closed[-1:])
        if not has_vol_spike:
            return None, False, "Немає сплеску об'єму на 15м (>= 2.0x)"

        # --- 2. РОЗРАХУНОК KDJ ДЛЯ ОБОХ ТАЙМФРЕЙМІВ ---
        k_15m, d_15m, j_15m = calculate_kd(candles_15m_closed)
        k_1h, d_1h, j_1h = calculate_kd(candles_1h_closed)

        if len(k_15m) < 35 or len(k_1h) < 10:
            return None, True, "Недостатньо даних для KDJ"

        # --- 3. ПОТОЧНИЙ (ДРУГИЙ) ПЕРЕТИН НА 15м (останні свічки [-2] та [-1]) ---
        m15_cross2_up = (k_15m[-2] <= d_15m[-2]) and (k_15m[-1] > d_15m[-1])
        m15_cross2_down = (k_15m[-2] >= d_15m[-2]) and (k_15m[-1] < d_15m[-1])

        # --- 4. ПОПЕРЕДНІЙ ПЕРЕТИН НА 15м ТА ПОШУК ЕКСТРЕМУМУ J НА ГЛИБИНІ 30 СВІЧОК ---
        first_cross_found = False
        extreme_j_found = False

        # Перевіряємо, чи був екстремум J у зонах перепроданості/перекупленості на всій глибині пошуку (30 свічок)
        # Наприклад, шукаємо J < 10 (для лонгу) або J > 90 (для шорту) в діапазоні
        if m15_cross2_up:
            if any(val < 10 for val in j_15m[-30:]):
                extreme_j_found = True
        elif m15_cross2_down:
            if any(val > 90 for val in j_15m[-30:]):
                extreme_j_found = True
        else:
            return None, True, "Немає поточного перетину на 15м"

        # Шукаємо попередній перетин у межах 30 свічок
        for i in range(-30, -3):
            is_up = (k_15m[i-1] <= d_15m[i-1]) and (k_15m[i] > d_15m[i])
            is_down = (k_15m[i-1] >= d_15m[i-1]) and (k_15m[i] < d_15m[i])

            if (m15_cross2_up and is_up) or (m15_cross2_down and is_down):
                first_cross_found = True
                break

        if not first_cross_found or not extreme_j_found:
            return None, True, "Немає першого перетину або екстремуму J в історії"
            
        # --- 5. ТРЕНД НА ГОДИННИКУ (1h) ---
        h_trend_long = False
        h_trend_short = False

        for h in range(-1, -13, -1):
            # Шукаємо перетин у ширшому діапазоні (12 свічок) 
            # АБО перевіряємо загальний стан, коли K впевнено вище/нижче D
            is_cross_up = (k_1h[h-1] <= d_1h[h-1]) and (k_1h[h] > d_1h[h])
            is_cross_down = (k_1h[h-1] >= d_1h[h-1]) and (k_1h[h] < d_1h[h])
            
            # Додатково враховуємо поточний стан тренду на годиннику
            is_state_up = (k_1h[-1] > d_1h[-1]) and (k_1h[-2] >= d_1h[-2])
            is_state_down = (k_1h[-1] < d_1h[-1]) and (k_1h[-2] <= d_1h[-2])

            if is_cross_up or is_state_up:
                h_trend_long = True
                break
            if is_cross_down or is_state_down:
                h_trend_short = True
                break
                
        # --- 6. ФІНАЛЬНИЙ СИГНАЛ (СИНХРОНІЗАЦІЯ 15м ТА 1h) ---
        is_long_signal = m15_cross2_up and h_trend_long
        is_short_signal = m15_cross2_down and h_trend_short

        if not is_long_signal and not is_short_signal:
            return None, True, "Немає збігу напрямку з годинником (1h)"
            

        atr_th = calculate_atr(candles_1h_closed, period=14)
        if atr_th == 0:
            return None

        ticker_resp = await session.get(f"{BINGX_BASE_URL}/openApi/swap/v2/quote/ticker?symbol={symbol}")
        ticker_data = await ticker_resp.json()
        if ticker_data.get('code') == 0 and 'data' in ticker_data:
            current_price = float(ticker_data['data']['lastPrice'])
        else:
            current_price = float(candles_1h_closed[-1]['close'])

        if is_long_signal:
            signal_name = "LONG (1h Trend + 15m Vol & KDJ Entry)"
            stop_loss = round(current_price - (atr_th * 2.0), 8)
            tp1 = round(current_price + (atr_th * 2.5), 8)
            tp2 = round(tp1 + (atr_th * 2.0), 8)
            tp3 = round(tp2 + (atr_th * 2.0), 8)

            risk_per_coin = current_price - stop_loss
            if risk_per_coin > 0:
                pos_qty = RISK_USD / risk_per_coin
                notional_usd = pos_qty * current_price
                margin_usd = notional_usd / LEVERAGE
                return {
                    "symbol": symbol, "signal": signal_name,
                    "entry": current_price, "stop_loss": stop_loss,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "pos_qty": pos_qty, "notional_usd": notional_usd, "margin_usd": margin_usd
                }

        elif is_short_signal:
            signal_name = "SHORT (1h Trend + 15m Vol & KDJ Entry)"
            stop_loss = round(current_price + (atr_th * 2.0), 8)
            tp1 = round(current_price - (atr_th * 2.5), 8)
            tp2 = round(tp1 - (atr_th * 2.0), 8)
            tp3 = round(tp2 - (atr_th * 2.0), 8)

            risk_per_coin = stop_loss - current_price
            if risk_per_coin > 0:
                pos_qty = RISK_USD / risk_per_coin
                notional_usd = pos_qty * current_price
                margin_usd = notional_usd / LEVERAGE
                return {
                    "symbol": symbol, "signal": signal_name,
                    "entry": current_price, "stop_loss": stop_loss,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "pos_qty": pos_qty, "notional_usd": notional_usd, "margin_usd": margin_usd
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
                
