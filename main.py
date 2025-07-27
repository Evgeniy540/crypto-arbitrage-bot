import time
import hmac
import hashlib
import base64
import requests
import json
import logging
import threading
from datetime import datetime
from flask import Flask

# === НАСТРОЙКИ ===
API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
API_PASSPHRASE = "Evgeniy84"

TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

TRADE_SYMBOLS = ["btcusdt_UMCBL", "ethusdt_UMCBL", "solusdt_UMCBL", "xrpusdt_UMCBL", "trxusdt_UMCBL"]
TRADE_AMOUNT = 10
INTERVAL = "1H"
TP_PERCENT = 1.5
SL_PERCENT = 1.0

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=payload)
    except Exception as e:
        logging.error(f"Ошибка Telegram: {e}")

def sign_request(timestamp, method, path, body=""):
    msg = f'{timestamp}{method}{path}{body}'
    signature = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return signature

def get_headers(method, path, body=""):
    timestamp = str(int(time.time() * 1000))
    sign = sign_request(timestamp, method, path, body)
    return {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": API_PASSPHRASE,
        "Content-Type": "application/json"
    }

def get_klines(symbol):
    url = f"https://api.bitget.com/api/mix/v1/market/candles?symbol={symbol}&granularity={INTERVAL}"
    try:
        res = requests.get(url).json()
        if 'data' not in res or not res['data']:
            return None
        closes = [float(k[4]) for k in res['data']]
        return closes[::-1]
    except Exception as e:
        logging.error(f"Ошибка получения свечей: {e}")
        return None

def calculate_ema(data, period):
    if len(data) < period:
        return None
    ema = sum(data[:period]) / period
    k = 2 / (period + 1)
    for price in data[period:]:
        ema = price * k + ema * (1 - k)
    return ema

def place_order(symbol, side):
    url = "https://api.bitget.com/api/mix/v1/order/placeOrder"
    data = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "size": str(TRADE_AMOUNT),
        "side": side,
        "orderType": "market",
        "tradeSide": "open",
        "productType": "umcbl"
    }
    body = json.dumps(data)
    headers = get_headers("POST", "/api/mix/v1/order/placeOrder", body)
    res = requests.post(url, headers=headers, data=body)
    try:
        result = res.json()
        if result.get("code") == "00000":
            send_telegram_message(f"✅ Открыт {side.upper()} ордер по {symbol.upper()} на {TRADE_AMOUNT} USDT")
        else:
            send_telegram_message(f"❌ Ошибка при открытии {side.upper()} на {symbol.upper()}:\n{result}")
    except Exception as e:
        send_telegram_message(f"⚠️ Ошибка при открытии сделки: {e}")

def strategy():
    for symbol in TRADE_SYMBOLS:
        data = get_klines(symbol)
        if not data or len(data) < 22:
            logging.warning(f"Недостаточно данных для {symbol}")
            continue

        ema9 = calculate_ema(data, 9)
        ema21 = calculate_ema(data, 21)
        logging.info(f"{symbol} EMA9: {ema9:.2f}, EMA21: {ema21:.2f}")

        if ema9 > ema21:
            place_order(symbol, "buy")
        elif ema9 < ema21:
            place_order(symbol, "sell")
        else:
            logging.info(f"📊 Нет сигнала для {symbol}")

def run_bot():
    while True:
        try:
            send_telegram_message("🤖 Бот запущен и анализирует рынок...")
            strategy()
        except Exception as e:
            logging.error(f"Ошибка в стратегии: {e}")
            send_telegram_message(f"⚠️ Ошибка бота: {e}")
        time.sleep(60 * 60)  # раз в час

@app.route("/")
def home():
    return "✅ Bitget Futures Trading Bot запущен!"

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
