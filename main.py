# === main.py ===
import time, hmac, hashlib, json, requests, threading, numpy as np, os
from flask import Flask, request
from datetime import datetime
import schedule

# === КЛЮЧИ ===
API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
API_PASSPHRASE = "Evgeniy84"
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

# === ПАРАМЕТРЫ ===
TRADE_AMOUNT = 10
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT", "PEPEUSDT", "BGBUSDT"]
POSITION_FILE = "position.json"
PROFIT_FILE = "profit.json"
HEADERS = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
POSITION, ENTRY, SYMBOL = None, None, None
LAST_HOUR_MSG = 0

# === FLASK ===
app = Flask(__name__)
@app.route('/')
def home(): return '✅ Crypto Bot is running!'
@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    data = request.get_json()
    if "message" in data:
        chat_id = str(data["message"]["chat"]["id"])
        text = data["message"].get("text", "")
        if text == "/profit" and chat_id == TELEGRAM_CHAT_ID:
            send_profit_report()
    return "ok"

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg})

# === BITGET ===
def sign(ts, method, path, body=""):
    msg = str(ts) + method + path + body
    return hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
def get_headers(method, path, body=""):
    ts = str(int(time.time() * 1000))
    sig = sign(ts, method, path, body)
    return {
        "ACCESS-KEY": API_KEY, "ACCESS-SIGN": sig, "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": API_PASSPHRASE, "Content-Type": "application/json"
    }
def get_balance():
    url = "https://api.bitget.com/api/spot/v1/account/assets"
    r = requests.get(url, headers=get_headers("GET", "/api/spot/v1/account/assets")).json()
    for a in r.get("data", []):
        if a["coinName"] == "USDT":
            return float(a["available"])
    return 0
def get_price(symbol):
    url = f"https://api.bitget.com/api/spot/v1/market/ticker?symbol={symbol}"
    r = requests.get(url, headers=HEADERS).json()
    return float(r["data"]["last"])
def get_candles(symbol):
    url = f"https://api.bitget.com/api/spot/v1/market/candles?symbol={symbol}&granularity=60"
    try:
        data = requests.get(url, headers=HEADERS).json()["data"]
        return [float(c[4]) for c in data[::-1]]
    except:
        return []

# === POSITION ===
def save_position(symbol, qty, entry):
    with open(POSITION_FILE, "w") as f:
        json.dump({"symbol": symbol, "qty": qty, "price": entry}, f)
def load_position():
    global POSITION, ENTRY, SYMBOL
    if os.path.exists(POSITION_FILE):
        with open(POSITION_FILE) as f:
            d = json.load(f)
            POSITION = d["qty"]
            ENTRY = d["price"]
            SYMBOL = d["symbol"]
def clear_position():
    global POSITION, ENTRY, SYMBOL
    POSITION, ENTRY, SYMBOL = None, None, None
    if os.path.exists(POSITION_FILE):
        os.remove(POSITION_FILE)

# === DEAL LOG ===
def save_profit(amount):
    global TRADE_AMOUNT
    data = {"total_profit": 0, "deals": 0}
    if os.path.exists(PROFIT_FILE):
        with open(PROFIT_FILE) as f:
            data = json.load(f)
    data["total_profit"] += round(amount, 4)
    data["deals"] += 1
    with open(PROFIT_FILE, "w") as f:
        json.dump(data, f)
    TRADE_AMOUNT += round(amount, 4)
def send_profit_report():
    if os.path.exists(PROFIT_FILE):
        with open(PROFIT_FILE) as f:
            d = json.load(f)
            send_telegram(f"📊 Сделок: {d['deals']}\n💰 Прибыль: {d['total_profit']:.4f} USDT\n🔁 Следующая сделка на: {TRADE_AMOUNT:.2f} USDT")
    else:
        send_telegram("Нет данных по прибыли.")

# === ТОРГОВЛЯ ===
def place_order(symbol, side, size):
    url = "https://api.bitget.com/api/spot/v1/trade/orders"
    body = json.dumps({
        "symbol": symbol, "side": side, "orderType": "market",
        "force": "gtc", "size": str(size)
    })
    headers = get_headers("POST", "/api/spot/v1/trade/orders", body)
    return requests.post(url, headers=headers, data=body).json()

def calculate_ema(prices, period):
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    return np.convolve(prices, weights[::-1], mode='valid')

def check_signals():
    global POSITION, ENTRY, SYMBOL, LAST_HOUR_MSG, TRADE_AMOUNT
    if POSITION:
        price = get_price(SYMBOL)
        if price >= ENTRY * 1.015:
            place_order(SYMBOL, "sell", POSITION)
            profit = (price - ENTRY) * POSITION
            send_telegram(f"✅ TP: {SYMBOL} {price:.4f}\nПрибыль: {profit:.4f}")
            save_profit(profit)
            clear_position()
        elif price <= ENTRY * 0.99:
            place_order(SYMBOL, "sell", POSITION)
            loss = (ENTRY - price) * POSITION
            send_telegram(f"🛑 SL: {SYMBOL} {price:.4f}\nУбыток: -{loss:.4f}")
            clear_position()
        return

    for symbol in SYMBOLS:
        candles = get_candles(symbol)
        if len(candles) < 21:
            continue
        ema9 = calculate_ema(candles[-21:], 9)
        ema21 = calculate_ema(candles[-21:], 21)
        if ema9[-1] > ema21[-1]:
            balance = get_balance()
            if balance < TRADE_AMOUNT:
                send_telegram(f"❗ Недостаточно USDT: {balance:.2f}, нужно {TRADE_AMOUNT}")
                return
            price = candles[-1]
            qty = round(TRADE_AMOUNT / price, 6)
            resp = place_order(symbol, "buy", qty)
            if resp.get("code") == "00000":
                POSITION, ENTRY, SYMBOL = qty, price, symbol
                save_position(symbol, qty, price)
                send_telegram(f"📈 Куплено {symbol} по {price:.4f} на {TRADE_AMOUNT:.2f} USDT")
            else:
                send_telegram(f"❌ Ошибка покупки {symbol}: {resp}")
            return

    if time.time() - LAST_HOUR_MSG > 3600:
        send_telegram("ℹ️ Нет сигнала на вход")
        LAST_HOUR_MSG = time.time()

# === Запуск ===
def run_bot():
    send_telegram("🤖 Бот запущен на Render!")
    load_position()
    while True:
        try:
            check_signals()
        except Exception as e:
            send_telegram(f"Ошибка: {e}")
        time.sleep(30)

# === Ежедневный отчёт ===
def daily_report():
    send_profit_report()
schedule.every().day.at("20:47").do(daily_report)

def schedule_loop():
    while True:
        schedule.run_pending()
        time.sleep(5)

# === Старт ===
if __name__ == '__main__':
    threading.Thread(target=run_bot).start()
    threading.Thread(target=schedule_loop).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
