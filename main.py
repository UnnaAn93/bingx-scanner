import os
import time
import hmac
import hashlib
import aiohttp
import asyncio
import threading
from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "BingX Scanner Bot is running!"

BINGX_API_KEY = os.getenv("BINGX_API_KEY")
BINGX_SECRET_KEY = os.getenv("BINGX_SECRET_KEY")
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

def get_bingX_signature(secret_key, message):
    return hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()

async def check_bingx_positions():
    if not BINGX_API_KEY or not BINGX_SECRET_KEY:
        print("❌ [BingX Check] Відсутні API-ключі BingX!", flush=True)
        return

    base_url = "https://open-api.bingx.com"
    endpoint = "/openApi/swap/v2/user/positions"
    
    timestamp = str(int(time.time() * 1000))
    params_str = f"timestamp={timestamp}"
    
    signature = get_bingX_signature(BINGX_SECRET_KEY, params_str)
    url = f"{base_url}{endpoint}?{params_str}&signature={signature}"
    
    headers = {
        "X-BX-APIKEY": BINGX_API_KEY
    }

    print(f"🔍 [BingX Check] Запит позицій: {url}", flush=True)
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as response:
                print(f"📥 [BingX Check] Статус: {response.status}", flush=True)
                data = await response.json()
                print(f"📦 [BingX Check] Відповідь: {data}", flush=True)
                
                if data.get("code") == 0:
                    positions = data.get("data", [])
                    if positions:
                        for pos in positions:
                            symbol = pos.get("symbol")
                            size = pos.get("positionAmt")
                            print(f"📈 Знайдено позицію: {symbol}, Розмір: {size}", flush=True)
                    else:
                        print("ℹ️ Немає відкритих позицій.", flush=True)
                else:
                    print(f"⚠️ Помилка від біржі BingX: {data}", flush=True)
    except Exception as e:
        print(f"❌ [BingX Check] Виняток: {e}", flush=True)

async def background_scanner():
    await asyncio.sleep(5)
    print("🤖 [Scanner] BingX фоновий сканер стартував!", flush=True)
    
    while True:
        try:
            print("🔄 Сканування відкритих позицій на BingX...", flush=True)
            await check_bingx_positions()
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
    
