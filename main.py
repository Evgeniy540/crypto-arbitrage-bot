import time
import hmac
import hashlib
import base64
import requests
import json
import threading
from datetime import datetime
from flask import Flask
import logging

# === НАСТРОЙКИ ===
KUCOIN_API_KEY = "687d0016c714e80001eecdbe"
KUCOIN_API_SECRET = "d954b08b-7fbd-408e-a117-4e358a8a764d"
KUCOIN_API_PASSPHRASE = "Evgeniy@84"
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"
TRADE_AMOUNT = 50  # USDT
SYMBOLS = ["BTCUSDTM", "ETHUSDTM", "SOLUSDTM", "GALAUSDTM", "TRXUSDTM"]

# === Telegram уведомление ===
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})

# === Подпись KuCoin запроса ===
def sign_request(method, endpoint, body=""):
    now = int(time.time() * 1000)
    str_to_sign = f"{now}{method}{endpoint}{body}"
    signature = base64.b64encode(
        hmac.new(KUCOIN_API_SECRET.encode(), str_to_sign.encode(), hashlib.sha256).digest()
    ).decode()
    passphrase = base64.b64encode(
        hmac.new(KUCOIN_API_SECRET.encode(), KUCOIN_API_PASSPHRASE.encode(), hashlib.sha256).digest()
    ).decode()
    headers = {
        "KC-API-KEY": KUCOIN_API_KEY,
        "KC-API-SIGN": signature,
        "KC-API-TIMESTAMP": str(now),
        "KC-API-PASSPHRASE": passphrase,
        "KC-API-KEY-VERSION": "2",
        "Content-Type": "application/json"
    }
    return headers

# === Получение свечей для стратегии ===
def get_klines(symbol):
    url = f"https://api-futures.kucoin.com/api/v1/kline/query?symbol={symbol}&granularity=5"
    try:
        res = requests.get(url)
        data = res.json()["data"]
        closes = [float(i[2]) for i in data[-30:]]  # последние 30 свечей
        return closes
    except Exception as e:
        print("Ошибка получения свечей:", e)
        return []

# === EMA ===
def ema(data, period):
    alpha = 2 / (period + 1)
    ema_val = data[0]
    for price in data[1:]:
        ema_val = alpha * price + (1 - alpha) * ema_val
    return ema_val

# === Отправка ордера на фьючерсы ===
def place_futures_order(symbol, side, size):
    endpoint = "/api/v1/orders"
    url = "https://api-futures.kucoin.com" + endpoint
    body = json.dumps({
        "symbol": symbol,
        "side": side,
        "leverage": 5,
        "type": "market",
        "size": size
    })
    headers = sign_request("POST", endpoint, body)
    response = requests.post(url, headers=headers, data=body)
    if response.status_code == 200:
        send_telegram(f"✅ {side.upper()} {symbol} на {size} USD открыт.")
    else:
        send_telegram(f"❌ Ошибка при открытии ордера {symbol}: {response.text}")

# === Получение цены — последний trade ===
def get_price(symbol):
    url = f"https://api-futures.kucoin.com/api/v1/ticker?symbol={symbol}"
    try:
        res = requests.get(url)
        return float(res.json()['data']['price'])
    except:
        return None

# === Основная логика торговли ===
def trade():
    while True:
        for symbol in SYMBOLS:
            try:
                candles = get_klines(symbol)
                if not candles or len(candles) < 21:
                    continue

                ema9 = ema(candles[-9:], 9)
                ema21 = ema(candles[-21:], 21)
                price = get_price(symbol)
                size = round(TRADE_AMOUNT / price, 3)

                if ema9 > ema21:
                    send_telegram(f"📈 {symbol} сигнал на LONG\nЦена: {price}")
                    place_futures_order(symbol, "buy", size)
                elif ema9 < ema21:
                    send_telegram(f"📉 {symbol} сигнал на SHORT\nЦена: {price}")
                    place_futures_order(symbol, "sell", size)
                time.sleep(1)
            except Exception as e:
                print(f"[Ошибка] {symbol}:", e)
        time.sleep(60)

# === Flask keep-alive ===
app = Flask(__name__)

@app.route('/')
def home():
    return "Криптовалютный трейдер KuCoin работает"

# === Стартуем ===
if __name__ == '__main__':
    send_telegram("🤖 Фьючерсный бот KuCoin запущен!")
    threading.Thread(target=trade).start()
    app.run(host='0.0.0.0', port=10000)
