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
    return "Bitget Unified Scanner & Bot is running!"

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

async def check_uni_assets():
    if not BITGET_API_KEY or not BITGET_SECRET_KEY or not BITGET_PASSPHRASE:
        print("❌ [Uni Check] Помилка: відсутні API-ключі Bitget!", flush=True)
        return

    base_url = "https://api.bitget.com"
    endpoint = "/api/v2/uni/account/assets"
    
    timestamp = str(int(time.time() * 1000))
    method = "GET"
    
    # Для GET запиту без параметрів рядок підпису виглядає так:
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
    print(f"🔍 [Uni Check] Запит активів Unified API: {url}", flush=True)
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as response:
                print(f"📥 [Uni Check] Статус: {response.status}", flush=True)
                data = await response.json()
                print(f"📦 [Uni Check] Відповідь: {data}", flush=True)
                
                if data.get("code") == "00000":
                    print("✅ Успішно отримано дані єдиного акаунта!", flush=True)
                else:
                    print(f"⚠️ [Uni Check] Помилка від біржі: {data}", flush=True)
    except Exception as e:
        print(f"❌ [Uni Check] Виняток: {e}", flush=True)

async def background_scanner():
    await asyncio.sleep(5)
    print("🤖 [Scanner] Фоновий процес стартував!", flush=True)
    
    while True:
        try:
            print("🔄 Запуск перевірки єдиного акаунта...", flush=True)
            await check_uni_assets()
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
    
