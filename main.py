# === main.py ===
import time
import hmac
import hashlib
import base64
import requests
import json
from datetime import datetime
from flask import Flask
import threading
import numpy as np

# === Bitget API keys ===
API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
API_PASSPHRASE = "Evgeniy84"

# === Telegram Bot ===
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

# === Настройки торговли ===
TRADE_AMOUNT = 5
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT", "PEPEUSDT", "BGBUSDT"]
POSITION = None
ENTRY_PRICE = None
IN_POSITION_SYMBOL = None
LAST_NO_SIGNAL_TIME = 0
HEADERS = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}

# === Flask для Render ===
app = Flask(__name__)

@app.route('/')
def home():
    return '✅ Crypto Bot is running!'

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})

def sign_request(timestamp, method, request_path, body=""):
    message = str(timestamp) + method + request_path + body
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

def get_balance():
    url = "https://api.bitget.com/api/spot/v1/account/assets"
    headers = get_headers("GET", "/api/spot/v1/account/assets")
    resp = requests.get(url, headers=headers).json()
    for asset in resp.get("data", []):
        if asset["coinName"] == "USDT":
            return float(asset["available"])
    return 0

def get_candles(symbol):
    url = f"https://api.bitget.com/api/spot/v1/market/candles?symbol={symbol}&granularity=60"
    try:
        r = requests.get(url, headers=HEADERS)
        data = r.json().get("data", [])
        closes = [float(c[4]) for c in data[::-1]]
        return closes
    except:
        return []

def calculate_ema(prices, period):
    return np.convolve(prices, np.ones(period)/period, mode='valid')

def place_order(symbol, side, size):
    url = "https://api.bitget.com/api/spot/v1/trade/orders"
    body = {
        "symbol": symbol,
        "side": side,
        "orderType": "market",
        "force": "gtc",
        "size": str(size)
    }
    body_json = json.dumps(body)
    headers = get_headers("POST", "/api/spot/v1/trade/orders", body_json)
    response = requests.post(url, headers=headers, data=body_json).json()
    return response

def check_signal():
    global POSITION, ENTRY_PRICE, IN_POSITION_SYMBOL, TRADE_AMOUNT, LAST_NO_SIGNAL_TIME
    if POSITION:
        url = f"https://api.bitget.com/api/spot/v1/market/ticker?symbol={IN_POSITION_SYMBOL}"
        data = requests.get(url).json().get("data", {})
        last_price = float(data.get("last", 0))
        if last_price >= ENTRY_PRICE * 1.015:
            place_order(IN_POSITION_SYMBOL, "sell", POSITION)
            send_telegram(f"✅ Продажа {IN_POSITION_SYMBOL} по TP! Цена: {last_price:.4f}")
            profit = (last_price - ENTRY_PRICE) * POSITION
            TRADE_AMOUNT += profit
            POSITION = None
            IN_POSITION_SYMBOL = None
        elif last_price <= ENTRY_PRICE * 0.99:
            place_order(IN_POSITION_SYMBOL, "sell", POSITION)
            send_telegram(f"🛑 Продажа {IN_POSITION_SYMBOL} по SL! Цена: {last_price:.4f}")
            POSITION = None
            IN_POSITION_SYMBOL = None
        return

    for symbol in SYMBOLS:
        candles = get_candles(symbol)
        if len(candles) < 21:
            continue
        ema9 = calculate_ema(candles[-21:], 9)
        ema21 = calculate_ema(candles[-21:], 21)
        if len(ema21) == 0:
            continue
        if ema9[-1] > ema21[-1]:
            balance = get_balance()
            if balance < TRADE_AMOUNT:
                send_telegram(f"❗Недостаточно USDT: {balance:.2f} доступно, нужно {TRADE_AMOUNT}")
                return
            price = candles[-1]
            qty = round(TRADE_AMOUNT / price, 6)
            resp = place_order(symbol, "buy", qty)
            if resp.get("code") == "00000":
                POSITION = qty
                ENTRY_PRICE = price
                IN_POSITION_SYMBOL = symbol
                send_telegram(f"📈 Покупка {symbol} по цене {price:.4f} на сумму {TRADE_AMOUNT} USDT")
            else:
                send_telegram(f"❌ Ошибка покупки {symbol}: {resp}")
            return

    if time.time() - LAST_NO_SIGNAL_TIME > 3600:
        send_telegram("ℹ️ Сейчас нет сигналов на вход")
        LAST_NO_SIGNAL_TIME = time.time()

def run_bot():
    send_telegram("🤖 Бот запущен и работает на Render!")
    while True:
        try:
            check_signal()
        except Exception as e:
            send_telegram(f"Ошибка: {e}")
        time.sleep(30)

# === Запуск ===
if __name__ == '__main__':
    threading.Thread(target=run_bot).start()
    app.run(host='0.0.0.0', port=8080)
