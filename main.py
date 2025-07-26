import time
import requests
import hmac
import base64
import hashlib
import json
import threading
from flask import Flask
from datetime import datetime
import numpy as np
import logging

# === НАСТРОЙКИ ===
KUCOIN_API_KEY = "687d0016c714e80001eecdbe"
KUCOIN_API_SECRET = "d954b08b-7fbd-408e-a117-4e358a8a764d"
KUCOIN_API_PASSPHRASE = "Evgeniy@84"
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"
TRADE_AMOUNT = 50
SYMBOLS = ["BTCUSDTM", "ETHUSDTM", "SOLUSDTM", "GALAUSDTM", "TRXUSDTM"]
LEVERAGE = 5
API_URL = "https://api-futures.kucoin.com"

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    except:
        pass

def get_kucoin_signature(endpoint, method, body=""):
    now = int(time.time() * 1000)
    str_to_sign = f"{now}{method}{endpoint}{body}"
    signature = base64.b64encode(hmac.new(KUCOIN_API_SECRET.encode(), str_to_sign.encode(), hashlib.sha256).digest())
    passphrase = base64.b64encode(hmac.new(KUCOIN_API_SECRET.encode(), KUCOIN_API_PASSPHRASE.encode(), hashlib.sha256).digest())
    return {
        "KC-API-KEY": KUCOIN_API_KEY,
        "KC-API-SIGN": signature.decode(),
        "KC-API-TIMESTAMP": str(now),
        "KC-API-KEY-VERSION": "2",
        "KC-API-PASSPHRASE": passphrase.decode(),
        "Content-Type": "application/json"
    }

def get_klines(symbol):
    url = f"{API_URL}/api/v1/kline/query?symbol={symbol}&granularity=5"
    try:
        res = requests.get(url)
        data = res.json().get("data", [])
        closes = [float(c[2]) for c in data][-21:]
        return closes
    except:
        return []

def calculate_ema(closes, period):
    if len(closes) < period:
        return None
    return np.round(np.convolve(closes, np.exp(np.linspace(-1., 0., period)), mode='valid'), 2)[-1]

def get_position(symbol):
    url = f"{API_URL}/api/v1/position?symbol={symbol}"
    headers = get_kucoin_signature("/api/v1/position", "GET")
    res = requests.get(url, headers=headers)
    try:
        data = res.json()["data"]
        return float(data["currentQty"]), data["currentSide"]
    except:
        return 0, ""

def place_order(symbol, side):
    endpoint = "/api/v1/orders"
    url = f"{API_URL}{endpoint}"
    headers = get_kucoin_signature(endpoint, "POST")
    order = {
        "clientOid": str(int(time.time() * 1000)),
        "symbol": symbol,
        "side": side,
        "leverage": str(LEVERAGE),
        "type": "market",
        "size": str(TRADE_AMOUNT),
        "marginType": "isolated"
    }
    res = requests.post(url, headers=headers, data=json.dumps(order))
    if res.status_code == 200:
        send_telegram(f"📈 Открыта позиция {side.upper()} на {symbol}")
    else:
        send_telegram(f"⚠️ Ошибка при открытии {side} на {symbol}:\n{res.text}")

def trade_loop():
    send_telegram("🤖 Бот запущен на KuCoin Futures")
    while True:
        for symbol in SYMBOLS:
            closes = get_klines(symbol)
            if len(closes) < 21:
                continue
            ema9 = np.mean(closes[-9:])
            ema21 = np.mean(closes[-21:])
            qty, side = get_position(symbol)

            if ema9 > ema21 and qty == 0:
                place_order(symbol, "buy")
            elif ema9 < ema21 and qty == 0:
                place_order(symbol, "sell")
            time.sleep(1)
        time.sleep(60)

@app.route('/')
def home():
    return "KuCoin Futures Bot is running!"

if __name__ == '__main__':
    threading.Thread(target=trade_loop).start()
    app.run(host="0.0.0.0", port=10000)
