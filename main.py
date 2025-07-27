import time
import hmac
import hashlib
import base64
import requests
import json
import logging
import os
import threading
import numpy as np
from flask import Flask

# === Настройки из переменных окружения ===
API_KEY = os.getenv("BITGET_API_KEY")
API_SECRET = os.getenv("BITGET_API_SECRET")
API_PASSPHRASE = os.getenv("BITGET_API_PASSPHRASE")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
TRADE_AMOUNT = 50
EMA_FAST = 9
EMA_SLOW = 21

HEADERS = {
    "Content-Type": "application/json",
    "ACCESS-KEY": API_KEY,
    "ACCESS-PASSPHRASE": API_PASSPHRASE
}

last_trade_time = {}

app = Flask(__name__)

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    except Exception as e:
        print(f"Telegram error: {e}")

def sign_request(timestamp, method, request_path, body=""):
    prehash = f'{timestamp}{method}{request_path}{body}'
    signature = hmac.new(
        API_SECRET.encode('utf-8'),
        prehash.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return signature

def get_klines(symbol):
    url = f"https://api.bitget.com/api/mix/market/candles?symbol={symbol}_UMCBL&granularity=60&limit=50"
    try:
        resp = requests.get(url).json()
        return [float(c[4]) for c in resp['data']]  # closing prices
    except Exception as e:
        send_telegram(f"❌ Ошибка получения свечей {symbol}: {e}")
        return []

def calculate_ema(prices, period):
    prices = np.array(prices)
    if len(prices) < period:
        return None
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    a = np.convolve(prices, weights, mode='full')[:len(prices)]
    a[:period] = a[period]
    return a

def place_order(symbol, side):
    endpoint = "/api/mix/v1/order/placeOrder"
    url = f"https://api.bitget.com{endpoint}"
    timestamp = str(int(time.time() * 1000))

    body = {
        "symbol": f"{symbol}_UMCBL",
        "marginCoin": "USDT",
        "side": side,
        "orderType": "market",
        "size": str(TRADE_AMOUNT),
        "timeInForceValue": "normal"
    }

    sign = sign_request(timestamp, "POST", endpoint, json.dumps(body))
    headers = {
        **HEADERS,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-SIGN": sign
    }

    response = requests.post(url, headers=headers, json=body)
    try:
        res_json = response.json()
        if res_json.get("code") == "00000":
            send_telegram(f"✅ Ордер {side} {symbol} размещён на {TRADE_AMOUNT} USDT")
        else:
            send_telegram(f"⚠️ Ошибка размещения ордера: {res_json}")
    except:
        send_telegram("⚠️ Не удалось обработать ответ от Bitget")

def trade(symbol):
    now = time.time()
    if symbol in last_trade_time and now - last_trade_time[symbol] < 60 * 60:
        return  # cooldown 1 час

    prices = get_klines(symbol)
    if not prices:
        return

    ema_fast = calculate_ema(prices, EMA_FAST)
    ema_slow = calculate_ema(prices, EMA_SLOW)
    if ema_fast is None or ema_slow is None:
        return

    if ema_fast[-1] > ema_slow[-1] and ema_fast[-2] <= ema_slow[-2]:
        place_order(symbol, "open_long")
        last_trade_time[symbol] = now
    elif ema_fast[-1] < ema_slow[-1] and ema_fast[-2] >= ema_slow[-2]:
        place_order(symbol, "open_short")
        last_trade_time[symbol] = now

def start_trading():
    send_telegram("🤖 Бот успешно запущен на Render!")
    while True:
        for sym in SYMBOLS:
            try:
                trade(sym)
            except Exception as e:
                send_telegram(f"❌ Ошибка торговли по {sym}: {e}")
        time.sleep(60)

@app.route('/')
def index():
    return "🤖 Crypto Futures Bot is Running!"

if __name__ == '__main__':
    t = threading.Thread(target=start_trading)
    t.start()
    app.run(host='0.0.0.0', port=10000)
