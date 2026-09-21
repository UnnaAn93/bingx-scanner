import os
import time
import hmac
import hashlib
import base64
import aiohttp
import asyncio
from flask import Flask

# Ініціалізація вебсервера для Render (щоб сервіс не засинав)
app = Flask(__name__)

@app.route("/")
def home():
    return "Bitget Scanner & Position Bot is running!"

# Змінні середовища
BITGET_API_KEY = os.getenv("BITGET_API_KEY")
BITGET_SECRET_KEY = os.getenv("BITGET_SECRET_KEY")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

async def send_discord_notification(message: str):
    """Надсилання повідомлень у Discord"""
    if not DISCORD_WEBHOOK_URL:
        print("⚠️ [Discord] Webhook URL не налаштовано!")
        return
    
    payload = {"content": message}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10) as resp:
                if resp.status not in [200, 204]:
                    print(f"⚠️ [Discord] Помилка відправки: статус {resp.status}")
        except Exception as e:
            print(f"❌ [Discord] Виняток при відправці: {e}")

async def check_zec_position():
    """Перевірка позиції ZECUSDT через Bitget UNI API з детальним логуванням"""
    if not BITGET_API_KEY or not BITGET_SECRET_KEY or not BITGET_PASSPHRASE:
        print("❌ [ZEC Check] Помилка: відсутні API-ключі Bitget у змінних середовища!")
        return

    base_url = "https://api.bitget.com"
    endpoint = "/api/v2/uni/mix/position/all-position"
    params = {"productType": "USDT-FUTURES"}
    
    timestamp = str(int(time.time() * 1000))
    method = "GET"
    
    # Підпис за стандартами Bitget V2
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
    print(f"🔍 [ZEC Check] Надсилаю запит до Bitget UNI API: {url}")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params, timeout=10) as response:
                print(f"📥 [ZEC Check] Статус відповіді від Bitget: {response.status}")
                text_response = await response.text()
                print(f"📦 [ZEC Check] Тіло відповіді: {text_response}")
                
                data = await response.json()
                if data.get("code") == "00000":
                    positions = data.get("data", [])
                    print(f"📊 [ZEC Check] Успішно отримано позицій: {len(positions)}")
                    found = False
                    for pos in positions:
                        if pos.get("symbol") == "ZECUSDT":
                            found = True
                            hold_side = pos.get("holdSide")
                            total = pos.get("total")
                            print(f"🎯 Знайдено позицію ZECUSDT! Сторона: {hold_side}, Об'єм: {total}")
                            await send_discord_notification(f"📊 **Позиція ZECUSDT**: {hold_side}, об'єм: {total}")
                    if not found:
                        print("ℹ️ [ZEC Check] Позиція ZECUSDT наразі відсутня в списку відкритих.")
                else:
                    print(f"⚠️ [ZEC Check] Помилка від біржі Bitget: {data}")
    except Exception as e:
        print(f"❌ [ZEC Check] Виняток під час запиту позиції: {e}")

async def background_scanner():
    """Головний цикл бота: сканування ринку та періодична перевірка позицій"""
    await asyncio.sleep(5)  клієнтів
    await send_discord_notification("🤖 **Бот оновлено!** Переведено на ендпоінти Єдиного акаунта Bitget для відстеження ZEC.")
    
    while True:
        try:
            print("🔄 Запуск чергового циклу перевірки...")
            # Тут виконується перевірка позиції
            await check_zec_position()
            
        except Exception as e:
            print(f"❌ Помилка в циклі сканера: {e}")
            
        # Пауза між перевірками (наприклад, 60 секунд)
        await asyncio.sleep(60)

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    # Запускаємо фоновий цикл бота асинхронно разом із Flask
    loop = asyncio.get_event_loop()
    loop.create_task(background_scanner())
    
    # Запуск вебсервера
    run_flask()
    
