import requests
import time
import hmac
import hashlib
import base64
import json
import threading
import logging
from flask import Flask
from datetime import datetime
import numpy as np

# === Настройки пользователя ===
API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
API_PASSPHRASE = "Evgeniy84"
TRADE_AMOUNT = 10  # USDT
SYMBOLS = ["BTCUSDT_UMCBL", "ETHUSDT_UMCBL", "SOLUSDT_UMCBL", "XRPUSDT_UMCBL", "TRXUSDT_UMCBL"]
TG_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TG_CHAT_ID = "5723086631"

# === Telegram ===
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TG_CHAT_ID, "text": message})
    except Exception as e:
        print("Ошибка отправки в Telegram:", e)

# === Bitget запросы ===
def get_server_time():
    url = "https://api.bitget.com/api/mix/v1/market/history-candles?symbol=BTCUSDT_UMCBL&granularity=1min&limit=2&productType=umcbl"
    try:
        response = requests.get(url)
        return int(time.time() * 1000)
    except:
        return int(time.time() * 1000)

def sign_request(timestamp, method, path, body=""):
    pre_hash = f"{timestamp}{method}{path}{body}"
    return base64.b64encode(hmac.new(API_SECRET.encode(), pre_hash.encode(), hashlib.sha256).digest()).decode()

def place_order(symbol, side, size):
    timestamp = str(get_server_time())
    method = "POST"
    path = "/api/mix/v1/order/placeOrder"
    url = "https://api.bitget.com" + path
    body = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "size": str(size),
        "side": side,
        "orderType": "market",
        "timeInForceValue": "normal",
        "tradeSide": "open",
        "productType": "umcbl"
    }
    body_json = json.dumps(body)
    signature = sign_request(timestamp, method, path, body_json)
    headers = {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": signature,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": API_PASSPHRASE,
        "Content-Type": "application/json"
    }
    r = requests.post(url, headers=headers, data=body_json)
    if r.status_code == 200:
        send_telegram(f"✅ Открыта позиция: {side} {symbol} на {size} USDT")
    else:
        send_telegram(f"❌ Ошибка при открытии позиции {symbol}: {r.text}")

# === EMA стратегия ===
def calculate_ema(data, period):
    return np.convolve(data, np.ones(period)/period, mode='valid')

def get_candles(symbol):
    url = f"https://api.bitget.com/api/mix/v1/market/history-candles?symbol={symbol}&granularity=1min&limit=100&productType=umcbl"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, headers=headers)
        if r.status_code == 200:
            candles = r.json()["data"]
            if len(candles) < 21:
                send_telegram(f"🚫 Недостаточно данных по {symbol} ({len(candles)} свечей)")
                return None
            return [float(c[4]) for c in candles[::-1]]  # close prices
        else:
            send_telegram(f"❗Ошибка HTTP {r.status_code} для {symbol}")
            return None
    except Exception as e:
        send_telegram(f"❗Ошибка получения свечей {symbol}: {e}")
        return None

# === Основной цикл ===
def strategy_loop():
    last_signal_time = {s: 0 for s in SYMBOLS}
    last_notify = {s: 0 for s in SYMBOLS}
    while True:
        for symbol in SYMBOLS:
            prices = get_candles(symbol)
            if not prices:
                continue

            prices = np.array(prices)
            ema9 = calculate_ema(prices, 9)
            ema21 = calculate_ema(prices, 21)
            if len(ema9) < 1 or len(ema21) < 1:
                continue

            if ema9[-1] > ema21[-1]:
                if time.time() - last_signal_time[symbol] > 3600:
                    place_order(symbol, "open_long", TRADE_AMOUNT)
                    last_signal_time[symbol] = time.time()
            else:
                if time.time() - last_notify[symbol] > 3600:
                    send_telegram(f"📉 По {symbol} сейчас нет сигнала")
                    last_notify[symbol] = time.time()
        time.sleep(30)

# === Flask Keep-alive ===
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Бот работает!"

# === Запуск ===
def start_bot():
    send_telegram("🤖 Бот запущен и работает на Render!")
    threading.Thread(target=strategy_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)

if __name__ == "__main__":
    start_bot()
