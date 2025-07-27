import time
import requests
import hashlib
import hmac
import base64
import json
import threading
from flask import Flask
import logging
from datetime import datetime

# === API НАСТРОЙКИ ===
BITGET_API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"

TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

TRADE_AMOUNT = 10
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT"]
INTERVAL = "1m"
TP_PERCENT = 0.015
SL_PERCENT = 0.01

app = Flask(__name__)

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})
    except Exception as e:
        print(f"Ошибка Telegram: {e}")

def get_timestamp():
    return str(int(time.time() * 1000))

def sign_request(timestamp, method, request_path, body=""):
    prehash = f"{timestamp}{method}{request_path}{body}"
    sign = hmac.new(BITGET_API_SECRET.encode(), prehash.encode(), hashlib.sha256).hexdigest()
    return sign

def get_headers(method, path, body=""):
    timestamp = get_timestamp()
    sign = sign_request(timestamp, method, path, body)
    return {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json"
    }

def get_candles(symbol):
    try:
        url = f"https://api.bitget.com/api/v2/market/candles?symbol={symbol}UMCBL&granularity=1min&limit=30"
        response = requests.get(url)
        data = response.json().get("data", [])
        if not data or len(data) < 22:
            return None
        return [float(c[4]) for c in data]  # close prices
    except:
        return None

def calculate_ema(prices, period):
    if not prices or len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = prices[0]
    for price in prices[1:]:
        ema = price * k + ema * (1 - k)
    return ema

def get_position(symbol):
    try:
        url = f"/api/v2/mix/position/single-position?symbol={symbol}UMCBL&marginCoin=USDT"
        headers = get_headers("GET", url)
        res = requests.get("https://api.bitget.com" + url, headers=headers)
        return res.json()
    except:
        return None

def place_order(symbol, side):
    price_url = f"https://api.bitget.com/api/v2/market/ticker?symbol={symbol}UMCBL"
    price_res = requests.get(price_url).json()
    price = float(price_res.get("data", {}).get("last", 0))
    if price == 0:
        send_telegram(f"❌ Не удалось получить цену для {symbol}")
        return

    size = round(TRADE_AMOUNT / price, 3)
    order_data = {
        "symbol": symbol + "UMCBL",
        "marginCoin": "USDT",
        "size": str(size),
        "side": "open_long" if side == "LONG" else "open_short",
        "orderType": "market"
    }

    path = "/api/v2/mix/order/place-order"
    headers = get_headers("POST", path, json.dumps(order_data))
    res = requests.post("https://api.bitget.com" + path, headers=headers, data=json.dumps(order_data))

    try:
        result = res.json()
        if result.get("code") == "00000":
            send_telegram(f"✅ Открыт {side} по {symbol}\nЦена: {price}")
        else:
            send_telegram(f"⚠️ Ошибка при открытии {side} на {symbol}:\n{result}")
    except:
        send_telegram("⚠️ Не удалось распарсить ответ от Bitget")

def strategy_loop():
    while True:
        for symbol in SYMBOLS:
            prices = get_candles(symbol)
            if not prices:
                send_telegram(f"⚠️ Нет данных по {symbol}")
                continue

            ema9 = calculate_ema(prices[-10:], 9)
            ema21 = calculate_ema(prices[-22:], 21)

            if not ema9 or not ema21:
                continue

            if ema9 > ema21:
                place_order(symbol, "LONG")
            elif ema9 < ema21:
                place_order(symbol, "SHORT")

        time.sleep(60)

@app.route("/")
def home():
    return "🤖 Bitget Futures Bot активен!"

if __name__ == "__main__":
    send_telegram("🤖 Бот запущен и ждёт сигнала по фьючерсам Bitget!")
    threading.Thread(target=strategy_loop).start()
    app.run(host="0.0.0.0", port=8080)
