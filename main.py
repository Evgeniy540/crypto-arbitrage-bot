import os
import json
import time
import hmac
import hashlib
import base64
import requests
import threading
from flask import Flask
from datetime import datetime
import logging
import schedule

# === Настройки ===
BITGET_API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"
TRADE_AMOUNT = 5
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT", "PEPEUSDT", "BGBUSDT"]
TP_PERCENT = 1.5
SL_PERCENT = 1.0

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

def send_telegram_message(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    except Exception as e:
        print("Ошибка Telegram:", e)

def get_timestamp():
    return str(int(time.time() * 1000))

def sign_request(api_secret, method, url, timestamp, body=""):
    message = timestamp + method + url + body
    signature = hmac.new(api_secret.encode('utf-8'), message.encode('utf-8'), hashlib.sha256).digest()
    return base64.b64encode(signature).decode()

def get_candles(symbol):
    try:
        url = f"https://api.bitget.com/api/spot/v1/market/candles?symbol={symbol}&period=1m&limit=50"
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
        data = response.json()
        if "data" not in data:
            return []
        return [float(c[4]) for c in reversed(data["data"])]
    except Exception as e:
        print("Ошибка получения свечей:", e)
        return []

def calculate_ema(data, period):
    if len(data) < period:
        return None
    ema = sum(data[:period]) / period
    multiplier = 2 / (period + 1)
    for price in data[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def get_balance():
    url = "/api/spot/v1/account/assets"
    timestamp = get_timestamp()
    sign = sign_request(BITGET_API_SECRET, "GET", url, timestamp)
    headers = {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json"
    }
    try:
        r = requests.get("https://api.bitget.com" + url, headers=headers)
        assets = r.json().get("data", [])
        usdt = next((item for item in assets if item["coin"] == "USDT"), None)
        return float(usdt["available"]) if usdt else 0
    except Exception as e:
        print("Ошибка баланса:", e)
        return 0

def place_order(symbol, side, size):
    url = "/api/spot/v1/trade/orders"
    timestamp = get_timestamp()
    body = json.dumps({
        "symbol": symbol,
        "side": side,
        "orderType": "market",
        "force": "gtc",
        "size": str(size)
    })
    sign = sign_request(BITGET_API_SECRET, "POST", url, timestamp, body)
    headers = {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json"
    }
    try:
        r = requests.post("https://api.bitget.com" + url, headers=headers, data=body)
        return r.json()
    except Exception as e:
        print("Ошибка ордера:", e)
        return None

def check_signals():
    for symbol in SYMBOLS:
        candles = get_candles(symbol)
        if len(candles) < 21:
            send_telegram_message(f"📉 Недостаточно данных для {symbol}")
            continue
        ema9 = calculate_ema(candles, 9)
        ema21 = calculate_ema(candles, 21)
        if ema9 and ema21 and ema9 > ema21:
            balance = get_balance()
            if balance < TRADE_AMOUNT:
                send_telegram_message(f"💸 Недостаточно USDT ({balance}) для покупки {symbol}")
                return
            qty = round(TRADE_AMOUNT / candles[-1], 4)
            order = place_order(symbol, "buy", qty)
            send_telegram_message(f"🟢 Куплено {symbol} на {TRADE_AMOUNT} USDT")
            monitor_trade(symbol, candles[-1], qty)
            return
        else:
            print(f"Нет сигнала для {symbol}")

def monitor_trade(symbol, entry_price, qty):
    while True:
        candles = get_candles(symbol)
        if not candles:
            time.sleep(30)
            continue
        last_price = candles[-1]
        profit_percent = ((last_price - entry_price) / entry_price) * 100
        if profit_percent >= TP_PERCENT:
            place_order(symbol, "sell", qty)
            send_telegram_message(f"✅ Продано {symbol} по {last_price} (TP), прибыль: {profit_percent:.2f}%")
            break
        elif profit_percent <= -SL_PERCENT:
            place_order(symbol, "sell", qty)
            send_telegram_message(f"❌ Продано {symbol} по {last_price} (SL), убыток: {profit_percent:.2f}%")
            break
        time.sleep(30)

def run_bot():
    send_telegram_message("🤖 Бот запущен на Render!")
    while True:
        try:
            check_signals()
            time.sleep(60)
        except Exception as e:
            send_telegram_message(f"⚠️ Ошибка: {e}")
            time.sleep(60)

@app.route("/")
def index():
    return "Бот работает"

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
