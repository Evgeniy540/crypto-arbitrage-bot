import time
import hmac
import hashlib
import base64
import json
import requests
import threading
from flask import Flask

# ==== НАСТРОЙКИ ====
BITGET_API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"
TRADE_AMOUNT = 10
SYMBOLS = ["BTCUSDT_UMCBL", "ETHUSDT_UMCBL", "SOLUSDT_UMCBL", "XRPUSDT_UMCBL", "TRXUSDT_UMCBL"]
EMA_SHORT = 9
EMA_LONG = 21

# ==== TELEGRAM ====
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})
    except Exception as e:
        print(f"Ошибка Telegram: {e}")

# ==== BITGET SIGN ====
def generate_signature(timestamp, method, request_path, body=""):
    message = f"{timestamp}{method}{request_path}{body}"
    mac = hmac.new(BITGET_API_SECRET.encode(), message.encode(), hashlib.sha256)
    d = mac.digest()
    return base64.b64encode(d).decode()

# ==== BITGET HEADERS ====
def get_headers(timestamp, method, path, body):
    sign = generate_signature(timestamp, method, path, body)
    return {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"
    }

# ==== GET CANDLES ====
def get_candles(symbol):
    try:
        url = "https://api.bitget.com/api/mix/v1/market/candles"
        params = {
            "symbol": symbol,
            "granularity": "1min",
            "limit": "100",
            "productType": "umcbl"
        }
        res = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"})
        data = res.json()
        return data["data"] if "data" in data else []
    except Exception as e:
        send_telegram(f"❌ Ошибка свечей по {symbol}: {e}")
        return []

# ==== EMA ====
def calculate_ema(prices, period):
    if len(prices) < period:
        return None
    ema = prices[0]
    k = 2 / (period + 1)
    for price in prices[1:]:
        ema = price * k + ema * (1 - k)
    return ema

# ==== PLACE ORDER ====
def place_order(symbol, side):
    timestamp = str(int(time.time() * 1000))
    path = "/api/mix/v1/order/placeOrder"
    url = "https://api.bitget.com" + path
    body_dict = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "size": str(TRADE_AMOUNT),
        "side": side,
        "orderType": "market",
        "tradeSide": "long" if side == "buy" else "short",
        "productType": "umcbl"
    }
    body = json.dumps(body_dict)
    headers = get_headers(timestamp, "POST", path, body)
    try:
        res = requests.post(url, headers=headers, data=body)
        result = res.json()
        if result.get("code") == "00000":
            send_telegram(f"✅ Сделка {side.upper()} по {symbol}: {result}")
        else:
            send_telegram(f"⚠️ Ошибка сделки {symbol}: {result}")
    except Exception as e:
        send_telegram(f"❌ Ошибка запроса {symbol}: {e}")

# ==== ПРОВЕРКА СИГНАЛА ====
def check_signal(symbol):
    candles = get_candles(symbol)
    if not candles or len(candles) < EMA_LONG:
        return
    try:
        close_prices = [float(c[4]) for c in candles[::-1]]
        ema9 = calculate_ema(close_prices[-EMA_SHORT:], EMA_SHORT)
        ema21 = calculate_ema(close_prices[-EMA_LONG:], EMA_LONG)
        if ema9 and ema21:
            if ema9 > ema21:
                send_telegram(f"📈 LONG сигнал по {symbol}")
                place_order(symbol, "buy")
            elif ema9 < ema21:
                send_telegram(f"📉 SHORT сигнал по {symbol}")
                place_order(symbol, "sell")
    except Exception as e:
        send_telegram(f"❌ Ошибка стратегии по {symbol}: {e}")

# ==== ОСНОВНАЯ ЛОГИКА ====
def run_bot():
    send_telegram("🤖 Бот успешно запущен и анализирует рынок!")
    while True:
        for symbol in SYMBOLS:
            check_signal(symbol)
            time.sleep(3)
        time.sleep(30)

# ==== FLASK ====
app = Flask(__name__)

@app.route("/")
def home():
    return "🤖 Bitget Futures Bot работает!"

# ==== ЗАПУСК ====
if __name__ == "__main__":
    threading.Thread(target=run_bot).start()
    app.run(host="0.0.0.0", port=10000)
