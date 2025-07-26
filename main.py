import time
import requests
from flask import Flask
import threading
import hmac
import hashlib
import base64
import json

# ========== НАСТРОЙКИ ==========
KUCOIN_API_KEY = "687d0016c714e80001eecdbe"
KUCOIN_API_SECRET = "d954b08b-7fbd-408e-a117-4e358a8a764d"
KUCOIN_API_PASSPHRASE = "Evgeniy@84"

TRADE_SYMBOL = "TRX-USDT"
TRADE_AMOUNT = 10  # USDT
CHECK_INTERVAL = 30  # сек между проверками
TP_PERCENT = 1.2
SL_PERCENT = 0.9

app = Flask(__name__)
last_price = None
position_opened = False
entry_price = 0

# ========== KuCoin API ==========
def kucoin_headers(endpoint, method="GET", body=""):
    now = int(time.time() * 1000)
    str_to_sign = f"{now}{method}{endpoint}{body}"
    signature = base64.b64encode(hmac.new(KUCOIN_API_SECRET.encode(), str_to_sign.encode(), hashlib.sha256).digest())
    passphrase = base64.b64encode(hmac.new(KUCOIN_API_SECRET.encode(), KUCOIN_API_PASSPHRASE.encode(), hashlib.sha256).digest())

    return {
        "KC-API-KEY": KUCOIN_API_KEY,
        "KC-API-SIGN": signature.decode(),
        "KC-API-TIMESTAMP": str(now),
        "KC-API-PASSPHRASE": passphrase.decode(),
        "KC-API-KEY-VERSION": "2",
        "Content-Type": "application/json"
    }

def get_kucoin_price():
    url = f"https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={TRADE_SYMBOL}"
    r = requests.get(url)
    return float(r.json()['data']['price'])

def place_market_order(side):
    url = "https://api.kucoin.com/api/v1/orders"
    body = {
        "clientOid": str(int(time.time() * 1000)),
        "side": side,
        "symbol": TRADE_SYMBOL,
        "type": "market",
        "funds": str(TRADE_AMOUNT) if side == "buy" else None,
        "size": None  # для продажи размер автоопределяется по балансу
    }
    body_str = json.dumps({k: v for k, v in body.items() if v is not None})
    headers = kucoin_headers("/api/v1/orders", method="POST", body=body_str)
    res = requests.post(url, headers=headers, data=body_str)
    print(f"[{side.upper()}] {res.json()}")
    return res.json()

# ========== ТРЕЙДЕР ==========
def trader():
    global last_price, position_opened, entry_price

    while True:
        try:
            current_price = get_kucoin_price()
            print(f"[Цена] {TRADE_SYMBOL}: {current_price} USDT")

            if not position_opened:
                if last_price and current_price < last_price * 0.995:
                    place_market_order("buy")
                    entry_price = current_price
                    position_opened = True
                    print(f"[ПОКУПКА] Цена входа: {entry_price}")
            else:
                if current_price >= entry_price * (1 + TP_PERCENT / 100):
                    print("[TP] Продаю")
                    place_market_order("sell")
                    position_opened = False
                elif current_price <= entry_price * (1 - SL_PERCENT / 100):
                    print("[SL] Продаю")
                    place_market_order("sell")
                    position_opened = False

            last_price = current_price
        except Exception as e:
            print("[ОШИБКА]", str(e))

        time.sleep(CHECK_INTERVAL)

# ========== Flask для Render ==========
@app.route("/")
def index():
    return "✅ Крипто-бот работает!"

if __name__ == "__main__":
    threading.Thread(target=trader).start()
    app.run(host="0.0.0.0", port=10000)
