import os
import asyncio
import aiohttp
import threading
from datetime import datetime, timezone, timedelta
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
        async with session.get(url, params=params, timeout=4) as resp:
            if resp.status == 200:
                data = await resp.json()
                klines = data.get("data", [])
                if not klines and isinstance(data, list):
                    klines = data
                return klines
    except:
        pass
    return []

async def get_current_price(session, symbol):
    url = f"{BINGX_BASE_URL}/openApi/swap/v1/ticker/price"
    params = {"symbol": symbol}
    try:
        async with session.get(url, params=params, timeout=3) as resp:
            if resp.status == 200:
                data = await resp.json()
                price_str = data.get("data", {}).get("price")
                if price_str:
                    return float(price_str)
    except:
        pass
    return None

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

def calculate_kd(candles, n=9):
    try:
        if not candles or len(candles) < n: 
            return [], [], []
        
        closes = [float(x['close']) for x in candles]
        lows = [float(x['low']) for x in candles]
        highs = [float(x['high']) for x in candles]
        
        k_list, d_list, j_list = [], [], []
        k = 50.0
        d = 50.0
        
        for i in range(len(candles)):
            if i < n - 1:
                k_list.append(k)
                d_list.append(d)
                j_list.append(3.0 * k - 2.0 * d)
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
                
            k = (2.0 / 3.0) * k + (1.0 / 3.0) * rsv
            d = (2.0 / 3.0) * d + (1.0 / 3.0) * k
            j = 3.0 * k - 2.0 * d
            
            k_list.append(k)
            d_list.append(d)
            j_list.append(j)
            
        return k_list, d_list, j_list
    except Exception:
        return [], [], []

# --- 1. ЩОДЕННИЙ ЗВІТ ПО ЗАКРИТИХ СВІПАХ (1D) ---
async def job_daily_sweep_report(session, symbols):
    print("[INFO] Формування звіту по закритих денних свіпах (1D)...", flush=True)
    bullish_sweeps = []
    bearish_sweeps = []

    batch_size = 25
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        tasks = [get_klines(session, sym, '1d', 10) for sym in batch]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for j, klines_1d in enumerate(results):
                if isinstance(klines_1d, Exception) or not klines_1d or len(klines_1d) < 3:
                    continue
                symbol = batch[j]
                
                c_prev = klines_1d[-3]  
                c_closed = klines_1d[-2] 
                
                try:
                    high_prev, low_prev = float(c_prev['high']), float(c_prev['low'])
                    high_closed, low_closed = float(c_closed['high']), float(c_closed['low'])
                    close_closed = float(c_closed['close'])

                    if high_closed > high_prev and close_closed < high_prev:
                        bearish_sweeps.append(f"`{symbol}` (High Sweep)")
                    elif low_closed < low_prev and close_closed > low_prev:
                        bullish_sweeps.append(f"`{symbol}` (Low Sweep)")
                except:
                    continue
        except:
            pass
        await asyncio.sleep(0.05)

    report = "🔔 *Щоденний звіт по закритих свіпах (1D)*\n\n"
    if bullish_sweeps:
        report += "🟢 *Бичачі свіпи (Лонг):*\n" + "\n".join(bullish_sweeps[:15]) + "\n\n"
    if bearish_sweeps:
        report += "🔴 *Ведмежі свіпи (Шорт):*\n" + "\n".join(bearish_sweeps[:15]) + "\n\n"
    if not bullish_sweeps and not bearish_sweeps:
        report += "ℹ️ За минулу добу чітких свіпів за заданими умовами не виявлено."

    await send_telegram(report)
    print("[INFO] Звіт по свіпах успішно надіслано!", flush=True)

