import time
import hmac
import hashlib
import base64
import requests
import json
import os
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
HEADERS = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}

# === Flask для Render ===
app = Flask(__name__)

@app.route('/')
def home():
    return '✅ Crypto Bot is running!'

# === Глобальные переменные ===
POSITION = None
ENTRY_PRICE = None
IN_POSITION_SYMBOL = None
LAST_NO_SIGNAL_TIME = 0
LAST_POSITION_ALERT = 0
TOTAL_PROFIT = 0
TRADES_COUNT = 0
POSITION_FILE = "position.json"

# === Telegram ===
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})

# === Подпись запроса Bitget ===
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

# === Баланс USDT ===
def get_balance():
    url = "https://api.bitget.com/api/spot/v1/account/assets"
    headers = get_headers("GET", "/api/spot/v1/account/assets")
    resp = requests.get(url, headers=headers).json()
    for asset in resp.get("data", []):
        if asset["coinName"] == "USDT":
            return float(asset["available"])
    return 0

# === Свечи ===
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

# === Ордер ===
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

# === Сохранение позиции ===
def save_position():
    global POSITION, ENTRY_PRICE, IN_POSITION_SYMBOL
    with open(POSITION_FILE, "w") as f:
        json.dump({
            "symbol": IN_POSITION_SYMBOL,
            "entry": ENTRY_PRICE,
            "qty": POSITION
        }, f)

def load_position():
    global POSITION, ENTRY_PRICE, IN_POSITION_SYMBOL
    if os.path.exists(POSITION_FILE):
        with open(POSITION_FILE, "r") as f:
            pos = json.load(f)
            IN_POSITION_SYMBOL = pos["symbol"]
            ENTRY_PRICE = pos["entry"]
            POSITION = pos["qty"]

# === Основная логика ===
def check_signal():
    global POSITION, ENTRY_PRICE, IN_POSITION_SYMBOL, LAST_NO_SIGNAL_TIME
    global LAST_POSITION_ALERT, TOTAL_PROFIT, TRADES_COUNT, TRADE_AMOUNT

    if POSITION:
        url = f"https://api.bitget.com/api/spot/v1/market/ticker?symbol={IN_POSITION_SYMBOL}"
        data = requests.get(url).json().get("data", {})
        last_price = float(data.get("last", 0))
        if last_price >= ENTRY_PRICE * 1.015:
            sell_resp = place_order(IN_POSITION_SYMBOL, "sell", POSITION)
            order_id = sell_resp.get("data", {}).get("orderId")
            profit = (last_price - ENTRY_PRICE) * POSITION
            TRADE_AMOUNT += profit
            TOTAL_PROFIT += profit
            TRADES_COUNT += 1
            send_telegram(f"✅ TP: {IN_POSITION_SYMBOL} продано по {last_price:.4f}
💰 +{profit:.4f} USDT
🆔 Ордер: {order_id}
📊 Сделок: {TRADES_COUNT}, Всего: {TOTAL_PROFIT:.4f} USDT")
            POSITION = None
            IN_POSITION_SYMBOL = None
            os.remove(POSITION_FILE)
        elif last_price <= ENTRY_PRICE * 0.99:
            sell_resp = place_order(IN_POSITION_SYMBOL, "sell", POSITION)
            order_id = sell_resp.get("data", {}).get("orderId")
            send_telegram(f"🛑 SL: {IN_POSITION_SYMBOL} продано по {last_price:.4f}
🆔 Ордер: {order_id}")
            POSITION = None
            IN_POSITION_SYMBOL = None
            os.remove(POSITION_FILE)
        elif time.time() - LAST_POSITION_ALERT > 1800:
            send_telegram(f"📍 В позиции: {IN_POSITION_SYMBOL} @ {ENTRY_PRICE:.4f}, объём: {POSITION}")
            LAST_POSITION_ALERT = time.time()
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
                order_id = resp.get("data", {}).get("orderId")
                save_position()
                send_telegram(f"📈 Покупка {symbol} по {price:.4f} на {TRADE_AMOUNT} USDT
🆔 Ордер: {order_id}")
            else:
                send_telegram(f"❌ Ошибка покупки {symbol}: {resp}")
            return

    if time.time() - LAST_NO_SIGNAL_TIME > 3600:
        send_telegram("ℹ️ Сейчас нет сигналов на вход")
        LAST_NO_SIGNAL_TIME = time.time()

def run_bot():
    send_telegram("🤖 Бот запущен и работает на Render!")
    load_position()
    while True:
        try:
            check_signal()
        except Exception as e:
            send_telegram(f"Ошибка: {e}")
        time.sleep(30)

# === Запуск ===
if __name__ == '__main__':
    threading.Thread(target=run_bot).start()
    app.run(host="0.0.0.0", port=8080)
