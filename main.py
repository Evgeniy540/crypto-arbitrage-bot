import requests
import time
import hmac
import hashlib
import base64
import json
import threading
from flask import Flask
import logging

# === КЛЮЧИ ===
API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
API_PASSPHRASE = "Evgeniy84"
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

# === НАСТРОЙКИ ===
SYMBOLS = ["BTCUSDT_UMCBL", "ETHUSDT_UMCBL", "SOLUSDT_UMCBL", "XRPUSDT_UMCBL", "TRXUSDT_UMCBL"]
TRADE_AMOUNT = 10  # в USDT
TP_PERCENT = 1.5
SL_PERCENT = 1.0
INTERVAL = "1min"
LIMIT = 100

# === Flask keep-alive ===
app = Flask(__name__)

@app.route('/')
def home():
    return "✅ Бот работает!"

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=payload)
    except Exception as e:
        print("Ошибка Telegram:", e)

def bitget_request(method, path, params=None, body=None):
    timestamp = str(int(time.time() * 1000))
    pre_hash = timestamp + method + path
    if body:
        pre_hash += json.dumps(body)
    sign = hmac.new(API_SECRET.encode(), pre_hash.encode(), hashlib.sha256).hexdigest()
    headers = {
        'ACCESS-KEY': API_KEY,
        'ACCESS-SIGN': sign,
        'ACCESS-TIMESTAMP': timestamp,
        'ACCESS-PASSPHRASE': API_PASSPHRASE,
        'Content-Type': 'application/json'
    }
    url = f"https://api.bitget.com{path}"
    try:
        if method == "GET":
            res = requests.get(url, headers=headers, params=params)
        elif method == "POST":
            res = requests.post(url, headers=headers, json=body)
        return res.json()
    except Exception as e:
        print("Ошибка Bitget API:", e)
        return None

def get_klines(symbol):
    url = f"https://api.bitget.com/api/mix/v1/market/history-candles"
    params = {
        "symbol": symbol,
        "granularity": "1min",
        "limit": str(LIMIT)
    }
    try:
        res = requests.get(url, params=params)
        data = res.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        else:
            send_telegram_message(f"⚠️ Не удалось получить свечи по {symbol}")
            return None
    except Exception as e:
        send_telegram_message(f"❌ Ошибка получения свечей {symbol}: {str(e)}")
        return None

def calculate_ema(data, period):
    prices = [float(candle[4]) for candle in data]
    ema = []
    k = 2 / (period + 1)
    for i in range(len(prices)):
        if i < period:
            ema.append(None)
        elif i == period:
            sma = sum(prices[i-period+1:i+1]) / period
            ema.append(sma)
        else:
            ema.append(prices[i] * k + ema[-1] * (1 - k))
    return ema

def place_order(symbol, side):
    path = "/api/mix/v1/order/placeOrder"
    order_data = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "size": str(TRADE_AMOUNT),
        "side": side,
        "orderType": "market",
        "tradeSide": side.lower(),
        "productType": "umcbl"
    }
    result = bitget_request("POST", path, body=order_data)
    return result

def strategy(symbol):
    candles = get_klines(symbol)
    if candles is None or len(candles) < 21:
        return
    ema9 = calculate_ema(candles, 9)
    ema21 = calculate_ema(candles, 21)
    if ema9[-1] and ema21[-1] and ema9[-1] > ema21[-1]:
        send_telegram_message(f"🟢 Сигнал на покупку {symbol}")
        order = place_order(symbol, "open_long")
        send_telegram_message(f"🛒 КУПИТЬ {symbol}: {order}")
    elif ema9[-1] and ema21[-1] and ema9[-1] < ema21[-1]:
        send_telegram_message(f"🔴 Сигнал на продажу {symbol}")
        order = place_order(symbol, "open_short")
        send_telegram_message(f"📉 ПРОДАТЬ {symbol}: {order}")

def run_bot():
    while True:
        for symbol in SYMBOLS:
            try:
                strategy(symbol)
            except Exception as e:
                print(f"Ошибка по {symbol}:", e)
        time.sleep(60)

# Запуск
send_telegram_message("🤖 Бот успешно запущен на Render!")
threading.Thread(target=run_bot, daemon=True).start()
app.run(host="0.0.0.0", port=10000)
