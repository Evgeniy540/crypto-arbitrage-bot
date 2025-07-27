import time
import hmac
import hashlib
import base64
import requests
import json
import threading
from flask import Flask
from datetime import datetime
import schedule

# === НАСТРОЙКИ ===
BITGET_API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"
SYMBOLS = ["BTCUSDT_UMCBL", "ETHUSDT_UMCBL", "SOLUSDT_UMCBL", "XRPUSDT_UMCBL", "TRXUSDT_UMCBL"]
TRADE_AMOUNT = 10
TP_PERCENT = 1.5
SL_PERCENT = 1.0

last_signal_time = {}
entry_prices = {}
in_position = {}
last_message_time = {}

for sym in SYMBOLS:
    last_signal_time[sym] = 0
    in_position[sym] = False
    last_message_time[sym] = 0

app = Flask(__name__)

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print("Telegram error:", e)

def get_timestamp():
    return str(int(time.time() * 1000))

def sign_request(timestamp, method, endpoint, body=""):
    message = timestamp + method + endpoint + body
    mac = hmac.new(BITGET_API_SECRET.encode(), message.encode(), hashlib.sha256)
    return mac.hexdigest()

def get_headers(method, endpoint, body=""):
    timestamp = get_timestamp()
    sign = sign_request(timestamp, method, endpoint, body)
    return {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json",
    }

def get_candles(symbol):
    url = f"https://api.bitget.com/api/mix/v1/market/history-candles"
    params = {"symbol": symbol, "granularity": "60", "limit": "100"}
    try:
        res = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"})
        data = res.json()
        if "data" not in data or not data["data"] or len(data["data"]) < 21:
            send_telegram(f"⚠️ Недостаточно данных для {symbol}. Получено свечей: {len(data['data']) if 'data' in data else 0}")
            return []
        candles = [[float(x[1])] for x in data["data"]]
        return candles[::-1]
    except Exception as e:
        send_telegram(f"❗Ошибка получения свечей для {symbol}: {e}")
        return []

def calculate_ema(data, period):
    ema = []
    k = 2 / (period + 1)
    for i in range(len(data)):
        if i < period - 1:
            ema.append(None)
        elif i == period - 1:
            sma = sum(data[:period]) / period
            ema.append(sma)
        else:
            ema.append(data[i] * k + ema[i-1] * (1 - k))
    return ema

def place_order(symbol, side, amount):
    endpoint = "/api/mix/v1/order/placeOrder"
    url = f"https://api.bitget.com{endpoint}"
    body = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "size": str(amount),
        "side": side,
        "orderType": "market",
        "tradeSide": "open",
        "productType": "umcbl"
    }
    body_json = json.dumps(body)
    headers = get_headers("POST", endpoint, body_json)
    try:
        response = requests.post(url, headers=headers, data=body_json)
        data = response.json()
        if "code" in data and data["code"] != "00000":
            send_telegram(f"❗Ошибка размещения ордера для {symbol}: {data}")
        return data
    except Exception as e:
        send_telegram(f"❗Ошибка при размещении ордера: {e}")

def close_position(symbol, side, amount):
    endpoint = "/api/mix/v1/order/placeOrder"
    url = f"https://api.bitget.com{endpoint}"
    body = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "size": str(amount),
        "side": side,
        "orderType": "market",
        "tradeSide": "close",
        "productType": "umcbl"
    }
    body_json = json.dumps(body)
    headers = get_headers("POST", endpoint, body_json)
    try:
        response = requests.post(url, headers=headers, data=body_json)
        data = response.json()
        if "code" in data and data["code"] != "00000":
            send_telegram(f"❗Ошибка закрытия позиции по {symbol}: {data}")
        return data
    except Exception as e:
        send_telegram(f"❗Ошибка при закрытии позиции: {e}")

def check_signals():
    for symbol in SYMBOLS:
        now = time.time()
        candles = get_candles(symbol)
        if len(candles) < 21:
            continue
        closes = [x[0] for x in candles]
        ema9 = calculate_ema(closes, 9)
        ema21 = calculate_ema(closes, 21)
        if ema9[-1] is None or ema21[-1] is None:
            continue

        if not in_position[symbol] and ema9[-1] > ema21[-1]:
            entry_price = closes[-1]
            result = place_order(symbol, "buy", TRADE_AMOUNT)
            if result:
                in_position[symbol] = True
                entry_prices[symbol] = entry_price
                send_telegram(f"✅ Вход в позицию {symbol} по {entry_price:.4f}")
        elif in_position[symbol]:
            current_price = closes[-1]
            entry_price = entry_prices[symbol]
            if current_price >= entry_price * (1 + TP_PERCENT / 100):
                close_position(symbol, "sell", TRADE_AMOUNT)
                in_position[symbol] = False
                global TRADE_AMOUNT
                TRADE_AMOUNT *= 1.01  # реинвестируем 1% прибыли
                send_telegram(f"📈 TP по {symbol}! Продано по {current_price:.4f}")
            elif current_price <= entry_price * (1 - SL_PERCENT / 100):
                close_position(symbol, "sell", TRADE_AMOUNT)
                in_position[symbol] = False
                send_telegram(f"📉 SL по {symbol}! Продано по {current_price:.4f}")
        elif now - last_message_time[symbol] >= 3600:
            send_telegram(f"ℹ️ По {symbol} сейчас нет сигнала")
            last_message_time[symbol] = now

def send_daily_report():
    send_telegram(f"📊 Ежедневный отчёт:\nТекущая сумма сделки: {TRADE_AMOUNT:.2f} USDT")

def run_scheduler():
    schedule.every().day.at("20:47").do(send_daily_report)
    while True:
        schedule.run_pending()
        time.sleep(1)

def main_loop():
    send_telegram("🤖 Бот запущен и работает на Render!")
    while True:
        check_signals()
        time.sleep(30)

@app.route('/')
def home():
    return "Бот работает!"

if __name__ == "__main__":
    threading.Thread(target=main_loop).start()
    threading.Thread(target=run_scheduler).start()
    app.run(host="0.0.0.0", port=10000)
