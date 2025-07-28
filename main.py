import time
import hmac
import hashlib
import base64
import json
import requests
import threading
from flask import Flask
import datetime
import schedule

# === НАСТРОЙКИ ===
API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
API_PASSPHRASE = "Evgeniy84"

TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

TRADE_AMOUNT = 10.0
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT"]
TP_PERCENT = 0.015
SL_PERCENT = 0.01

checked_pairs = {}
last_signal_time = {}

app = Flask(__name__)

def send_telegram(text):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    except:
        pass

def get_timestamp():
    return str(int(time.time() * 1000))

def sign_request(timestamp, method, request_path, body=""):
    body_str = json.dumps(body) if body else ""
    message = f"{timestamp}{method}{request_path}{body_str}"
    signature = hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    return signature

def get_headers(timestamp, method, path, body=""):
    return {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": sign_request(timestamp, method, path, body),
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": API_PASSPHRASE,
        "Content-Type": "application/json"
    }

def get_klines(symbol):
    url = f"https://api.bitget.com/api/spot/v1/market/candles?symbol={symbol}&period=1m&limit=100"
    try:
        response = requests.get(url)
        data = response.json()
        if "data" in data:
            return [float(c[4]) for c in data["data"]][::-1]
    except:
        return None
    return None

def calculate_ema(data, period):
    if len(data) < period:
        return None
    ema = sum(data[:period]) / period
    k = 2 / (period + 1)
    for price in data[period:]:
        ema = price * k + ema * (1 - k)
    return ema

def get_balance(symbol):
    timestamp = get_timestamp()
    path = "/api/spot/v1/account/assets"
    headers = get_headers(timestamp, "GET", path)
    try:
        r = requests.get("https://api.bitget.com" + path, headers=headers)
        assets = r.json().get("data", [])
        for asset in assets:
            if asset["coin"].upper() == symbol:
                return float(asset["available"])
    except:
        return 0.0
    return 0.0

def place_order(symbol, side, size):
    timestamp = get_timestamp()
    path = "/api/spot/v1/trade/orders"
    price = get_klines(symbol)[-1]
    body = {
        "symbol": symbol,
        "side": side.lower(),
        "orderType": "market",
        "force": "gtc",
        "size": str(size)
    }
    headers = get_headers(timestamp, "POST", path, body)
    try:
        response = requests.post("https://api.bitget.com" + path, headers=headers, json=body)
        send_telegram(f"🟢 Открыта сделка {side.upper()} по {symbol} на {size} USDT")
        return response.json()
    except Exception as e:
        send_telegram(f"Ошибка при открытии ордера: {e}")
        return None

def trade_loop():
    global TRADE_AMOUNT
    for symbol in SYMBOLS:
        usdt_balance = get_balance("USDT")
        if usdt_balance < TRADE_AMOUNT:
            send_telegram(f"Недостаточно USDT для торговли по {symbol}")
            continue

        klines = get_klines(symbol)
        if not klines or len(klines) < 21:
            continue

        ema9 = calculate_ema(klines, 9)
        ema21 = calculate_ema(klines, 21)
        price = klines[-1]

        if ema9 is None or ema21 is None:
            continue

        # Проверка на уже открытые сделки
        if checked_pairs.get(symbol, False):
            continue

        if ema9 > ema21:
            checked_pairs[symbol] = True
            order = place_order(symbol, "buy", TRADE_AMOUNT / price)

            target_price = price * (1 + TP_PERCENT)
            stop_price = price * (1 - SL_PERCENT)

            while True:
                time.sleep(30)
                klines_new = get_klines(symbol)
                if not klines_new:
                    continue
                current_price = klines_new[-1]
                if current_price >= target_price:
                    balance = get_balance(symbol.replace("USDT", ""))
                    place_order(symbol, "sell", balance)
                    TRADE_AMOUNT *= 1 + TP_PERCENT
                    send_telegram(f"✅ TP достигнут по {symbol}, новая сумма сделки: {round(TRADE_AMOUNT, 2)} USDT")
                    checked_pairs[symbol] = False
                    break
                elif current_price <= stop_price:
                    balance = get_balance(symbol.replace("USDT", ""))
                    place_order(symbol, "sell", balance)
                    TRADE_AMOUNT *= 1 - SL_PERCENT
                    send_telegram(f"❌ SL сработал по {symbol}, новая сумма сделки: {round(TRADE_AMOUNT, 2)} USDT")
                    checked_pairs[symbol] = False
                    break
        else:
            now = time.time()
            if symbol not in last_signal_time or now - last_signal_time[symbol] > 3600:
                send_telegram(f"По {symbol} сейчас нет сигнала")
                last_signal_time[symbol] = now

def start_bot():
    while True:
        try:
            trade_loop()
            time.sleep(30)
        except Exception as e:
            send_telegram(f"Ошибка в боте: {e}")
            time.sleep(60)

def send_daily_summary():
    msg = f"📊 Сводка {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}:\n"
    msg += f"Текущая сумма сделки: {round(TRADE_AMOUNT, 2)} USDT"
    send_telegram(msg)

schedule.every().day.at("20:47").do(send_daily_summary)

def scheduler():
    while True:
        schedule.run_pending()
        time.sleep(10)

@app.route("/")
def home():
    return "Spot EMA Trading Bot is running!"

if __name__ == "__main__":
    threading.Thread(target=start_bot).start()
    threading.Thread(target=scheduler).start()
    app.run(host="0.0.0.0", port=8080)
