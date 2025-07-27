import time
import hmac
import hashlib
import base64
import requests
import json
from flask import Flask
import threading

# === НАСТРОЙКИ ===
BITGET_API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"

TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"
TRADE_AMOUNT = 10
SYMBOLS = ["BTCUSDT_UMCBL", "ETHUSDT_UMCBL", "SOLUSDT_UMCBL", "XRPUSDT_UMCBL", "TRXUSDT_UMCBL"]

app = Flask(__name__)

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print(f"Ошибка Telegram: {e}")

def get_bitget_candles(symbol):
    try:
        url = f"https://api.bitget.com/api/mix/v1/market/history-candles"
        params = {
            "symbol": symbol,
            "granularity": "1min",
            "limit": "100"
        }
        response = requests.get(url, params=params)
        data = response.json()
        return data["data"] if "data" in data else None
    except Exception as e:
        print(f"Ошибка свечей Bitget: {e}")
        return None

def generate_signature(timestamp, method, request_path, body=""):
    prehash = f"{timestamp}{method}{request_path}{body}"
    signature = hmac.new(BITGET_API_SECRET.encode(), prehash.encode(), hashlib.sha256).hexdigest()
    return signature

def place_order(symbol, side):
    timestamp = str(int(time.time() * 1000))
    url = "/api/mix/v1/order/placeOrder"
    method = "POST"
    body_dict = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "size": str(TRADE_AMOUNT),
        "side": side,
        "orderType": "market",
        "tradeSide": "long" if side == "buy" else "short",
        "productType": "umcbl"
    }
    body = json.dumps(body_dict)
    sign = generate_signature(timestamp, method, url, body)
    headers = {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json"
    }
    full_url = "https://api.bitget.com" + url
    response = requests.post(full_url, headers=headers, data=body)
    try:
        result = response.json()
        send_telegram_message(f"✅ Ордер {side.upper()} {symbol}: {result}")
    except:
        send_telegram_message(f"⚠️ Ошибка ордера {side.upper()} {symbol}: {response.text}")

def ema(values, period):
    weights = []
    alpha = 2 / (period + 1)
    for i in range(len(values)):
        weights.append((1 - alpha) ** i)
    weights = list(reversed(weights[-period:]))
    if len(values) < period:
        return None
    weighted_values = [v * w for v, w in zip(values[-period:], weights)]
    return sum(weighted_values) / sum(weights)

def check_signal(symbol):
    candles = get_bitget_candles(symbol)
    if not candles:
        send_telegram_message(f"⚠️ Не удалось получить свечи по {symbol}.")
        return

    try:
        close_prices = [float(c[4]) for c in candles[::-1]]
        ema9 = ema(close_prices, 9)
        ema21 = ema(close_prices, 21)

        if ema9 is None or ema21 is None:
            return

        if ema9 > ema21:
            place_order(symbol, "buy")
        elif ema9 < ema21:
            place_order(symbol, "sell")
    except Exception as e:
        send_telegram_message(f"⚠️ Ошибка анализа {symbol}: {e}")

def start_bot():
    send_telegram_message("🤖 Бот успешно запущен на Render!")
    while True:
        for symbol in SYMBOLS:
            check_signal(symbol)
            time.sleep(5)
        time.sleep(30)

@app.route('/')
def index():
    return "🚀 Crypto bot is running on Render!"

if __name__ == '__main__':
    t = threading.Thread(target=start_bot)
    t.daemon = True
    t.start()
    app.run(host="0.0.0.0", port=10000)
