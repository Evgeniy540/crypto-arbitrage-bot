import time
import hmac
import hashlib
import base64
import requests
import json
from flask import Flask, request
import threading

# === НАСТРОЙКИ ===
BITGET_API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"
TRADE_AMOUNT = 10
SYMBOLS = ["BTCUSDT_UMCBL", "ETHUSDT_UMCBL", "SOLUSDT_UMCBL", "XRPUSDT_UMCBL", "TRXUSDT_UMCBL"]
CHECK_INTERVAL = 30  # проверка сигналов каждые 30 секунд

positions = {}
last_signal_time = {}
last_profit = 0

app = Flask(__name__)

# === TELEGRAM ===
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        requests.post(url, data=data)
    except:
        pass

# === BITGET SIGN ===
def get_headers(method, endpoint, body=""):
    timestamp = str(int(time.time() * 1000))
    prehash = f"{timestamp}{method.upper()}{endpoint}{body}"
    sign = hmac.new(BITGET_API_SECRET.encode(), prehash.encode(), hashlib.sha256).digest()
    sign_b64 = base64.b64encode(sign).decode()
    return {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign_b64,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json"
    }

# === CANDLES ===
def get_candles(symbol):
    try:
        url = f"https://api.bitget.com/api/mix/v1/market/candles?symbol={symbol}&granularity=1min&limit=100"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers)
        data = r.json()
        candles = data.get("data", [])
        return list(reversed(candles)) if len(candles) >= 21 else []
    except Exception as e:
        send_telegram(f"⚠️ Ошибка получения свечей {symbol}: {e}")
        return []

# === EMA ===
def ema(data, period):
    if len(data) < period:
        return None
    k = 2 / (period + 1)
    ema_val = sum(data[:period]) / period
    for price in data[period:]:
        ema_val = price * k + ema_val * (1 - k)
    return ema_val

# === ORDER ===
def place_order(symbol, side):
    endpoint = "/api/mix/v1/order/placeOrder"
    url = "https://api.bitget.com" + endpoint
    body = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "size": str(TRADE_AMOUNT),
        "side": "open_long" if side == "buy" else "open_short",
        "orderType": "market",
        "productType": "umcbl"
    }
    headers = get_headers("POST", endpoint, json.dumps(body))
    try:
        res = requests.post(url, headers=headers, data=json.dumps(body)).json()
        send_telegram(f"✅ Открыт ордер {side.upper()} по {symbol}: {res}")
        positions[symbol] = {
            "side": side,
            "entry": float(get_candles(symbol)[-1][4]),
            "time": time.time()
        }
    except Exception as e:
        send_telegram(f"❌ Ошибка ордера {symbol}: {e}")

# === TP / SL ===
def check_exit(symbol):
    if symbol not in positions:
        return
    side = positions[symbol]["side"]
    entry = positions[symbol]["entry"]
    now_price = float(get_candles(symbol)[-1][4])
    change = (now_price - entry) / entry * 100 if side == "buy" else (entry - now_price) / entry * 100
    if change >= 1.5 or change <= -1.0:
        close_order(symbol, side, now_price)

def close_order(symbol, side, price):
    global last_profit
    profit = round((price - positions[symbol]["entry"]) * (1 if side == "buy" else -1), 4)
    last_profit += profit
    send_telegram(f"💰 Закрыт ордер {symbol} с прибылью {profit:.4f} USDT")
    del positions[symbol]

# === STRATEGY ===
def strategy():
    while True:
        for symbol in SYMBOLS:
            candles = get_candles(symbol)
            if len(candles) < 21:
                now = time.time()
                if time.time() - last_signal_time.get(symbol, 0) > 3600:
                    send_telegram(f"⏳ Недостаточно данных по {symbol}")
                    last_signal_time[symbol] = now
                continue

            close_prices = [float(c[4]) for c in candles]
            ema9 = ema(close_prices, 9)
            ema21 = ema(close_prices, 21)

            if ema9 is None or ema21 is None:
                continue

            # Закрытие по TP/SL
            check_exit(symbol)

            # Открытие позиции
            if symbol not in positions:
                if ema9 > ema21:
                    place_order(symbol, "buy")
                elif ema9 < ema21:
                    place_order(symbol, "sell")
                else:
                    if time.time() - last_signal_time.get(symbol, 0) > 3600:
                        send_telegram(f"ℹ️ По {symbol} сейчас нет сигнала")
                        last_signal_time[symbol] = time.time()

        time.sleep(CHECK_INTERVAL)

# === TELEGRAM COMMAND ===
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    msg = request.json
    if "message" in msg and msg["message"].get("text") == "/profit":
        profit_str = f"📊 Общая прибыль: {round(last_profit, 4)} USDT\n"
        for s in positions:
            profit_str += f"🔄 Открыта позиция: {s} {positions[s]['side']} @ {positions[s]['entry']}\n"
        send_telegram(profit_str)
    return "ok"

# === START ===
@app.route("/")
def home():
    return "🤖 Bitget бот работает!"

def start():
    send_telegram("🚀 Бот запущен на Render!")
    threading.Thread(target=strategy).start()

if __name__ == "__main__":
    start()
    app.run(host="0.0.0.0", port=10000)
