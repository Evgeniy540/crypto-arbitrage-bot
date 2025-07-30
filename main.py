# === main.py ===
import time, hmac, hashlib, json, requests, threading, numpy as np, os
from flask import Flask, request

# === Bitget API ===
API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
API_PASSPHRASE = "Evgeniy84"

# === Telegram ===
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

# === Торговля ===
TRADE_AMOUNT = 5
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT", "PEPEUSDT", "BGBUSDT"]
POSITION_FILE = "position.json"
PROFIT_FILE = "profit.json"
POSITION, ENTRY_PRICE, IN_POSITION_SYMBOL = None, None, None
LAST_NO_SIGNAL_TIME = 0
HEADERS = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}

# === Flask для Render ===
app = Flask(__name__)

@app.route('/')
def home():
    return '✅ Crypto Bot is running!'

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

def sign_request(timestamp, method, request_path, body=""):
    message = str(timestamp) + method + request_path + body
    return hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()

def get_headers(method, path, body=""):
    ts = str(int(time.time() * 1000))
    sig = sign_request(ts, method, path, body)
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

def get_candles(symbol):
    url = f"https://api.bitget.com/api/spot/v1/market/candles?symbol={symbol}&granularity=60"
    try:
        data = requests.get(url, headers=HEADERS).json().get("data", [])
        return [float(c[4]) for c in data[::-1]]
    except:
        return []

def calculate_ema(prices, period):
    return np.convolve(prices, np.ones(period)/period, mode='valid')

def place_order(symbol, side, size):
    url = "https://api.bitget.com/api/spot/v1/trade/orders"
    body = json.dumps({
        "symbol": symbol, "side": side, "orderType": "market",
        "force": "gtc", "size": str(size)
    })
    headers = get_headers("POST", "/api/spot/v1/trade/orders", body)
    return requests.post(url, headers=headers, data=body).json()

def save_position(symbol, qty, price):
    with open(POSITION_FILE, "w") as f:
        json.dump({"symbol": symbol, "qty": qty, "price": price}, f)

def load_position():
    global POSITION, ENTRY_PRICE, IN_POSITION_SYMBOL
    if os.path.exists(POSITION_FILE):
        with open(POSITION_FILE, "r") as f:
            d = json.load(f)
            POSITION = d.get("qty")
            ENTRY_PRICE = d.get("price")
            IN_POSITION_SYMBOL = d.get("symbol")

def clear_position():
    global POSITION, ENTRY_PRICE, IN_POSITION_SYMBOL
    POSITION, ENTRY_PRICE, IN_POSITION_SYMBOL = None, None, None
    if os.path.exists(POSITION_FILE):
        os.remove(POSITION_FILE)

def save_profit(amount):
    profit_data = {"total_profit": 0, "deals": 0}
    if os.path.exists(PROFIT_FILE):
        with open(PROFIT_FILE, "r") as f:
            profit_data = json.load(f)
    profit_data["total_profit"] += round(amount, 4)
    profit_data["deals"] += 1
    with open(PROFIT_FILE, "w") as f:
        json.dump(profit_data, f)

def send_profit_report():
    if os.path.exists(PROFIT_FILE):
        with open(PROFIT_FILE, "r") as f:
            d = json.load(f)
            send_telegram(f"📊 Сделок: {d.get('deals', 0)}\n💰 Прибыль: {d.get('total_profit', 0):.4f} USDT")
    else:
        send_telegram("Нет данных о прибыли.")

def check_signal():
    global POSITION, ENTRY_PRICE, IN_POSITION_SYMBOL, TRADE_AMOUNT, LAST_NO_SIGNAL_TIME
    if POSITION:
        url = f"https://api.bitget.com/api/spot/v1/market/ticker?symbol={IN_POSITION_SYMBOL}"
        last_price = float(requests.get(url).json().get("data", {}).get("last", 0))
        if last_price >= ENTRY_PRICE * 1.015:
            place_order(IN_POSITION_SYMBOL, "sell", POSITION)
            profit = (last_price - ENTRY_PRICE) * POSITION
            send_telegram(f"✅ TP: {IN_POSITION_SYMBOL} {last_price:.4f} | Прибыль: {profit:.4f} USDT")
            save_profit(profit)
            clear_position()
        elif last_price <= ENTRY_PRICE * 0.99:
            place_order(IN_POSITION_SYMBOL, "sell", POSITION)
            send_telegram(f"🛑 SL: {IN_POSITION_SYMBOL} продано по {last_price:.4f}")
            clear_position()
        return

    for symbol in SYMBOLS:
        candles = get_candles(symbol)
        if len(candles) < 21:
            continue
        ema9 = calculate_ema(candles[-21:], 9)
        ema21 = calculate_ema(candles[-21:], 21)
        if len(ema21) == 0 or ema9[-1] <= ema21[-1]:
            continue
        balance = get_balance()
        if balance < TRADE_AMOUNT:
            send_telegram(f"❗Недостаточно USDT: {balance:.2f}, нужно {TRADE_AMOUNT}")
            return
        price = candles[-1]
        qty = round(TRADE_AMOUNT / price, 6)
        resp = place_order(symbol, "buy", qty)
        if resp.get("code") == "00000":
            POSITION, ENTRY_PRICE, IN_POSITION_SYMBOL = qty, price, symbol
            save_position(symbol, qty, price)
            send_telegram(f"📈 Куплено {symbol} по {price:.4f} на {TRADE_AMOUNT} USDT")
        else:
            send_telegram(f"❌ Ошибка покупки {symbol}: {resp}")
        return

    if time.time() - LAST_NO_SIGNAL_TIME > 3600:
        send_telegram("ℹ️ Нет сигнала на вход")
        LAST_NO_SIGNAL_TIME = time.time()

def run_bot():
    send_telegram("🤖 Бот запущен на Render!")
    load_position()
    while True:
        try:
            check_signal()
        except Exception as e:
            send_telegram(f"Ошибка: {e}")
        time.sleep(30)

if __name__ == '__main__':
    threading.Thread(target=run_bot).start()