# --- 2. КОЖНІ 5 ХВ: ОБ'ЄМ (15m) + ПЕРЕТИН KDJ НА 2 ЗАКРИТИХ ГОДИННИХ СВІЧКАХ (1h) ---
async def analyze_signal(session, symbol):
    try:
        async def fetch_data():
            raw_1h = await get_klines(session, symbol, '1h', 300)
            raw_15m = await get_klines(session, symbol, '15m', 50)
            cur_p = await get_current_price(session, symbol)
            return raw_1h, raw_15m, cur_p

        raw_1h, raw_15m, current_price = await asyncio.wait_for(fetch_data(), timeout=5.0)

        if len(raw_1h) < 50 or len(raw_15m) < 25 or current_price is None:
            return None

        # 1. ОБ'ЄМ НА 15m (перевірка сплеску за останні 5 свічок, виключаючи поточну живу)
        candles_15m = [{'volume': float(c['volume'])} for c in raw_15m[:-1]]
        avg_vol_15m = sum(c['volume'] for c in candles_15m[-25:-5]) / 20
        if avg_vol_15m == 0:
            return None
        
        has_volume_spike_15m = any(c['volume'] >= avg_vol_15m * 2.0 for c in candles_15m[-5:])
        if not has_volume_spike_15m:
            return None

        # 2. ГОДИННИЙ KDJ: суворо відкидаємо останню живу свічку (raw_1h[-1])
        candles_1h_closed = [{'high': float(c['high']), 'low': float(c['low']), 'close': float(c['close'])} for c in raw_1h[:-1]]
        
        k_vals, d_vals, _ = calculate_kd(candles_1h_closed)
        if len(k_vals) < 3:
            return None

        curr_k, curr_d = k_vals[-1], d_vals[-1]
        prev_k, prev_d = k_vals[-2], d_vals[-2]

        atr_1h = calculate_atr(candles_1h_closed, period=14)
        if atr_1h == 0:
            return None

        kdj_long_crossover = (prev_k <= prev_d and curr_k > curr_d)
        kdj_short_crossover = (prev_k >= prev_d and curr_k < curr_d)

        if kdj_long_crossover or kdj_short_crossover:
            signal_type = "LONG" if kdj_long_crossover else "SHORT"
            print(f"[SIGNAL CHECK] {symbol} | {signal_type} | 1h Closed K[-2]={prev_k:.2f}, D[-2]={prev_d:.2f} -> K[-1]={curr_k:.2f}, D[-1]={curr_d:.2f}", flush=True)

        if kdj_long_crossover:
            stop_loss = round(current_price - (atr_1h * 2.0), 6)
            tp1 = round(current_price + (atr_1h * 3.0), 6)
            tp2 = round(tp1 + (atr_1h * 2.0), 6)
            tp3 = round(tp2 + (atr_1h * 2.0), 6)

            risk_per_coin = current_price - stop_loss
            if risk_per_coin > 0:
                pos_qty = RISK_USD / risk_per_coin
                notional_usd = pos_qty * current_price
                margin_usd = notional_usd / LEVERAGE
                return {
                    "symbol": symbol, "signal": "LONG (1h KDJ Crossover + 15m Vol)",
                    "entry": current_price, "stop_loss": stop_loss,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "pos_qty": pos_qty, "notional_usd": notional_usd, "margin_usd": margin_usd
                }

        elif kdj_short_crossover:
            stop_loss = round(current_price + (atr_1h * 2.0), 6)
            tp1 = round(current_price - (atr_1h * 3.0), 6)
            tp2 = round(tp1 - (atr_1h * 2.0), 6)
            tp3 = round(tp2 - (atr_1h * 2.0), 6)

            risk_per_coin = stop_loss - current_price
            if risk_per_coin > 0:
                pos_qty = RISK_USD / risk_per_coin
                notional_usd = pos_qty * current_price
                margin_usd = notional_usd / LEVERAGE
                return {
                    "symbol": symbol, "signal": "SHORT (1h KDJ Crossover + 15m Vol)",
                    "entry": current_price, "stop_loss": stop_loss,
                    "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    "pos_qty": pos_qty, "notional_usd": notional_usd, "margin_usd": margin_usd
                }
    except:
        pass
    return None

async def scan_5min_loop(session, symbols):
    print("[INFO] Початок сканування ринку (1h KDJ Crossover + 15m Об'єм)...", flush=True)
    batch_size = 25
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
                        f"🟢 Вхід (орієнтир): `{setup['entry']:.6f}`\n"
                        f"🔴 Стоп-лос: `{setup['stop_loss']:.6f}`\n"
                        f"🎯 TP1: `{setup['tp1']:.6f}` | TP2: `{setup['tp2']:.6f}` | TP3: `{setup['tp3']:.6f}`\n\n"
                        f"🧮 **Розрахунок позиції (Депозит $20):**\n"
                        f"📦 Кількість: `{setup['pos_qty']:.4f}`\n"
                        f"💵 Обсяг позиції: `{setup['notional_usd']:.2f} USD`\n"
                        f"🏦 Маржа (10x): `{setup['margin_usd']:.2f} USD`\n"
                        f"🛡️ Ризик на стоп: `$0.60` (3%)\n\n"
                        f"⏳ Очікування ручного відкриття..."
                    )
                    await send_telegram(msg)
        except:
            pass
        await asyncio.sleep(0.1)
    print("[INFO] Сканування ринку завершено.", flush=True)

# --- ГОЛОВНИЙ ДЕМОН ---
async def main_bot_loop():
    await send_telegram("🚀 Торговий бот успішно запущено!\n• Щоденний звіт по закритих свіпах активний\n• Перевірка KDJ (на 2 закритих свічках 1h) + Об'єм кожні 5 хв активна.")
    
    last_daily_report_date = None

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{BINGX_BASE_URL}/openApi/swap/v2/quote/contracts", timeout=10) as resp:
                    if resp.status != 200:
                        await asyncio.sleep(60)
                        continue
                    res = await resp.json()

                contracts_data = res if isinstance(res, list) else res.get("data", [])
                
                exclude_patterns = ("NCF", "NCS", "NCC", "-USDC")
                symbols = [
                    item["symbol"] for item in contracts_data 
                    if isinstance(item, dict) and "symbol" in item 
                    and not any(p in item["symbol"] for p in exclude_patterns)
                ]

                now_utc = datetime.now(timezone.utc)
                today_str = now_utc.strftime("%Y-%m-%d")

                if last_daily_report_date != today_str and now_utc.hour >= 0 and now_utc.minute >= 5:
                    await job_daily_sweep_report(session, symbols)
                    last_daily_report_date = today_str

                await scan_5min_loop(session, symbols)

        except Exception as e:
            print(f"[ERROR] Помилка в основному циклі: {e}", flush=True)

        await asyncio.sleep(300)

async def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    await main_bot_loop()

if __name__ == "__main__":
    asyncio.run(main())
  
