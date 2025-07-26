import time
import hmac
import hashlib
import base64
import requests
import json
import threading
from datetime import datetime
from flask import Flask

# ==== КЛЮЧИ KuCoin Futures ====
API_KEY = "687d0016c714e80001eecdbe"
API_SECRET = "d954b08b-7fbd-408e-a117-4e358a8a764d"
API_PASSPHRASE = "Evgeniy@84"

# ==== Telegram ====
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

# ==== ПАРАМЕТРЫ ====
TRADE_AMOUNT = 50
LEVERAGE = 5
SYMBOLS = ["BTCUSDTM", "ETHUSDTM", "SOLUSDTM", "GALAUSDTM", "TRXUSDTM"]
INTERVAL = "1m"
API_URL = "https://api-futures.kucoin.com"

# ==== Flask ====
app = Flask(__name__)

@app.route('/')
def home():
    return 'Futures bot is running on Render!'

# ==== Telegram ====
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text})

# ==== Подпись запроса ====
def sign_request(endpoint, method, params=None, is_private=False):
    now = int(time.time() * 1000)
    str_to_sign = f"{now}{method}{endpoint}"
    if params and method == "GET":
        query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
        endpoint += f"?{query_string}"
        str_to_sign = f"{now}{method}{endpoint}"
    elif params and method == "POST":
        body = json.dumps(params)
        str_to_sign += body
    else:
        body = ""

    signature = base64.b64encode(
        hmac.new(API_SECRET.encode(), str_to_sign.encode(), hashlib.sha256).digest()
    ).decode()

    headers = {
        "KC-API-KEY": API_KEY,
        "KC-API-SIGN": signature,
        "KC-API-TIMESTAMP": str(now),
        "KC-API-PASSPHRASE": base64.b64encode(API_PASSPHRASE.encode()).decode(),
        "KC-API-KEY-VERSION": "2",
        "Content-Type": "application/json"
    }
    return endpoint, headers, body if method == "POST" else None

# ==== Получение EMA ====
def get_ema(symbol, period):
    url = f"https://api-futures.kucoin.com/api/v1/kline/query"
    params = {
        "symbol": symbol,
        "granularity": 60,
        "from": int(time.time()) - 60 * 50,
        "to": int(time.time())
    }
    r = requests.get(url, params=params).json()
    candles = r.get("data", [])
    closes = [float(c[2]) for c in candles][-period:]
    return sum(closes) / period if closes else None

# ==== Открытие позиции ====
def place_order(symbol, side):
    endpoint = "/api/v1/orders"
    price = get_last_price(symbol)
    size = round((TRADE_AMOUNT * LEVERAGE) / price, 4)

    params = {
        "clientOid": str(int(time.time() * 1000)),
        "symbol": symbol,
        "side": side,
        "leverage": str(LEVERAGE),
        "type": "market",
        "size": size
    }

    ep, headers, body = sign_request(endpoint, "POST", params, True)
    res = requests.post(API_URL + ep, headers=headers, data=json.dumps(params)).json()

    send_telegram_message(f"{'🟢 LONG' if side == 'buy' else '🔴 SHORT'} {symbol} на {TRADE_AMOUNT} USD открыт.")
    return res

# ==== Последняя цена ====
def get_last_price(symbol):
    r = requests.get(f"{API_URL}/api/v1/ticker?symbol={symbol}").json()
    return float(r["data"]["price"]) if "data" in r else 0

# ==== Логика торговли ====
def trade_logic():
    send_telegram_message("🤖 Бот запущен на KuCoin Futures")
    while True:
        for symbol in SYMBOLS:
            try:
                ema9 = get_ema(symbol, 9)
                ema21 = get_ema(symbol, 21)
                if not ema9 or not ema21:
                    continue

                price = get_last_price(symbol)
                if ema9 > ema21:
                    place_order(symbol, "buy")
                elif ema9 < ema21:
                    place_order(symbol, "sell")
                time.sleep(3)
            except Exception as e:
                send_telegram_message(f"Ошибка для {symbol}: {e}")
        time.sleep(60)

# ==== Запуск ====
if __name__ == '__main__':
    threading.Thread(target=trade_logic, daemon=True).start()
    app.run(host='0.0.0.0', port=8080)
