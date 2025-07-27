import time
import hmac
import hashlib
import requests
import base64
import json
import threading
from flask import Flask
import logging

# ==== НАСТРОЙКИ ====
BITGET_API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"

TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

TRADE_AMOUNT = 10
SYMBOLS = ["BTCUSDT_UMCBL", "ETHUSDT_UMCBL", "SOLUSDT_UMCBL", "XRPUSDT_UMCBL", "TRXUSDT_UMCBL"]

HEADERS = {
    "ACCESS-KEY": BITGET_API_KEY,
    "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
    "Content-Type": "application/json"
}

app = Flask(__name__)

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})
    except:
        pass

def get_server_time():
    try:
        r = requests.get("https://api.bitget.com/api/mix/v1/market/time")
        return str(r.json()["data"])
    except:
        return str(int(time.time() * 1000))

def sign_request(timestamp, method, request_path, body=""):
    message = f"{timestamp}{method}{request_path}{body}"
    signature = hmac.new(BITGET_API_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    return signature

def get_candles(symbol):
    url = f"https://api.bitget.com/api/mix/v1/market/history-candles"
    params = {
        "symbol": symbol.replace("_UMCBL", ""),
        "granularity": "1min",
        "limit": "100",
        "productType": "umcbl"
    }
    try:
        response = requests.get(url, params=params)
        data = response.json()
        if "data" not in data or not data["data"]:
            raise ValueError
        candles = list(reversed(data["data"]))  # от старых к новым
        closes = [float(c[4]) for c in candles]
        return closes
    except:
        send_telegram_message(f"⚠️ Не удалось получить свечи по {symbol}.")
        return None

def calculate_ema(data, period):
    if len(data) < period:
        return None
    ema = sum(data[:period]) / period
    multiplier = 2 / (period + 1)
    for price in data[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def place_order(symbol, side):
    timestamp = get_server_time()
    endpoint = "/api/mix/v1/order/placeOrder"
    url = f"https://api.bitget.com{endpoint}"

    body = {
        "symbol": symbol.replace("_UMCBL", ""),
        "marginCoin": "USDT",
        "size": str(TRADE_AMOUNT),
        "side": side,
        "orderType": "market",
        "productType": "umcbl"
    }

    body_json = json.dumps(body)
    signature = sign_request(timestamp, "POST", endpoint, body_json)

    headers = {
        **HEADERS,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-SIGN": signature
    }

    try:
        r = requests.post(url, headers=headers, data=body_json)
        res = r.json()
        if res.get("code") == "00000":
            send_telegram_message(f"✅ Открыта позиция {side.upper()} по {symbol}")
        else:
            send_telegram_message(f"❌ Ошибка при открытии позиции {side.upper()} по {symbol}: {res}")
    except Exception as e:
        send_telegram_message(f"❌ Ошибка при запросе ордера по {symbol}: {e}")

def analyze_and_trade():
    while True:
        for symbol in SYMBOLS:
            closes = get_candles(symbol)
            if not closes:
                continue
            ema9 = calculate_ema(closes, 9)
            ema21 = calculate_ema(closes, 21)
            if not ema9 or not ema21:
                continue
            if ema9 > ema21:
                place_order(symbol, "open_long")
            elif ema9 < ema21:
                place_order(symbol, "open_short")
        time.sleep(60)

@app.route("/")
def home():
    return "✅ Бот работает!"

def start_trading():
    send_telegram_message("🤖 Бот успешно запущен на Render!")
    analyze_and_trade()

threading.Thread(target=start_trading).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
