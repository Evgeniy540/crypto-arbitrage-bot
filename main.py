import time
import hmac
import hashlib
import base64
import requests
import json
from flask import Flask
import threading

# === НАСТРОЙКИ ===
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"
API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
API_PASSPHRASE = "Evgeniy84"
TRADE_AMOUNT = 10
SYMBOLS = ["BTCUSDT_UMCBL", "ETHUSDT_UMCBL", "SOLUSDT_UMCBL", "XRPUSDT_UMCBL", "TRXUSDT_UMCBL"]

# === Flask Keep-Alive ===
app = Flask(__name__)
@app.route('/')
def home():
    return "Crypto Bot is running!"
def run_flask():
    app.run(host='0.0.0.0', port=10000)

# === Telegram ===
def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }
    try:
        requests.post(url, data=payload)
    except Exception as e:
        print(f"Ошибка при отправке сообщения в Telegram: {str(e)}")

# === Получение свечей с Bitget ===
def get_klines(symbol):
    url = "https://api.bitget.com/api/mix/v1/market/history-candles"
    params = {
        "symbol": symbol,
        "granularity": "1min",
        "limit": "100"
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        if isinstance(data, dict) and "data" in data and isinstance(data["data"], list) and len(data["data"]) > 0:
            return data["data"]
        else:
            message = f"⚠️ Не удалось получить свечи по {symbol}.
Ответ API: {json.dumps(data)}"
            print(message)
            send_telegram_message(message)
            return None

    except Exception as e:
        message = f"❌ Ошибка при запросе свечей по {symbol}: {str(e)}"
        print(message)
        send_telegram_message(message)
        return None

# === Примерная торговая логика ===
def analyze_and_trade(symbol):
    candles = get_klines(symbol)
    if candles is None:
        return

    closes = [float(c[4]) for c in candles]
    ema9 = sum(closes[-9:]) / 9
    ema21 = sum(closes[-21:]) / 21

    if ema9 > ema21:
        send_telegram_message(f"📈 Сигнал на покупку: {symbol}")
    else:
        print(f"Нет сигнала на {symbol}")

# === Главный цикл ===
def main_loop():
    while True:
        for symbol in SYMBOLS:
            analyze_and_trade(symbol)
            time.sleep(2)
        time.sleep(60)

# === Старт ===
if __name__ == "__main__":
    send_telegram_message("🤖 Бот запущен на Render!")
    threading.Thread(target=run_flask).start()
    main_loop()
