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

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    except Exception as e:
        print("Ошибка Telegram:", e)

def get_bitget_candles(symbol):
    url = f"https://api.bitget.com/api/mix/v1/market/history-candles?symbol={symbol}&granularity=1min&limit=100"
    try:
        response = requests.get(url)
        data = response.json()
        if "data" in data and isinstance(data["data"], list):
            candles = data["data"]
            closes = [float(c[4]) for c in candles][::-1]
            return closes
    except Exception as e:
        print("Ошибка при получении свечей:", e)
    return None

def calculate_ema(data, period):
    if len(data) < period:
        return None
    ema = sum(data[:period]) / period
    k = 2 / (period + 1)
    for price in data[period:]:
        ema = price * k + ema * (1 - k)
    return ema

def bitget_signature(timestamp, method, path, body, secret_key):
    message = f"{timestamp}{method}{path}{body}"
    return base64.b64encode(hmac.new(secret_key.encode(), message.encode(), hashlib.sha256).digest()).decode()

def place_bitget_order(symbol, side):
    timestamp = str(int(time.time() * 1000))
    path = "/api/mix/v1/order/placeOrder"
    url = "https://api.bitget.com" + path
    body = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "size": str(TRADE_AMOUNT),
        "side": side,
        "orderType": "market",
        "timeInForceValue": "normal",
        "tradeSide": "open",
        "productType": "umcbl"
    }
    body_json = json.dumps(body)
    sign = bitget_signature(timestamp, "POST", path, body_json, BITGET_API_SECRET)
    headers = {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json"
    }
    try:
        response = requests.post(url, headers=headers, data=body_json)
        res_data = response.json()
        return res_data
    except Exception as e:
        print("Ошибка при размещении ордера:", e)
        return None

def check_and_trade():
    while True:
        for symbol in SYMBOLS:
            closes = get_bitget_candles(symbol)
            if not closes:
                send_telegram_message(f"⚠️ Не удалось получить свечи по {symbol}.")
                continue
            ema9 = calculate_ema(closes, 9)
            ema21 = calculate_ema(closes, 21)
            if not ema9 or not ema21:
                continue
            if ema9 > ema21:
                side = "buy"
                result = place_bitget_order(symbol, side)
                send_telegram_message(f"✅ LONG: {symbol} по стратегии EMA\\nОтвет: {result}")
            elif ema9 < ema21:
                side = "sell"
                result = place_bitget_order(symbol, side)
                send_telegram_message(f"🔻 SHORT: {symbol} по стратегии EMA\\nОтвет: {result}")
            time.sleep(2)
        time.sleep(60)

@app.route("/")
def home():
    return "🤖 Crypto bot is running on Render!"

def start_bot():
    send_telegram_message("🤖 Бот успешно запущен на Render!")
    check_and_trade()

if __name__ == "__main__":
    threading.Thread(target=start_bot).start()
    app.run(host="0.0.0.0", port=10000)
