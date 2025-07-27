import time
import hmac
import hashlib
import base64
import requests
import json
import threading
from flask import Flask
import logging
from datetime import datetime
import statistics

# === КЛЮЧИ Bitget ===
API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
API_PASSPHRASE = "Evgeniy84"

# === НАСТРОЙКИ ===
TRADE_SYMBOLS = ["btcusdt_UMCBL", "ethusdt_UMCBL", "solusdt_UMCBL", "xrpusdt_UMCBL", "trxusdt_UMCBL"]
TRADE_AMOUNT = 10
TP_PERCENT = 1.5
SL_PERCENT = 1
INTERVAL = 60  # минут
BASE_URL = "https://api.bitget.com"
TRADE_COOLDOWN = 60 * 60 * 3
last_trade_time = {}

# === TELEGRAM ===
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=payload)
    except Exception as e:
        print("Telegram error:", e)

def sign_request(timestamp, method, request_path, body=""):
    message = f"{timestamp}{method}{request_path}{body}"
    signature = hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha256)
    return base64.b64encode(signature.digest()).decode()

def get_headers(method, request_path, body=""):
    timestamp = str(int(time.time() * 1000))
    sign = sign_request(timestamp, method, request_path, body)
    return {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": API_PASSPHRASE,
        "Content-Type": "application/json"
    }

def get_candles(symbol):
    url = f"/api/mix/v1/market/history-candles?symbol={symbol}&granularity=1min"
    try:
        response = requests.get(BASE_URL + url, headers=get_headers("GET", url))
        data = response.json()
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
            return list(reversed(data["data"]))
    except Exception as e:
        print(f"Ошибка свечей {symbol}:", e)
    return None

def calculate_ema(values, period):
    if len(values) < period:
        return None
    return statistics.fmean(values[-period:])

def place_order(symbol, side):
    url = "/api/mix/v1/order/place"
    body = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "side": side,
        "orderType": "market",
        "size": str(TRADE_AMOUNT),
        "productType": "umcbl"
    }
    body_json = json.dumps(body)
    try:
        headers = get_headers("POST", url, body_json)
        r = requests.post(BASE_URL + url, headers=headers, data=body_json)
        print("ОРДЕР:", r.text)
        return r.json()
    except Exception as e:
        print("Ошибка размещения ордера:", e)

def strategy(symbol):
    now = time.time()
    if symbol in last_trade_time and now - last_trade_time[symbol] < TRADE_COOLDOWN:
        return

    candles = get_candles(symbol)
    if not candles or len(candles) < 22:
        send_telegram(f"⚠️ Не удалось получить свечи по {symbol}")
        return

    closes = [float(c[4]) for c in candles]
    ema9 = calculate_ema(closes, 9)
    ema21 = calculate_ema(closes, 21)

    if ema9 is None or ema21 is None:
        return

    price = closes[-1]
    if ema9 > ema21:
        # Покупка (LONG)
        order = place_order(symbol, "open_long")
        if order and order.get("code") == "00000":
            send_telegram(f"✅ LONG по {symbol} @ {price}")
            last_trade_time[symbol] = now
    elif ema9 < ema21:
        # Продажа (SHORT)
        order = place_order(symbol, "open_short")
        if order and order.get("code") == "00000":
            send_telegram(f"✅ SHORT по {symbol} @ {price}")
            last_trade_time[symbol] = now

def trade_loop():
    send_telegram("🤖 Бот запущен и готов к торговле!")
    while True:
        for symbol in TRADE_SYMBOLS:
            try:
                strategy(symbol)
            except Exception as e:
                print("Ошибка стратегии:", e)
        time.sleep(INTERVAL)

# === FLASK ===
app = Flask(__name__)

@app.route("/")
def home():
    return "🤖 Bitget Trading Bot работает!"

if __name__ == "__main__":
    threading.Thread(target=trade_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
