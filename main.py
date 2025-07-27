import time
import requests
import hmac
import hashlib
import json
import base64
import logging
from flask import Flask
import threading
import datetime

# === КЛЮЧИ И НАСТРОЙКИ ===
BITGET_API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"

TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

TRADE_AMOUNT = 10  # USDT
TP_PERCENT = 1.5
SL_PERCENT = 1.0
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]

BASE_URL = "https://api.bitget.com"

# === ФЛАСК ===
app = Flask(__name__)

@app.route('/')
def home():
    return "🤖 Bitget Futures Bot is running!"

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        requests.post(url, data=data)
    except:
        pass

def get_server_time():
    res = requests.get(BASE_URL + "/api/mix/v1/common/server-time")
    return str(res.json()['data'])

def sign_request(timestamp, method, path, body=''):
    message = timestamp + method + path + body
    signature = hmac.new(BITGET_API_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    return signature

def bitget_headers(method, path, body=''):
    timestamp = get_server_time()
    sign = sign_request(timestamp, method, path, body)
    return {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json"
    }

def get_klines(symbol):
    url = f"{BASE_URL}/api/mix/v1/market/candles?symbol={symbol}UMCBL&granularity=1m"
    r = requests.get(url)
    data = r.json()
    if "data" in data:
        closes = [float(c[4]) for c in data['data']][::-1]
        return closes
    return []

def calculate_ema(prices, period):
    if len(prices) < period:
        return None
    ema = prices[0]
    k = 2 / (period + 1)
    for price in prices[1:]:
        ema = price * k + ema * (1 - k)
    return ema

def place_order(symbol, side, size):
    path = "/api/mix/v1/order/placeOrder"
    url = BASE_URL + path
    data = {
        "symbol": symbol + "UMCBL",
        "marginCoin": "USDT",
        "side": side,
        "orderType": "market",
        "size": str(size),
        "tradeSide": "open"
    }
    headers = bitget_headers("POST", path, json.dumps(data))
    res = requests.post(url, headers=headers, data=json.dumps(data))
    return res.json()

def close_position(symbol, side, size):
    path = "/api/mix/v1/order/placeOrder"
    url = BASE_URL + path
    data = {
        "symbol": symbol + "UMCBL",
        "marginCoin": "USDT",
        "side": side,
        "orderType": "market",
        "size": str(size),
        "tradeSide": "close"
    }
    headers = bitget_headers("POST", path, json.dumps(data))
    res = requests.post(url, headers=headers, data=json.dumps(data))
    return res.json()

def check_position(symbol):
    path = f"/api/mix/v1/position/singlePosition?symbol={symbol}UMCBL&marginCoin=USDT"
    url = BASE_URL + path
    headers = bitget_headers("GET", path)
    res = requests.get(url, headers=headers)
    return res.json()

def trade_loop():
    while True:
        for symbol in SYMBOLS:
            try:
                prices = get_klines(symbol)
                if len(prices) < 21:
                    continue
                ema9 = calculate_ema(prices[-9:], 9)
                ema21 = calculate_ema(prices[-21:], 21)

                current_price = prices[-1]

                if ema9 and ema21:
                    position = check_position(symbol)
                    holding = float(position['data']['total'])

                    if ema9 > ema21 and holding == 0:
                        size = round(TRADE_AMOUNT / current_price, 3)
                        result = place_order(symbol, "buy", size)
                        send_telegram_message(f"📈 Открыт LONG по {symbol}, цена: {current_price}, size: {size}\nОтвет: {result}")
                    
                    elif ema9 < ema21 and holding > 0:
                        size = float(position['data']['available'])
                        result = close_position(symbol, "sell", size)
                        send_telegram_message(f"📉 Закрыт LONG по {symbol}, цена: {current_price}, size: {size}\nОтвет: {result}")
            except Exception as e:
                send_telegram_message(f"⚠️ Ошибка по {symbol}: {e}")
        time.sleep(60)

# === ЗАПУСК ===
if __name__ == "__main__":
    send_telegram_message("🤖 Бот запущен и готов к торговле на Bitget (Futures)")
    threading.Thread(target=trade_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
