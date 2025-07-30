import os
import requests
import time
import hmac
import hashlib
import json
from flask import Flask
import threading
from datetime import datetime

# === КЛЮЧИ ===
BITGET_API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

# === НАСТРОЙКИ ===
SYMBOLS = ["TRXUSDT", "PEPEUSDT", "BGBUSDT", "ETHUSDT", "BTCUSDT", "SOLUSDT", "XRPUSDT"]
TRADE_AMOUNT = 5
TP_PERCENT = 1.5
SL_PERCENT = 1.0
INTERVAL = 60  # частота проверки сигналов (сек)

# === СЕРВИСЫ ===
app = Flask(__name__)
position_file = "position.json"
profit_file = "profit.json"
last_no_signal = {}

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message})
    except:
        pass

def signed_request(method, path, params=None, body=None):
    timestamp = str(int(time.time() * 1000))
    body_json = json.dumps(body) if body else ""
    query = "?" + "&".join([f"{k}={v}" for k, v in params.items()]) if params else ""
    prehash = timestamp + method + path + query + body_json
    signature = hmac.new(BITGET_API_SECRET.encode(), prehash.encode(), hashlib.sha256).hexdigest()
    headers = {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": signature,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json"
    }
    url = f"https://api.bitget.com{path}{query}"
    response = requests.request(method, url, headers=headers, data=body_json)
    return response.json()

def get_candles(symbol):
    try:
        url = f"https://api.bitget.com/api/spot/v1/market/candles?symbol={symbol}&period=1m&limit=100"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
        data = r.json().get("data", [])
        candles = [[float(i[1]), float(i[4])] for i in data[::-1]]
        return candles if len(candles) >= 21 else []
    except:
        return []

def ema(values, period):
    alpha = 2 / (period + 1)
    ema_val = values[0]
    for price in values[1:]:
        ema_val = alpha * price + (1 - alpha) * ema_val
    return ema_val

def get_balance(symbol="USDT"):
    result = signed_request("GET", "/api/spot/v1/account/assets", params={"coin": symbol})
    for asset in result.get("data", []):
        if asset["coin"] == symbol:
            return float(asset["available"])
    return 0

def place_order(symbol, side, amount):
    price = get_price(symbol)
    if not price:
        return None
    params = {
        "symbol": symbol,
        "side": side,
        "orderType": "market",
        "force": "gtc",
        "quantity": round(amount / price, 6)
    }
    return signed_request("POST", "/api/spot/v1/trade/orders", body=params)

def get_price(symbol):
    try:
        r = requests.get(f"https://api.bitget.com/api/spot/v1/market/ticker?symbol={symbol}")
        return float(r.json()["data"]["last"])
    except:
        return None

def load_file(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}

def save_file(path, data):
    with open(path, "w") as f:
        json.dump(data, f)

def check_signals():
    global TRADE_AMOUNT
    position = load_file(position_file)
    profit_data = load_file(profit_file)

    if position:
        symbol = position["symbol"]
        side = position["side"]
        entry = position["entry"]
        amount = position["amount"]
        price = get_price(symbol)

        if price:
            change = ((price - entry) / entry) * 100 * (-1 if side == "sell" else 1)
            if change >= TP_PERCENT or change <= -SL_PERCENT:
                close_side = "buy" if side == "sell" else "sell"
                place_order(symbol, close_side, amount)
                profit = (price - entry) * amount * (-1 if side == "sell" else 1)
                profit_data.setdefault("total", 0)
                profit_data["total"] += profit
                save_file(profit_file, profit_data)
                send_telegram(f"📉 Продано {symbol} по {price}, профит: {round(profit, 4)} USDT")
                save_file(position_file, {})
                TRADE_AMOUNT += max(0, round(profit, 4))
        return

    balance = get_balance()
    if balance < TRADE_AMOUNT:
        send_telegram(f"❗Недостаточно USDT для торговли. Баланс: {balance}")
        return

    for symbol in SYMBOLS:
        candles = get_candles(symbol)
        if not candles:
            continue
        closes = [c[1] for c in candles]
        ema9 = ema(closes[-9:], 9)
        ema21 = ema(closes[-21:], 21)
        if ema9 < ema21:
            continue

        amount = TRADE_AMOUNT
        order = place_order(symbol, "buy", amount)
        price = get_price(symbol)
        if order:
            qty = round(amount / price, 6)
            data = {
                "symbol": symbol,
                "side": "buy",
                "entry": price,
                "amount": qty
            }
            save_file(position_file, data)
            send_telegram(f"🟢 Куплено {symbol} по {price}")
            break
    else:
        now = int(time.time())
        if now - last_no_signal.get("time", 0) > 3600:
            send_telegram("ℹ️ Нет сигнала на вход")
            last_no_signal["time"] = now

@app.route("/")
def home():
    return "Bot is running."

@app.route("/profit")
def show_profit():
    data = load_file(profit_file)
    total = data.get("total", 0)
    return f"💰 Общая прибыль: {round(total, 4)} USDT"

def run_bot():
    send_telegram("🤖 Бот запущен на Render!")
    while True:
        try:
            check_signals()
        except Exception as e:
            print("Ошибка:", e)
        time.sleep(INTERVAL)

if __name__ == "__main__":
    threading.Thread(target=run_bot).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
