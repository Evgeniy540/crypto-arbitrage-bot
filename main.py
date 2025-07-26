import time
import hmac
import hashlib
import base64
import requests
import json
import threading
from flask import Flask
import logging
import datetime

# === API КЛЮЧИ KUCOIN FUTURES ===
API_KEY = "68855c7628335c0001f5d42e"
API_SECRET = "0c475ab6-4588-4301-9eb3-77c493b7e621"
API_PASSPHRASE = "Evgeniy@84"

# === TELEGRAM ===
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

# === ПАРАМЕТРЫ ===
TRADE_AMOUNT = 50
LEVERAGE = 5
SYMBOLS = ["BTCUSDTM", "ETHUSDTM", "SOLUSDTM", "GALAUSDTM", "TRXUSDTM"]
COOLDOWN = 60 * 60 * 6  # 6 часов

last_trade_time = {}
BASE_URL = "https://api-futures.kucoin.com"

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# === TELEGRAM ===
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})

# === ПОДПИСЬ ===
def get_headers(endpoint, method, body=""):
    now = int(time.time() * 1000)
    str_to_sign = f"{now}{method}{endpoint}{body}"
    signature = base64.b64encode(
        hmac.new(API_SECRET.encode(), str_to_sign.encode(), hashlib.sha256).digest()
    ).decode()
    passphrase = base64.b64encode(
        hmac.new(API_SECRET.encode(), API_PASSPHRASE.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "KC-API-KEY": API_KEY,
        "KC-API-SIGN": signature,
        "KC-API-TIMESTAMP": str(now),
        "KC-API-KEY-VERSION": "2",
        "KC-API-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }

# === ПОЛУЧЕНИЕ СВЕЧЕЙ ===
def get_klines(symbol):
    url = f"/api/v1/kline/query?symbol={symbol}&granularity=5"
    headers = get_headers(url, "GET")
    r = requests.get(BASE_URL + url, headers=headers)
    try:
        data = r.json()
        return [float(k[2]) for k in data["data"]][-21:]
    except:
        send_telegram(f"⚠️ Ошибка получения свечей для {symbol}")
        return []

# === EMA ===
def calculate_ema(data, period):
    if len(data) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(data[:period]) / period
    for price in data[period:]:
        ema = price * k + ema * (1 - k)
    return ema

# === ОТКРЫТИЕ СДЕЛКИ ===
def place_order(symbol, side):
    endpoint = "/api/v1/orders"
    url = BASE_URL + endpoint
    order = {
        "symbol": symbol,
        "type": "market",
        "side": side,
        "leverage": str(LEVERAGE),
        "size": get_contract_size(symbol),
    }
    headers = get_headers(endpoint, "POST", json.dumps(order))
    r = requests.post(url, headers=headers, data=json.dumps(order))
    if r.status_code == 200 and "orderId" in r.text:
        send_telegram(f"✅ Открыт {'LONG' if side=='buy' else 'SHORT'} на {symbol}")
    else:
        send_telegram(f"⚠️ Ошибка при открытии {side.upper()} на {symbol}:\n{r.text}")

# === РАСЧЁТ РАЗМЕРА КОНТРАКТА ===
def get_contract_size(symbol):
    # Заглушка на 50 USDT (можно заменить логикой по стоимости контракта)
    return "1"

# === ПРОВЕРКА СИГНАЛА ===
def trade_logic(symbol):
    now = time.time()
    if symbol in last_trade_time and now - last_trade_time[symbol] < COOLDOWN:
        return
    candles = get_klines(symbol)
    if not candles or len(candles) < 21:
        return
    ema9 = calculate_ema(candles, 9)
    ema21 = calculate_ema(candles, 21)
    if ema9 and ema21:
        if ema9 > ema21:
            place_order(symbol, "buy")
        elif ema9 < ema21:
            place_order(symbol, "sell")
        last_trade_time[symbol] = now

# === ОСНОВНОЙ ЦИКЛ ===
def main_loop():
    send_telegram("🤖 Бот запущен на KuCoin Futures!")
    while True:
        for symbol in SYMBOLS:
            try:
                trade_logic(symbol)
            except Exception as e:
                send_telegram(f"❌ Ошибка в {symbol}: {e}")
        time.sleep(60)

# === FLASK KEEP-ALIVE ===
@app.route("/")
def home():
    return "KuCoin Futures Bot работает!"

# === СТАРТ ===
if __name__ == "__main__":
    threading.Thread(target=main_loop).start()
    app.run(host="0.0.0.0", port=8080)
