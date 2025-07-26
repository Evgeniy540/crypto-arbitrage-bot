import requests
import time
import hmac
import base64
import hashlib
import json
import threading
import logging
import math
from flask import Flask
import datetime

# === Настройки ===
API_KEY = "68855c7628335c0001f5d42e"
API_SECRET = "0c475ab6-4588-4301-9eb3-77c493b7e621"
API_PASSPHRASE = "Evgeniy@84"
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"
TRADE_AMOUNT = 50
SYMBOLS = ["BTCUSDTM", "ETHUSDTM", "SOLUSDTM", "GALAUSDTM", "TRXUSDTM"]

app = Flask(__name__)
last_trade_time = {}

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print(f"Ошибка Telegram: {e}")

def kucoin_request(method, endpoint, payload=None):
    url = "https://api-futures.kucoin.com" + endpoint
    now = int(time.time() * 1000)
    str_to_sign = str(now) + method.upper() + endpoint
    if payload:
        body = json.dumps(payload)
        str_to_sign += body
    else:
        body = ""

    signature = base64.b64encode(
        hmac.new(API_SECRET.encode("utf-8"), str_to_sign.encode("utf-8"), hashlib.sha256).digest()
    )
    passphrase = base64.b64encode(
        hmac.new(API_SECRET.encode("utf-8"), API_PASSPHRASE.encode("utf-8"), hashlib.sha256).digest()
    )

    headers = {
        "KC-API-KEY": API_KEY,
        "KC-API-SIGN": signature.decode(),
        "KC-API-TIMESTAMP": str(now),
        "KC-API-PASSPHRASE": passphrase.decode(),
        "KC-API-KEY-VERSION": "2",
        "Content-Type": "application/json"
    }

    try:
        if method.upper() == "GET":
            response = requests.get(url, headers=headers)
        elif method.upper() == "POST":
            response = requests.post(url, headers=headers, data=body)
        return response.json()
    except Exception as e:
        return {"code": "error", "msg": str(e)}

def get_ema(prices, period):
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = prices[0]
    for price in prices[1:]:
        ema = price * k + ema * (1 - k)
    return ema

def fetch_klines(symbol):
    endpoint = f"/api/v1/kline/query?symbol={symbol}&granularity=5"
    data = kucoin_request("GET", endpoint)
    if "data" in data and data["data"]:
        return [float(candle[2]) for candle in data["data"]][-21:]
    return []

def place_order(symbol, side):
    endpoint = "/api/v1/orders"
    price_data = kucoin_request("GET", f"/api/v1/mark-price/{symbol}")
    if "data" not in price_data:
        send_telegram_message(f"❌ Не удалось получить цену для {symbol}")
        return
    price = float(price_data["data"]["markPrice"])
    size = round(TRADE_AMOUNT / price, 3)
    order = {
        "symbol": symbol,
        "side": side,
        "type": "market",
        "size": size,
        "leverage": 5
    }
    response = kucoin_request("POST", endpoint, order)
    if response.get("code") == "200000":
        send_telegram_message(f"✅ Открыт {side.upper()} по {symbol} на {TRADE_AMOUNT} USDT")
    else:
        send_telegram_message(f"⚠️ Ошибка при открытии {side.upper()} на {symbol}: {response}")

def strategy_loop():
    send_telegram_message("🤖 Бот запущен на KuCoin Futures!")
    while True:
        for symbol in SYMBOLS:
            if symbol in last_trade_time and time.time() - last_trade_time[symbol] < 3600:
                continue
            prices = fetch_klines(symbol)
            if len(prices) < 21:
                continue
            ema9 = get_ema(prices[-9:], 9)
            ema21 = get_ema(prices, 21)
            if ema9 and ema21:
                if ema9 > ema21:
                    place_order(symbol, "buy")
                    last_trade_time[symbol] = time.time()
                elif ema9 < ema21:
                    place_order(symbol, "sell")
                    last_trade_time[symbol] = time.time()
        time.sleep(300)

threading.Thread(target=strategy_loop, daemon=True).start()

@app.route("/")
def home():
    return "KuCoin Futures бот работает!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
