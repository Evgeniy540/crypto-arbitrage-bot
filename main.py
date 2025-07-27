import time
import hmac
import hashlib
import base64
import requests
import json
from flask import Flask
import threading
import logging
import numpy as np
import telegram

# === НАСТРОЙКИ ===
API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
API_PASSPHRASE = "Evgeniy84"

SYMBOLS = ["BTCUSDTUMCBL", "ETHUSDTUMCBL", "SOLUSDTUMCBL"]
TRADE_AMOUNT = 10
EMA_FAST = 9
EMA_SLOW = 21
CHECK_INTERVAL = 60  # секунд между проверками

# === TELEGRAM ===
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"
bot = telegram.Bot(token=TELEGRAM_TOKEN)

# === FLASK ===
app = Flask(__name__)
@app.route("/")
def home():
    return "✅ Bitget Futures Bot is running"

def send_telegram(message):
    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
    except Exception as e:
        print("Ошибка Telegram:", e)

def sign_request(timestamp, method, path, body):
    message = f"{timestamp}{method}{path}{body}"
    signature = hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    return signature

def get_headers(method, path, body=""):
    timestamp = str(int(time.time() * 1000))
    signature = sign_request(timestamp, method, path, body)
    return {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": signature,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": API_PASSPHRASE,
        "Content-Type": "application/json"
    }

def get_candles(symbol):
    url = f"https://api.bitget.com/api/mix/v1/market/candles?symbol={symbol}&granularity=60&productType=umcbl"
    try:
        response = requests.get(url)
        data = response.json()
        closes = [float(c[4]) for c in data['data']][-EMA_SLOW:]
        return closes
    except:
        return []

def calculate_ema(prices, period):
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    a = np.convolve(prices, weights, mode='full')[:len(prices)]
    a[:period] = a[period]
    return a

def place_order(symbol, side):
    path = "/api/mix/v1/order/placeOrder"
    url = "https://api.bitget.com" + path
    body = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "side": side,
        "orderType": "market",
        "size": str(TRADE_AMOUNT),
        "price": "",  # Market order
        "tradeSide": "open",
        "productType": "umcbl"
    }
    body_json = json.dumps(body)
    headers = get_headers("POST", path, body_json)
    try:
        res = requests.post(url, headers=headers, data=body_json)
        res_json = res.json()
        if res_json["code"] == "00000":
            send_telegram(f"✅ Открыта {side.upper()} по {symbol}")
        else:
            send_telegram(f"⚠️ Ошибка при открытии {side.upper()} на {symbol}: {res_json}")
    except Exception as e:
        send_telegram(f"❌ Ошибка запроса: {e}")

def strategy():
    while True:
        for symbol in SYMBOLS:
            closes = get_candles(symbol)
            if len(closes) < EMA_SLOW:
                print(f"Недостаточно данных для {symbol}")
                continue
            ema_fast = calculate_ema(closes, EMA_FAST)[-1]
            ema_slow = calculate_ema(closes, EMA_SLOW)[-1]

            print(f"{symbol}: EMA{EMA_FAST}={ema_fast:.2f}, EMA{EMA_SLOW}={ema_slow:.2f}")

            if ema_fast > ema_slow:
                place_order(symbol, "buy")   # LONG
            elif ema_fast < ema_slow:
                place_order(symbol, "sell")  # SHORT

        time.sleep(CHECK_INTERVAL)

# === ЗАПУСК ===
def start_bot():
    send_telegram("🤖 Бот Bitget фьючерсы запущен и готов к торговле!")
    strategy()

if __name__ == "__main__":
    threading.Thread(target=start_bot).start()
    app.run(host="0.0.0.0", port=8080)
