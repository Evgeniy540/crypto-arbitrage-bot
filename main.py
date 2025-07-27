import time
import hmac
import hashlib
import base64
import json
import requests
import threading
from flask import Flask
from datetime import datetime

# === НАСТРОЙКИ ===
BITGET_API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"
SYMBOLS = ["BTCUSDT_UMCBL", "ETHUSDT_UMCBL", "SOLUSDT_UMCBL", "XRPUSDT_UMCBL", "TRXUSDT_UMCBL"]
app = Flask(__name__)

# === ГЛОБАЛЬНЫЕ ===
TRADE_AMOUNT = 10.0
last_hour_message = {}
profit = 0.0
positions = {}

# === TELEGRAM ===
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print("Ошибка Telegram:", e)

# === BITGET ===
def get_candles(symbol):
    try:
        url = f"https://api.bitget.com/api/mix/v1/market/history-candles"
        params = {
            "symbol": symbol,
            "granularity": "1min",
            "limit": "100",
            "productType": "umcbl"
        }
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, params=params)
        data = r.json()
        return data["data"] if "data" in data else None
    except Exception as e:
        send_telegram(f"⚠️ Ошибка получения свечей {symbol}: {e}")
        return None

def place_order(symbol, side):
    timestamp = str(int(time.time() * 1000))
    endpoint = "/api/mix/v1/order/placeOrder"
    body = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "size": str(TRADE_AMOUNT),
        "side": "open_long" if side == "buy" else "open_short",
        "orderType": "market",
        "productType": "umcbl"
    }
    json_body = json.dumps(body)
    prehash = f"{timestamp}POST{endpoint}{json_body}"
    sign = hmac.new(BITGET_API_SECRET.encode(), prehash.encode(), hashlib.sha256).digest()
    sign_b64 = base64.b64encode(sign).decode()
    headers = {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign_b64,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json"
    }
    url = "https://api.bitget.com" + endpoint
    r = requests.post(url, headers=headers, data=json_body)
    try:
        result = r.json()
        send_telegram(f"✅ Ордер {side.upper()} {symbol}: {result}")
    except:
        send_telegram(f"⚠️ Ошибка при ордере {symbol}: {r.text}")

# === EMA ===
def calculate_ema(prices, period):
    if len(prices) < period:
        return None
    ema = [sum(prices[:period]) / period]
    k = 2 / (period + 1)
    for price in prices[period:]:
        ema.append((price - ema[-1]) * k + ema[-1])
    return ema

# === STRATEGY ===
def strategy():
    global TRADE_AMOUNT, profit
    while True:
        now = datetime.utcnow()
        for symbol in SYMBOLS:
            candles = get_candles(symbol)
            if not candles or len(candles) < 21:
                if symbol not in last_hour_message or (time.time() - last_hour_message[symbol]) > 3600:
                    send_telegram(f"📭 Недостаточно данных по {symbol}")
                    last_hour_message[symbol] = time.time()
                continue
            close_prices = [float(c[4]) for c in candles[::-1]]
            ema9 = calculate_ema(close_prices, 9)
            ema21 = calculate_ema(close_prices, 21)
            if not ema9 or not ema21:
                continue
            if ema9[-1] > ema21[-1]:
                send_telegram(f"📈 LONG сигнал по {symbol}")
                place_order(symbol, "buy")
                profit += TRADE_AMOUNT * 0.015
                TRADE_AMOUNT += TRADE_AMOUNT * 0.015
            elif ema9[-1] < ema21[-1]:
                send_telegram(f"📉 SHORT сигнал по {symbol}")
                place_order(symbol, "sell")
                profit += TRADE_AMOUNT * 0.015
                TRADE_AMOUNT += TRADE_AMOUNT * 0.015
            else:
                if symbol not in last_hour_message or (time.time() - last_hour_message[symbol]) > 3600:
                    send_telegram(f"ℹ️ По {symbol} сейчас нет сигнала")
                    last_hour_message[symbol] = time.time()
        if now.hour == 20 and now.minute == 47:
            send_telegram(f"📊 Ежедневный отчёт:
💰 Текущая сумма сделки: {round(TRADE_AMOUNT, 2)} USDT
📈 Общая прибыль: {round(profit, 2)} USDT")
            time.sleep(60)
        time.sleep(30)

# === FLASK ===
@app.route("/")
def index():
    return "🤖 Bitget EMA бот работает на Render!"

# === ЗАПУСК ===
if __name__ == "__main__":
    threading.Thread(target=strategy).start()
    send_telegram("🤖 Бот запущен на Render!")
    app.run(host="0.0.0.0", port=10000)
