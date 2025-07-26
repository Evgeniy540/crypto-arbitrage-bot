import time
import hmac
import hashlib
import base64
import requests
import json
from flask import Flask
import threading
import logging
import math

# === НАСТРОЙКИ ===
API_KEY = "687d0016c714e80001eecdbe"
API_SECRET = "d954b08b-7fbd-408e-a117-4e358a8a764d"
API_PASSPHRASE = "Evgeniy@84"
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"
TRADE_AMOUNT = 50
LEVERAGE = 5
TP_PERCENT = 2.5
SL_PERCENT = 1.5

SYMBOLS = ["BTCUSDTM", "ETHUSDTM", "SOLUSDTM", "TRXUSDTM", "GALAUSDTM"]
INTERVAL = 60  # Проверка каждые 60 секунд
last_positions = {}

# === KuCoin Futures ===
BASE_URL = "https://api-futures.kucoin.com"

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=payload)
    except:
        pass

def get_headers(endpoint, method="GET", body=""):
    now = int(time.time() * 1000)
    str_to_sign = f"{now}{method}{endpoint}{body}"
    signature = base64.b64encode(
        hmac.new(API_SECRET.encode('utf-8'), str_to_sign.encode('utf-8'), hashlib.sha256).digest()
    )
    passphrase = base64.b64encode(
        hmac.new(API_SECRET.encode('utf-8'), API_PASSPHRASE.encode('utf-8'), hashlib.sha256).digest()
    )
    return {
        "KC-API-SIGN": signature,
        "KC-API-TIMESTAMP": str(now),
        "KC-API-KEY": API_KEY,
        "KC-API-PASSPHRASE": passphrase,
        "KC-API-KEY-VERSION": "2",
        "Content-Type": "application/json"
    }

def get_klines(symbol):
    url = f"{BASE_URL}/api/v1/kline/query?symbol={symbol}&granularity=1"
    try:
        res = requests.get(url)
        data = res.json()["data"]
        closes = [float(k[2]) for k in data[-21:]]  # close price
        return closes
    except:
        return []

def calculate_ema(data, period):
    k = 2 / (period + 1)
    ema = data[0]
    for price in data[1:]:
        ema = price * k + ema * (1 - k)
    return ema

def get_balance():
    endpoint = "/api/v1/account-overview?currency=USDT"
    headers = get_headers(endpoint)
    url = BASE_URL + endpoint
    try:
        res = requests.get(url, headers=headers)
        return float(res.json()["data"]["availableBalance"])
    except:
        return 0

def place_order(symbol, side, size):
    endpoint = "/api/v1/orders"
    url = BASE_URL + endpoint
    body = {
        "symbol": symbol,
        "side": side,
        "leverage": LEVERAGE,
        "type": "market",
        "size": size
    }
    body_json = json.dumps(body)
    headers = get_headers(endpoint, method="POST", body=body_json)
    try:
        r = requests.post(url, headers=headers, data=body_json)
        return r.json()
    except:
        return None

def get_price(symbol):
    url = f"{BASE_URL}/api/v1/mark-price/{symbol}"
    try:
        res = requests.get(url).json()
        return float(res['data']['value'])
    except:
        return 0

def trade():
    while True:
        for symbol in SYMBOLS:
            closes = get_klines(symbol)
            if len(closes) < 21:
                continue
            ema9 = calculate_ema(closes[-9:], 9)
            ema21 = calculate_ema(closes[-21:], 21)
            price = get_price(symbol)

            position = last_positions.get(symbol)

            # Условия на вход
            if not position:
                if ema9 > ema21:
                    direction = "buy"
                elif ema9 < ema21:
                    direction = "sell"
                else:
                    continue

                usdt_balance = get_balance()
                if usdt_balance < TRADE_AMOUNT:
                    continue

                size = round((TRADE_AMOUNT * LEVERAGE) / price, 4)
                result = place_order(symbol, direction, size)
                if result and result.get("code") == "200000":
                    last_positions[symbol] = direction
                    send_telegram(f"✅ Открыт {direction.upper()} по {symbol} на {TRADE_AMOUNT} USDT (цена: {price})")
            else:
                # Take Profit / Stop Loss логика будет добавлена при желании
                pass
        time.sleep(INTERVAL)

# === Flask keep-alive ===
app = Flask(__name__)

@app.route("/")
def home():
    return "KuCoin Futures бот активен."

def run():
    app.run(host='0.0.0.0', port=8080)

if __name__ == "__main__":
    send_telegram("🤖 Бот запущен на KuCoin Futures")
    threading.Thread(target=run).start()
    trade()
