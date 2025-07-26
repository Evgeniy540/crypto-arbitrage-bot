import requests
import time
import hmac
import base64
import hashlib
import json
import threading
import numpy as np
from flask import Flask
import logging
import datetime

# === API КЛЮЧИ И НАСТРОЙКИ ===
API_KEY = "68855c7628335c0001f5d42e"
API_SECRET = "0c475ab6-4588-4301-9eb3-77c493b7e621"
API_PASSPHRASE = "198483"
TRADE_AMOUNT = 50
LEVERAGE = 5
SYMBOLS = ["BTCUSDTM", "ETHUSDTM", "SOLUSDTM", "TRXUSDTM", "GALAUSDTM"]
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"
COOLDOWN = 60 * 60 * 3
last_trade_time = {}

BASE_URL = "https://api-futures.kucoin.com"

# === TELEGRAM ===
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=data)
    except:
        pass

# === ПОДПИСЬ ДЛЯ ЗАПРОСОВ ===
def get_headers(method, endpoint, body=""):
    now = int(time.time() * 1000)
    str_to_sign = f"{now}{method.upper()}{endpoint}{body}"
    signature = base64.b64encode(hmac.new(API_SECRET.encode(), str_to_sign.encode(), hashlib.sha256).digest()).decode()
    passphrase = base64.b64encode(hmac.new(API_SECRET.encode(), API_PASSPHRASE.encode(), hashlib.sha256).digest()).decode()
    return {
        "KC-API-KEY": API_KEY,
        "KC-API-SIGN": signature,
        "KC-API-TIMESTAMP": str(now),
        "KC-API-PASSPHRASE": passphrase,
        "KC-API-KEY-VERSION": "2",
        "Content-Type": "application/json"
    }

# === ПОЛУЧИТЬ СВЕЧИ ===
def get_klines(symbol):
    try:
        endpoint = f"/api/v1/kline/query?symbol={symbol}&granularity=1"
        url = BASE_URL + endpoint
        headers = get_headers("GET", f"/api/v1/kline/query?symbol={symbol}&granularity=1")
        res = requests.get(url, headers=headers).json()
        if "data" not in res or not res["data"]:
            return None
        closes = [float(candle[2]) for candle in res["data"][-50:]]
        return closes
    except:
        return None

# === ПРОВЕРКА НА EMA 9/21 ===
def should_buy_or_sell(closes):
    ema9 = np.mean(closes[-9:])
    ema21 = np.mean(closes[-21:])
    if ema9 > ema21:
        return "buy"
    elif ema9 < ema21:
        return "sell"
    else:
        return None

# === ОТКРЫТИЕ ПОЗИЦИИ ===
def place_order(symbol, side):
    endpoint = "/api/v1/orders"
    url = BASE_URL + endpoint
    data = {
        "symbol": symbol,
        "side": side,
        "type": "market",
        "leverage": LEVERAGE,
        "size": str(TRADE_AMOUNT),
        "clientOid": str(int(time.time() * 1000))
    }
    body = json.dumps(data)
    headers = get_headers("POST", endpoint, body)
    try:
        r = requests.post(url, headers=headers, data=body)
        response = r.json()
        if response.get("code") != "200000":
            send_telegram(f"⚠️ Ошибка при открытии {side} на {symbol}:\n{response}")
        else:
            send_telegram(f"✅ Открыта {side.upper()} позиция по {symbol} на {TRADE_AMOUNT} USDT")
    except Exception as e:
        send_telegram(f"❌ Исключение при открытии ордера: {e}")

# === ТОРГОВАЯ ЛОГИКА ===
def trade():
    send_telegram("🤖 Бот запущен на KuCoin Futures!")
    while True:
        for symbol in SYMBOLS:
            try:
                if symbol in last_trade_time and time.time() - last_trade_time[symbol] < COOLDOWN:
                    continue
                closes = get_klines(symbol)
                if not closes:
                    continue
                action = should_buy_or_sell(closes)
                if action:
                    place_order(symbol, action)
                    last_trade_time[symbol] = time.time()
            except Exception as e:
                send_telegram(f"⚠️ Ошибка по {symbol}: {e}")
        time.sleep(60)

# === FLASK KEEP-ALIVE ===
app = Flask(__name__)
@app.route('/')
def home():
    return "KuCoin Futures Bot is running!"

# === ЗАПУСК ===
if __name__ == "__main__":
    threading.Thread(target=trade).start()
    app.run(host="0.0.0.0", port=10000)
