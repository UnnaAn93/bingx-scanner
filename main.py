import os
import time
import hmac
import hashlib
import base64
import aiohttp
import asyncio
import threading
from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "Bitget Scanner & Position Bot is running!"

BITGET_API_KEY = os.getenv("BITGET_API_KEY")
BITGET_SECRET_KEY = os.getenv("BITGET_SECRET_KEY")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

async def send_discord_notification(message: str):
    if not DISCORD_WEBHOOK_URL:
        return
    payload = {"content": message}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10) as resp:
                pass
        except Exception as e:
            print(f"❌ [Discord Error]: {e}", flush=True)

async def check_zec_position():
    if not BITGET_API_KEY or not BITGET_SECRET_KEY or not BITGET_PASSPHRASE:
        print("❌ [ZEC Check] Помилка: відсутні API-ключі Bitget!", flush=True)
        return

    base_url = "https://api.bitget.com"
    # Виправляємо шлях для Unified Account позицій
    endpoint = "/api/v2/uni/mix/positions"
    params = {"productType": "USDT-FUTURES"}
    
    timestamp = str(int(time.time() * 1000))
    method = "GET"
    
    message = timestamp + method + endpoint
    signature = base64.b64encode(
        hmac.new(BITGET_SECRET_KEY.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    
    headers = {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": signature,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_PASSPHRASE,
        "Content-Type": "application/json"
    }

    url = base_url + endpoint
    print(f"🔍 [ZEC Check] Запит позицій Unified API: {url}", flush=True)
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params, timeout=10) as response:
                print(f"📥 [ZEC Check] Статус: {response.status}", flush=True)
                text_response = await response.text()
                print(f"📦 [ZEC Check] Тіло відповіді: {text_response}", flush=True)
                
                data = await response.json()
                if data.get("code") == "00000":
                    positions = data.get("data", [])
                    print(f"📊 [ZEC Check] Успішно отримано позицій: {len(positions)}", flush=True)
                    found = False
                    for pos in positions:
                        if pos.get("symbol") == "ZECUSDT":
                            found = True
                            hold_side = pos.get("holdSide")
                            total = pos.get("total")
                            print(f"🎯 Знайдено позицію ZECUSDT! Сторона: {hold_side}, Об'єм: {total}", flush=True)
                            await send_discord_notification(f"📊 **Позиція ZECUSDT**: {hold_side}, об'єм: {total}")
                    if not found:
                        print("ℹ️ [ZEC Check] Позиція ZECUSDT відсутня.", flush=True)
                else:
                    print(f"⚠️ [ZEC Check] Помилка від біржі: {data}", flush=True)
    except Exception as e:
        print(f"❌ [ZEC Check] Виняток: {e}", flush=True)

async def background_scanner():
    await asyncio.sleep(5)
    print("🤖 [Scanner] Фоновий процес стартував!", flush=True)
    
    while True:
        try:
            print("🔄 Запуск перевірки позицій...", flush=True)
            await check_zec_position()
        except Exception as e:
            print(f"❌ Помилка в циклі: {e}", flush=True)
            
        await asyncio.sleep(60)

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    def start_background_loop():
        asyncio.run(background_scanner())

    bot_thread = threading.Thread(target=start_background_loop, daemon=True)
    bot_thread.start()
    
    run_flask()
    
