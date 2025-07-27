import time
import hmac
import hashlib
import base64
import requests
import json
from flask import Flask
import threading
import logging
import datetime
import statistics

# === Bitget API Ключи ===
BITGET_API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"

# === Telegram ===
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

# === Настройки ===
TRADE_AMOUNT = 10  # USDT
SYMBOLS = ["BTCUSDT_UMCBL", "ETHUSDT_UMCBL", "SOLUSDT_UMCBL", "XRPUSDT_UMCBL", "TRXUSDT_UMCBL"]
BASE_URL = "https://api.bitget.com"
COOLDOWN = 60 * 60 * 3  # 3 часа
last_trade_time = {}

# === Telegram уведомление ===
def send_telegram_message(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        requests.post(url, data=payload)
    except Exception as e:
        print("Ошибка Telegram:", e)

# === Подпись Bitget запроса ===
def sign_request(timestamp, method, path, body=""):
    msg = f"{timestamp}{method}{path}{body}"
    mac = hmac.new(BITGET_API_SECRET.encode(), msg.encode(), hashlib.sha256)
    return mac.hexdigest()

# === Получение свечей ===
def get_candles(symbol):
    try:
        url = f"{BASE_URL}/api/mix/v1/market/history-candles?symbol={symbol}&granularity=1min&limit=100"
        resp = requests.get(url)
        data = resp.json()
        if "data" in data and data["data"]:
            candles = data["data"]
            return list(reversed(candles))
        else:
            return None
    except Exception as e:
        print(f"Ошибка получения свечей для {symbol}:", e)
        return None

# === EMA Расчёт ===
def calculate_ema(prices, period):
    return statistics.mean(prices[-period:])

# === Размещение ордера ===
def place_order(symbol, side):
    timestamp = str(int(time.time() * 1000))
    path = "/api/mix/v1/order/placeOrder"
    url = BASE_URL + path
    client_oid = f"bot_{timestamp}"
    direction = "open_long" if side == "buy" else "open_short"
    body = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "size": str(TRADE_AMOUNT),
        "side": "open",
        "orderType": "market",
        "tradeSide": direction,
        "clientOid": client_oid
    }
    body_json = json.dumps(body)
    sign = sign_request(timestamp, "POST", path, body_json)
    headers = {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json"
    }
    response = requests.post(url, headers=headers, data=body_json)
    return response.json()

# === Торговая логика ===
def trade():
    while True:
        for symbol in SYMBOLS:
            now = time.time()
            if symbol in last_trade_time and now - last_trade_time[symbol] < COOLDOWN:
                continue

            candles = get_candles(symbol)
            if not candles:
                send_telegram_message(f"⚠️ Не удалось получить свечи по {symbol}")
                continue

            try:
                closes = [float(c[4]) for c in candles]
                ema9 = calculate_ema(closes, 9)
                ema21 = calculate_ema(closes, 21)

                if ema9 > ema21:
                    response = place_order(symbol, "buy")
                    send_telegram_message(f"🟢 BUY {symbol}
Ответ: {response}")
                    last_trade_time[symbol] = now
                elif ema9 < ema21:
                    response = place_order(symbol, "sell")
                    send_telegram_message(f"🔴 SELL {symbol}
Ответ: {response}")
                    last_trade_time[symbol] = now
            except Exception as e:
                send_telegram_message(f"❌ Ошибка анализа или размещения ордера по {symbol}: {e}")

        time.sleep(60)

# === Flask для Render ===
app = Flask(__name__)

@app.route('/')
def home():
    return "🤖 Бот успешно запущен на Render!"

# === Запуск ===
if __name__ == '__main__':
    send_telegram_message("🤖 Бот успешно запущен на Render!")
    threading.Thread(target=trade, daemon=True).start()
    app.run(host='0.0.0.0', port=10000)
