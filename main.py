import time
import requests
import hmac
import hashlib
import base64
import json
import threading
from flask import Flask

# === НАСТРОЙКИ ===
KUCOIN_API_KEY = "687d0016c714e80001eecdbe"
KUCOIN_API_SECRET = "d954b08b-7fbd-408e-a117-4e358a8a764d"
KUCOIN_API_PASSPHRASE = "Evgeniy@84"

TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

SYMBOLS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "TRX-USDT", "GALA-USDT"]
TRADE_AMOUNT = 100
COOLDOWN = 60 * 60 * 3
TP_PERCENT = 1.5
SL_PERCENT = 1.0

active_trades = {}
last_trade_time = {}

# === FLASK ===
app = Flask(__name__)

@app.route('/')
def home():
    return '🤖 Бот запущен и работает внутри KuCoin'

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    try:
        requests.post(url, data=data)
    except:
        pass

def get_headers(endpoint, method, body=""):
    now = str(int(time.time() * 1000))
    str_to_sign = now + method + endpoint + body
    signature = base64.b64encode(
        hmac.new(KUCOIN_API_SECRET.encode(), str_to_sign.encode(), hashlib.sha256).digest()).decode()
    passphrase = base64.b64encode(
        hmac.new(KUCOIN_API_SECRET.encode(), KUCOIN_API_PASSPHRASE.encode(), hashlib.sha256).digest()).decode()

    return {
        "KC-API-KEY": KUCOIN_API_KEY,
        "KC-API-SIGN": signature,
        "KC-API-TIMESTAMP": now,
        "KC-API-PASSPHRASE": passphrase,
        "KC-API-KEY-VERSION": "2",
        "Content-Type": "application/json"
    }

def get_price(symbol):
    url = f"https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={symbol}"
    try:
        r = requests.get(url)
        return float(r.json()["data"]["price"])
    except:
        return None

def get_klines(symbol):
    url = f"https://api.kucoin.com/api/v1/market/candles?type=1min&symbol={symbol}&limit=50"
    r = requests.get(url)
    data = r.json()["data"]
    closes = [float(c[2]) for c in data[::-1]]
    return closes

def calculate_ema(data, period):
    ema = data[0]
    multiplier = 2 / (period + 1)
    for price in data[1:]:
        ema = (price - ema) * multiplier + ema
    return ema

def place_order(symbol, side, funds):
    url = "https://api.kucoin.com/api/v1/orders"
    endpoint = "/api/v1/orders"
    body = {
        "clientOid": str(int(time.time() * 1000)),
        "side": side,
        "symbol": symbol,
        "type": "market",
        "funds": str(funds) if side == "buy" else None
    }
    body_json = json.dumps({k: v for k, v in body.items() if v is not None})
    headers = get_headers(endpoint, "POST", body_json)
    r = requests.post(url, headers=headers, data=body_json)
    return r.json()

def trader():
    send_telegram("🤖 Бот успешно запущен на Render и готов торговать на KuCoin!")
    while True:
        for symbol in SYMBOLS:
            now = time.time()
            if symbol in last_trade_time and now - last_trade_time[symbol] < COOLDOWN:
                continue

            price = get_price(symbol)
            closes = get_klines(symbol)
            ema21 = calculate_ema(closes, 21)

            if symbol not in active_trades:
                if price < ema21 * 0.985:
                    result = place_order(symbol, "buy", TRADE_AMOUNT)
                    if result.get("code") == "200000":
                        active_trades[symbol] = price
                        last_trade_time[symbol] = now
                        send_telegram(f"🟢 Куплено {symbol} по {price:.4f} USDT (EMA21: {ema21:.4f})")
            else:
                entry = active_trades[symbol]
                if price >= entry * (1 + TP_PERCENT / 100):
                    result = place_order(symbol, "sell", None)
                    send_telegram(f"✅ Продано {symbol} по {price:.4f} | Профит +{TP_PERCENT}%")
                    active_trades.pop(symbol)
                elif price <= entry * (1 - SL_PERCENT / 100):
                    result = place_order(symbol, "sell", None)
                    send_telegram(f"🔴 Продано {symbol} по {price:.4f} | Убыток -{SL_PERCENT}%")
                    active_trades.pop(symbol)
            time.sleep(1)
        time.sleep(15)

if __name__ == "__main__":
    threading.Thread(target=trader, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
