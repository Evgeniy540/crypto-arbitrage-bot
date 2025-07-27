import requests
import time
import hmac
import hashlib
import base64
import json
from datetime import datetime
from flask import Flask
import threading

# === КЛЮЧИ ===
BITGET_API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

TRADE_AMOUNT = 10
SYMBOLS = ["BTCUSDT_UMCBL", "ETHUSDT_UMCBL", "SOLUSDT_UMCBL", "XRPUSDT_UMCBL", "TRXUSDT_UMCBL"]

app = Flask(__name__)

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print("Ошибка отправки сообщения в Telegram:", e)

def get_timestamp():
    return str(int(time.time() * 1000))

def sign_request(timestamp, method, endpoint, params):
    query = f"{timestamp}{method}{endpoint}{params}"
    signature = hmac.new(BITGET_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    return signature

def get_candles(symbol):
    try:
        url = f"https://api.bitget.com/api/mix/v1/market/history-candles"
        params = f"symbol={symbol}&granularity=1min&limit=100"
        response = requests.get(f"{url}?{params}")
        data = response.json()
        if "data" in data and isinstance(data["data"], list):
            return list(reversed(data["data"]))
        else:
            return None
    except:
        return None

def place_order(symbol, side):
    timestamp = get_timestamp()
    endpoint = "/api/mix/v1/order/placeOrder"
    method = "POST"
    margin_coin = "USDT"
    size = str(TRADE_AMOUNT)
    body = {
        "symbol": symbol.replace("_UMCBL", ""),
        "marginCoin": margin_coin,
        "size": size,
        "side": side,
        "orderType": "market",
        "tradeSide": "open",
        "productType": "umcbl"
    }
    body_json = json.dumps(body, separators=(",", ":"))
    sign = sign_request(timestamp, method, endpoint, body_json)
    headers = {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json"
    }
    url = f"https://api.bitget.com{endpoint}"
    response = requests.post(url, headers=headers, data=body_json)
    return response.json()

def strategy_loop():
    send_telegram_message("🤖 Бот запущен и работает на Render!")
    while True:
        for symbol in SYMBOLS:
            candles = get_candles(symbol)
            if not candles:
                send_telegram_message(f"⚠️ Не удалось получить свечи по {symbol}")
                continue

            closes = [float(c[4]) for c in candles]
            ema9 = sum(closes[-9:]) / 9
            ema21 = sum(closes[-21:]) / 21

            if ema9 > ema21:
                response = place_order(symbol, "buy")
                send_telegram_message(f"🟢 КУПИТЬ {symbol}
Ответ: {response}")
            elif ema9 < ema21:
                response = place_order(symbol, "sell")
                send_telegram_message(f"🔴 ПРОДАТЬ {symbol}
Ответ: {response}")

        time.sleep(60)

@app.route("/")
def home():
    return "Bot is running!"

if __name__ == "__main__":
    threading.Thread(target=strategy_loop).start()
    app.run(host="0.0.0.0", port=10000)
