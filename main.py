import time
import hmac
import hashlib
import base64
import requests
import json
import threading
from flask import Flask

# === НАСТРОЙКИ ===
BITGET_API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"
TRADE_AMOUNT = 10
SYMBOLS = ["BTCUSDT_UMCBL", "ETHUSDT_UMCBL", "SOLUSDT_UMCBL", "XRPUSDT_UMCBL", "TRXUSDT_UMCBL"]

app = Flask(__name__)
last_no_signal = {}
last_report_time = 0

# === TELEGRAM ===
def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print(f"Ошибка Telegram: {e}")

# === GET CANDLES ===
def get_candles(symbol):
    url = "https://api.bitget.com/api/mix/v1/market/candles"
    params = {
        "symbol": symbol,
        "granularity": "1min",
        "limit": "100",
        "productType": "umcbl"
    }
    try:
        response = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"})
        data = response.json()
        return data["data"] if "data" in data else None
    except Exception as e:
        print(f"Ошибка получения свечей {symbol}: {e}")
        return None

# === EMA ===
def calculate_ema(prices, period):
    ema = []
    k = 2 / (period + 1)
    for i, price in enumerate(prices):
        if i < period - 1:
            ema.append(None)
        elif i == period - 1:
            sma = sum(prices[:period]) / period
            ema.append(sma)
        else:
            ema.append((price - ema[-1]) * k + ema[-1])
    return ema

# === SIGN ===
def generate_signature(timestamp, method, request_path, body=""):
    prehash = f"{timestamp}{method}{request_path}{body}"
    signature = hmac.new(BITGET_API_SECRET.encode(), prehash.encode(), hashlib.sha256).hexdigest()
    return signature

def get_headers(timestamp, method, path, body=""):
    sign = generate_signature(timestamp, method, path, body)
    return {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json"
    }

# === ORDER ===
def place_order(symbol, side):
    timestamp = str(int(time.time() * 1000))
    url_path = "/api/mix/v1/order/placeOrder"
    method = "POST"
    data = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "size": str(TRADE_AMOUNT),
        "side": side,
        "orderType": "market",
        "tradeSide": "long" if side == "buy" else "short",
        "productType": "umcbl"
    }
    body = json.dumps(data)
    headers = get_headers(timestamp, method, url_path, body)
    full_url = "https://api.bitget.com" + url_path
    try:
        response = requests.post(full_url, headers=headers, data=body)
        result = response.json()
        if result.get("code") == "00000":
            send_telegram_message(f"✅ Ордер {side.upper()} {symbol} выполнен")
        else:
            send_telegram_message(f"❌ Ошибка ордера {symbol}: {result}")
    except Exception as e:
        send_telegram_message(f"⚠️ Ошибка запроса ордера {symbol}: {e}")

# === STRATEGY ===
def strategy():
    global last_report_time
    one_time_report = ""

    while True:
        for symbol in SYMBOLS:
            try:
                candles = get_candles(symbol)
                if not candles:
                    send_telegram_message(f"⚠️ Нет свечей для {symbol}")
                    continue

                close_prices = [float(c[4]) for c in candles if c[4] is not None]
                if len(close_prices) < 21:
                    one_time_report += f"❗ {symbol}: только {len(close_prices)} свечей\n"
                    continue

                ema9 = calculate_ema(close_prices, 9)
                ema21 = calculate_ema(close_prices, 21)

                if ema9[-1] > ema21[-1]:
                    place_order(symbol, "buy")
                elif ema9[-1] < ema21[-1]:
                    place_order(symbol, "sell")
                else:
                    now = time.time()
                    if symbol not in last_no_signal or now - last_no_signal[symbol] > 3600:
                        send_telegram_message(f"ℹ️ По {symbol} сейчас нет сигнала")
                        last_no_signal[symbol] = now

            except Exception as e:
                send_telegram_message(f"❌ Ошибка стратегии по {symbol}: {e}")

        if one_time_report and time.time() - last_report_time > 3600:
            send_telegram_message("📊 Статус свечей:\n" + one_time_report.strip())
            last_report_time = time.time()
            one_time_report = ""

        time.sleep(30)

# === FLASK ===
@app.route('/')
def index():
    return "🤖 Bitget бот работает!"

if __name__ == '__main__':
    send_telegram_message("🤖 Бот запущен на Render!")
    threading.Thread(target=strategy).start()
    app.run(host="0.0.0.0", port=10000)
