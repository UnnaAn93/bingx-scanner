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
    return "Bitget Account Assets Bot is running!"

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

async def check_account_assets():
    if not BITGET_API_KEY or not BITGET_SECRET_KEY or not BITGET_PASSPHRASE:
        print("❌ [Asset Check] Помилка: відсутні API-ключі Bitget!", flush=True)
        return

    base_url = "https://api.bitget.com"
    endpoint = "/api/v2/account/assets"
    
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
    print(f"🔍 [Asset Check] Запит активів: {url}", flush=True)
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as response:
                print(f"📥 [Asset Check] Статус: {response.status}", flush=True)
                data = await response.json()
                print(f"📦 [Asset Check] Відповідь: {data}", flush=True)
                
                if data.get("code") == "00000":
                    print("✅ Успішно підключено до акаунта!", flush=True)
                    assets = data.get("data", [])
                    for asset in assets:
                        coin = asset.get("coin")
                        available = asset.get("available")
                        if float(available or 0) > 0:
                            print(f"💰 Монета: {coin}, Доступно: {available}", flush=True)
                else:
                    print(f"⚠️ [Asset Check] Помилка від біржі: {data}", flush=True)
    except Exception as e:
        print(f"❌ [Asset Check] Виняток: {e}", flush=True)

async def background_scanner():
    await asyncio.sleep(5)
    print("🤖 [Scanner] Фоновий процес стартував!", flush=True)
    
    while True:
        try:
            print("🔄 Запуск перевірки активів...", flush=True)
            await check_account_assets()
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
    
