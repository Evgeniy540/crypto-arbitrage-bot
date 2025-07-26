import time
import hmac
import hashlib
import base64
import requests
import json
from flask import Flask
import threading
import logging

# === НАСТРОЙКИ ===
KUCOIN_API_KEY = "687d0016c714e80001eecdbe"
KUCOIN_API_SECRET = "d954b08b-7fbd-408e-a117-4e358a8a764d"
KUCOIN_API_PASSPHRASE = "Evgeniy@84"

BITGET_API_KEY = "b8c00194-cd2e-4196-9442-538774c5d228"
BITGET_API_SECRET = "0b2aa92e-8e69-4f87-b8c9-0b3b36e587a7"

TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

TRADE_AMOUNT = 100
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "TRX/USDT", "GALA/USDT"]
COOLDOWN = 60 * 60 * 3
ARBITRAGE_THRESHOLD = 0.35

last_trade_time = {}

# === Flask Keep-Alive ===
app = Flask(__name__)

@app.route("/")
def home():
    return "🤖 Crypto Arbitrage Bot is running!"

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print("Telegram error:", e)

@app.route("/status")
def status():
    return json.dumps(last_trade_time, indent=2)

def get_kucoin_headers(endpoint, method="GET"):
    now = int(time.time() * 1000)
    str_to_sign = str(now) + method + endpoint
    signature = base64.b64encode(
        hmac.new(KUCOIN_API_SECRET.encode('utf-8'), str_to_sign.encode('utf-8'), hashlib.sha256).digest()
    )
    passphrase = base64.b64encode(
        hmac.new(KUCOIN_API_SECRET.encode('utf-8'), KUCOIN_API_PASSPHRASE.encode('utf-8'), hashlib.sha256).digest()
    )
    return {
        "KC-API-KEY": KUCOIN_API_KEY,
        "KC-API-SIGN": signature.decode(),
        "KC-API-TIMESTAMP": str(now),
        "KC-API-PASSPHRASE": passphrase.decode(),
        "KC-API-KEY-VERSION": "2",
        "Content-Type": "application/json"
    }

def get_kucoin_price(symbol):
    pair = symbol.replace("/", "-")
    url = f"https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={pair}"
    try:
        r = requests.get(url)
        return float(r.json()["data"]["price"])
    except:
        return None

def get_bitget_price(symbol):
    pair = symbol.replace("/", "").lower() + "_spbl"
    url = f"https://api.bitget.com/api/spot/v1/market/ticker?symbol={pair}"
    try:
        r = requests.get(url)
        return float(r.json()["data"]["close"])
    except:
        return None

def place_kucoin_order(symbol, side, size):
    url = "https://api.kucoin.com/api/v1/orders"
    endpoint = "/api/v1/orders"
    data = {
        "symbol": symbol.replace("/", "-"),
        "side": side,
        "type": "market",
        "size": size
    }
    headers = get_kucoin_headers(endpoint, "POST")
    try:
        r = requests.post(url, headers=headers, json=data)
        return r.json()
    except Exception as e:
        print("Order Error:", e)
        return None

def check_opportunity():
    for symbol in SYMBOLS:
        now = time.time()
        if symbol in last_trade_time and now - last_trade_time[symbol] < COOLDOWN:
            continue

        kucoin_price = get_kucoin_price(symbol)
        bitget_price = get_bitget_price(symbol)

        if not kucoin_price or not bitget_price:
            continue

        diff = ((bitget_price - kucoin_price) / kucoin_price) * 100
        if diff >= ARBITRAGE_THRESHOLD:
            usdt_amount = TRADE_AMOUNT
            qty = round(usdt_amount / kucoin_price, 6)
            result = place_kucoin_order(symbol, "buy", qty)
            send_telegram(f"✅ Куплено {qty} {symbol.split('/')[0]} на KuCoin по {kucoin_price}$, разница: {diff:.2f}%")
            last_trade_time[symbol] = now
        else:
            print(f"{symbol}: нет возможности ({diff:.2f}%)")

def run_bot():
    while True:
        try:
            check_opportunity()
        except Exception as e:
            print("Ошибка в боте:", e)
        time.sleep(60)

threading.Thread(target=run_bot, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
