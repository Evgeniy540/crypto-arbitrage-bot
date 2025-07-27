import time
import hmac
import hashlib
import base64
import requests
import json
import threading
from datetime import datetime
from flask import Flask
import schedule

# === КЛЮЧИ ===
BITGET_API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

# === ПАРАМЕТРЫ ===
TRADE_AMOUNT = 10.0
SYMBOLS = ["BTCUSDT_UMCBL", "ETHUSDT_UMCBL", "SOLUSDT_UMCBL", "XRPUSDT_UMCBL", "TRXUSDT_UMCBL"]
CHECK_INTERVAL = 30  # секунд
TP_PERCENT = 1.5
SL_PERCENT = 1.0
last_signal_time = {}

app = Flask(__name__)

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text})

def get_timestamp():
    return str(int(time.time() * 1000))

def sign_request(timestamp, method, path, body):
    body = json.dumps(body) if body else ""
    message = f"{timestamp}{method}{path}{body}"
    mac = hmac.new(BITGET_API_SECRET.encode(), message.encode(), hashlib.sha256)
    return mac.hexdigest()

def get_headers(method, path, body=None):
    timestamp = get_timestamp()
    sign = sign_request(timestamp, method, path, body)
    return {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"
    }

def get_candles(symbol):
    url = "https://api.bitget.com/api/mix/v1/market/history-candles"
    params = {
        "symbol": symbol,
        "granularity": "1min",
        "limit": 100,
        "productType": "umcbl"
    }
    try:
        response = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"})
        if response.status_code != 200:
            send_telegram(f"❗Ошибка HTTP {response.status_code} для {symbol}")
            return []
        data = response.json().get("data", [])
        if not data:
            send_telegram(f"❗Недостаточно данных для {symbol}")
        return list(reversed(data))
    except Exception as e:
        send_telegram(f"Ошибка получения свечей {symbol}: {str(e)}")
        return []

def calculate_ema(data, period):
    ema = []
    k = 2 / (period + 1)
    for i, candle in enumerate(data):
        close = float(candle[4])
        if i == 0:
            ema.append(close)
        else:
            ema.append(close * k + ema[-1] * (1 - k))
    return ema

def place_order(symbol, side, size):
    path = "/api/mix/v1/order/placeOrder"
    url = f"https://api.bitget.com{path}"
    body = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "size": str(size),
        "side": side,
        "orderType": "market",
        "tradeSide": "open",
        "productType": "umcbl"
    }
    headers = get_headers("POST", path, body)
    response = requests.post(url, headers=headers, json=body)
    try:
        res = response.json()
        if res.get("code") == "00000":
            send_telegram(f"✅ Открыта позиция {side.upper()} по {symbol}")
            return True
        else:
            send_telegram(f"Ошибка открытия позиции {symbol}: {res}")
    except:
        send_telegram(f"Ошибка запроса при размещении ордера {symbol}")
    return False

def monitor():
    global TRADE_AMOUNT
    while True:
        for symbol in SYMBOLS:
            candles = get_candles(symbol)
            send_telegram(f"📊 Пришло свечей для {symbol}: {len(candles)}")
            if len(candles) < 21:
                send_telegram(f"❗Недостаточно данных для {symbol}")
                continue
            closes = candles[-21:]
            ema9 = calculate_ema(closes[-9:], 9)[-1]
            ema21 = calculate_ema(closes, 21)[-1]
            now = datetime.now()
            last_time = last_signal_time.get(symbol)
            time_diff = (now - last_time).total_seconds() / 3600 if last_time else 999

            if ema9 > ema21:
                if place_order(symbol, "buy", TRADE_AMOUNT):
                    last_signal_time[symbol] = now
                    TRADE_AMOUNT *= 1 + (TP_PERCENT / 100)
            elif ema9 < ema21:
                if place_order(symbol, "sell", TRADE_AMOUNT):
                    last_signal_time[symbol] = now
                    TRADE_AMOUNT *= 1 + (TP_PERCENT / 100)
            else:
                if time_diff > 1:
                    send_telegram(f"ℹ️ По {symbol} сейчас нет сигнала")
                    last_signal_time[symbol] = now
        time.sleep(CHECK_INTERVAL)

def daily_profit_report():
    send_telegram(f"📈 Ежедневный отчёт: сумма сделки сейчас {round(TRADE_AMOUNT, 2)} USDT")

@app.route("/")
def home():
    return "✅ Бот работает!"

if __name__ == "__main__":
    send_telegram("🤖 Бот запущен и работает на Render!")
    threading.Thread(target=monitor).start()
    schedule.every().day.at("20:47").do(daily_profit_report)

    def schedule_runner():
        while True:
            schedule.run_pending()
            time.sleep(10)

    threading.Thread(target=schedule_runner).start()
    app.run(host="0.0.0.0", port=10000)
