import time
import hmac
import hashlib
import json
import requests
import threading
import os
from flask import Flask
from datetime import datetime

# === КЛЮЧИ ===
API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
API_PASSPHRASE = "Evgeniy84"

TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

# === НАСТРОЙКИ ===
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT"]
TRADE_AMOUNT = 10  # USDT
TP_PERCENT = 1.5
SL_PERCENT = 1.0

# === Flask для Render ===
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

# === Telegram уведомления ===
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Ошибка отправки в Telegram: {e}")

# === Bitget API запрос ===
def bitget_request(method, endpoint, params=None, body=None):
    url = f"https://api.bitget.com{endpoint}"
    timestamp = str(int(time.time() * 1000))
    body_str = json.dumps(body) if body else ""
    params_str = ""
    if params:
        params_str = "&".join([f"{k}={v}" for k, v in params.items()])
        url += "?" + params_str
    pre_sign = timestamp + method + endpoint + (params_str if method == "GET" else body_str)
    sign = hmac.new(API_SECRET.encode(), pre_sign.encode(), hashlib.sha256).hexdigest()
    headers = {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": API_PASSPHRASE,
        "Content-Type": "application/json"
    }
    r = requests.request(method, url, headers=headers, data=body_str)
    return r.json()

# === Получение цены ===
def get_price(symbol):
    try:
        data = bitget_request("GET", "/api/spot/v1/market/ticker", params={"symbol": symbol})
        return float(data["data"]["last"])
    except:
        return None

# === Логика сигналов (простая EMA 9/21) ===
def get_signal(symbol):
    # Для простоты здесь просто тест на случайный сигнал
    import random
    if random.randint(1, 10) > 7:
        return "BUY"
    elif random.randint(1, 10) < 3:
        return "SELL"
    else:
        return None

# === Основной цикл бота ===
def run_bot():
    send_telegram("🤖 Бот запущен на Render!")
    while True:
        for symbol in SYMBOLS:
            signal = get_signal(symbol)
            if signal:
                send_telegram(f"📢 Сигнал по {symbol}: {signal}")
        time.sleep(60)

if __name__ == "__main__":
    threading.Thread(target=run_bot).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
