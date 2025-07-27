import requests
import time
import hmac
import hashlib
import base64
import json
from datetime import datetime, timedelta
from flask import Flask
import threading

# === API КЛЮЧИ ===
API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
API_PASSPHRASE = "Evgeniy84"

TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

# === НАСТРОЙКИ ===
SYMBOLS = ["BTCUSDT_UMCBL", "ETHUSDT_UMCBL", "SOLUSDT_UMCBL", "XRPUSDT_UMCBL", "TRXUSDT_UMCBL"]
TRADE_AMOUNT = 10
CHECK_INTERVAL = 30  # секунд
TP_PERCENT = 1.5
SL_PERCENT = 1.0
last_no_signal = {}
last_report_time = None

app = Flask(__name__)

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print("Ошибка Telegram:", e)

def get_timestamp():
    return str(int(time.time() * 1000))

def sign(params, secret):
    sorted_params = sorted(params.items())
    query = "&".join([f"{k}={v}" for k, v in sorted_params])
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()

def get_candles(symbol):
    url = "https://api.bitget.com/api/mix/v1/market/history-candles"
    params = {
        "symbol": symbol.replace("_", ""),
        "granularity": "1min",
        "limit": "100",
        "productType": "umcbl"
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, params=params, headers=headers)
        if response.status_code == 200:
            data = response.json()
            return [[float(x[1]) for x in data['data']]] if data.get("data") else None
        else:
            send_telegram(f"❗Ошибка HTTP {response.status_code} для {symbol}")
            return None
    except Exception as e:
        send_telegram(f"❗Ошибка получения свечей для {symbol}: {e}")
        return None

def calculate_ema(prices, period):
    if len(prices) < period:
        return None
    ema = prices[:period]
    multiplier = 2 / (period + 1)
    for price in prices[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema[-1]

def place_order(symbol, side, size):
    url = "https://api.bitget.com/api/mix/v1/order/placeOrder"
    timestamp = get_timestamp()
    body = {
        "symbol": symbol.replace("_", ""),
        "marginCoin": "USDT",
        "size": str(size),
        "side": side,
        "orderType": "market",
        "timeInForceValue": "normal",
        "clientOid": str(int(time.time() * 1000)),
        "productType": "umcbl"
    }
    body_json = json.dumps(body)
    message = timestamp + "POST" + "/api/mix/v1/order/placeOrder" + body_json
    sign_header = base64.b64encode(hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha256).digest()).decode()
    headers = {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": sign_header,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": API_PASSPHRASE,
        "Content-Type": "application/json"
    }
    response = requests.post(url, headers=headers, data=body_json)
    if response.status_code == 200:
        send_telegram(f"✅ Открыт ордер {side.upper()} {symbol} на {size} USDT")
    else:
        send_telegram(f"❗Ошибка при размещении ордера: {response.text}")

def check_signal(symbol):
    global TRADE_AMOUNT

    candles = get_candles(symbol)
    if not candles or len(candles[0]) < 21:
        send_telegram(f"❗ Недостаточно данных для {symbol}")
        return

    closes = candles[0]
    ema9 = calculate_ema(closes, 9)
    ema21 = calculate_ema(closes, 21)

    if ema9 is None or ema21 is None:
        return

    now = datetime.utcnow()
    if ema9 > ema21:
        place_order(symbol, "open_long", TRADE_AMOUNT)
        entry_price = closes[-1]
        tp_price = entry_price * (1 + TP_PERCENT / 100)
        sl_price = entry_price * (1 - SL_PERCENT / 100)
        send_telegram(f"🎯 Сигнал на LONG {symbol}\nЦена входа: {entry_price:.4f}\nTP: {tp_price:.4f}\nSL: {sl_price:.4f}")
        TRADE_AMOUNT += round(TRADE_AMOUNT * 0.015, 2)
    else:
        last = last_no_signal.get(symbol, datetime.min)
        if (now - last).seconds >= 3600:
            send_telegram(f"ℹ️ По {symbol} сейчас нет сигнала")
            last_no_signal[symbol] = now

def run_bot():
    send_telegram("🤖 Бот запущен и работает на Render!")
    while True:
        for symbol in SYMBOLS:
            check_signal(symbol)
        time.sleep(CHECK_INTERVAL)

@app.route('/')
def home():
    return "Бот работает!"

def start_flask():
    app.run(host="0.0.0.0", port=10000)

if __name__ == "__main__":
    threading.Thread(target=run_bot).start()
    threading.Thread(target=start_flask).start()
