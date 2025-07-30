import time
import hmac
import hashlib
import base64
import requests
import json
import threading
import logging
from flask import Flask
import os
from datetime import datetime

# === НАСТРОЙКИ ===
API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
API_PASSPHRASE = "Evgeniy84"
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"
TRADE_AMOUNT = 5
TP_PERCENT = 1.5
SL_PERCENT = 1.0
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT", "PEPEUSDT", "BGBUSDT"]
BASE_URL = "https://api.bitget.com"

app = Flask(__name__)

profit_log = []
position = {}
profit_file = "profit.json"
position_file = "position.json"

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        requests.post(url, data=data)
    except:
        pass

def sign_request(timestamp, method, request_path, body=""):
    message = f"{timestamp}{method}{request_path}{body}"
    hmac_key = hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha256)
    sign = base64.b64encode(hmac_key.digest()).decode()
    return sign

def get_headers(method, path, body=""):
    timestamp = str(int(time.time() * 1000))
    sign = sign_request(timestamp, method, path, body)
    return {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": API_PASSPHRASE,
        "Content-Type": "application/json"
    }

def get_candles(symbol):
    try:
        params = {"symbol": f"{symbol}_SPBL", "granularity": "1m", "limit": 100}
        response = requests.get(BASE_URL + "/api/spot/v1/market/candles", params=params)
        data = response.json()
        if "data" in data:
            candles = [float(x[4]) for x in data["data"]]
            return candles[::-1]
    except:
        return []

def calculate_ema(prices, period):
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = prices[0]
    for price in prices[1:]:
        ema = price * k + ema * (1 - k)
    return ema

def place_order(symbol, side, price=None):
    path = "/api/spot/v1/trade/orders"
    method = "POST"
    body = {
        "symbol": f"{symbol}_SPBL",
        "side": side,
        "orderType": "market",
        "size": str(TRADE_AMOUNT)
    }
    body_json = json.dumps(body)
    headers = get_headers(method, path, body_json)
    response = requests.post(BASE_URL + path, headers=headers, data=body_json)
    return response.json()

def check_signals():
    global TRADE_AMOUNT
    while True:
        if position:
            time.sleep(30)
            continue

        for symbol in SYMBOLS:
            prices = get_candles(symbol)
            if len(prices) < 21:
                continue
            ema9 = calculate_ema(prices[-9:], 9)
            ema21 = calculate_ema(prices[-21:], 21)
            if ema9 > ema21:
                result = place_order(symbol, "buy")
                if result.get("code") == "00000":
                    entry = float(prices[-1])
                    tp = entry * (1 + TP_PERCENT / 100)
                    sl = entry * (1 - SL_PERCENT / 100)
                    position.update({"symbol": symbol, "entry": entry, "tp": tp, "sl": sl})
                    with open(position_file, "w") as f:
                        json.dump(position, f)
                    send_telegram_message(f"✅ Куплено {symbol} по {entry:.4f}")
                break
        time.sleep(30)

def monitor_position():
    global TRADE_AMOUNT
    while True:
        if position:
            symbol = position["symbol"]
            prices = get_candles(symbol)
            if not prices:
                time.sleep(30)
                continue
            price = prices[-1]
            if price >= position["tp"]:
                result = place_order(symbol, "sell")
                if result.get("code") == "00000":
                    profit = (price - position["entry"]) * TRADE_AMOUNT
                    TRADE_AMOUNT += profit
                    profit_log.append({"symbol": symbol, "profit": profit})
                    with open(profit_file, "w") as f:
                        json.dump(profit_log, f)
                    send_telegram_message(f"📈 Продано {symbol} по {price:.4f}, профит +{profit:.2f} USDT")
                    position.clear()
                    with open(position_file, "w") as f:
                        json.dump(position, f)
            elif price <= position["sl"]:
                result = place_order(symbol, "sell")
                if result.get("code") == "00000":
                    loss = (price - position["entry"]) * TRADE_AMOUNT
                    profit_log.append({"symbol": symbol, "profit": loss})
                    with open(profit_file, "w") as f:
                        json.dump(profit_log, f)
                    send_telegram_message(f"📉 Продано {symbol} по {price:.4f}, убыток {loss:.2f} USDT")
                    position.clear()
                    with open(position_file, "w") as f:
                        json.dump(position, f)
        time.sleep(30)

@app.route("/")
def home():
    return "Bitget Spot Bot Running!"

@app.route("/profit")
def get_profit():
    if os.path.exists(profit_file):
        with open(profit_file) as f:
            data = json.load(f)
            total = sum(x["profit"] for x in data)
            return {"total_profit": total, "details": data}
    return {"message": "Нет данных о прибыли."}

def run_bot():
    send_telegram_message("🤖 Бот успешно запущен на Render!")
    threading.Thread(target=check_signals).start()
    threading.Thread(target=monitor_position).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

if __name__ == "__main__":
    run_bot()
