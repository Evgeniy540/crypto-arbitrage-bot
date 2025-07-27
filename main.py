import time
import hmac
import hashlib
import base64
import requests
import json
from datetime import datetime
from flask import Flask
import threading

# === НАСТРОЙКИ ===
BITGET_API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"
SYMBOLS = ["BTCUSDT_UMCBL", "ETHUSDT_UMCBL", "SOLUSDT_UMCBL", "XRPUSDT_UMCBL", "TRXUSDT_UMCBL"]

TRADE_AMOUNT = 10
PROFIT = 0.0
last_signal_time = {}
last_report_time = ""
lock = threading.Lock()

app = Flask(__name__)

# === TELEGRAM ===
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print(f"Ошибка Telegram: {e}")

# === EMA ===
def calculate_ema(data, period):
    if len(data) < period:
        return []
    ema = []
    k = 2 / (period + 1)
    ema.append(sum(data[:period]) / period)
    for price in data[period:]:
        ema.append((price - ema[-1]) * k + ema[-1])
    return ema

# === Bitget CANDLES ===
def get_candles(symbol):
    url = "https://api.bitget.com/api/mix/v1/market/history-candles"
    params = {
        "symbol": symbol,
        "granularity": "1min",
        "limit": "100",
        "productType": "umcbl"
    }
    try:
        response = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"})
        data = response.json()
        if "data" in data and isinstance(data["data"], list):
            return list(reversed(data["data"]))
        else:
            return None
    except Exception as e:
        print(f"Ошибка получения свечей {symbol}: {e}")
        return None

# === SIGNATURE ===
def sign_request(timestamp, method, endpoint, body=""):
    prehash = f"{timestamp}{method.upper()}{endpoint}{body}"
    sign = hmac.new(BITGET_API_SECRET.encode(), prehash.encode(), hashlib.sha256).digest()
    return base64.b64encode(sign).decode()

# === ORDER ===
def place_order(symbol, side):
    global TRADE_AMOUNT, PROFIT
    timestamp = str(int(time.time() * 1000))
    endpoint = "/api/mix/v1/order/placeOrder"
    url = f"https://api.bitget.com{endpoint}"

    data = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "size": str(TRADE_AMOUNT),
        "side": "open_long" if side == "buy" else "open_short",
        "orderType": "market",
        "productType": "umcbl"
    }

    body = json.dumps(data)
    headers = {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign_request(timestamp, "POST", endpoint, body),
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(url, headers=headers, data=body)
        result = response.json()
        if result.get("code") == "00000":
            PROFIT += TRADE_AMOUNT * 0.015
            TRADE_AMOUNT += round(TRADE_AMOUNT * 0.015, 2)
            send_telegram(f"✅ Открыт ордер {side.upper()} {symbol} на {TRADE_AMOUNT}$")
        else:
            send_telegram(f"❌ Ошибка ордера {symbol}: {result}")
    except Exception as e:
        send_telegram(f"⚠️ Ошибка при размещении ордера {symbol}: {e}")

# === СТРАТЕГИЯ ===
def check_symbol(symbol):
    global last_signal_time
    candles = get_candles(symbol)
    if candles is None or len(candles) < 21:
        now = time.time()
        if time.time() - last_signal_time.get(symbol, 0) > 3600:
            send_telegram(f"⛔ Недостаточно данных по {symbol} ({len(candles) if candles else 0} свечей)")
            last_signal_time[symbol] = now
        return

    try:
        close_prices = [float(c[4]) for c in candles if c[4] is not None]
        ema9 = calculate_ema(close_prices, 9)
        ema21 = calculate_ema(close_prices, 21)

        if not ema9 or not ema21:
            return

        if ema9[-1] > ema21[-1]:
            place_order(symbol, "buy")
        elif ema9[-1] < ema21[-1]:
            place_order(symbol, "sell")
        else:
            if time.time() - last_signal_time.get(symbol, 0) > 3600:
                send_telegram(f"ℹ️ По {symbol} сейчас нет сигнала")
                last_signal_time[symbol] = time.time()
    except Exception as e:
        send_telegram(f"❌ Ошибка анализа {symbol}: {e}")

# === ОСНОВНОЙ ЦИКЛ ===
def bot_loop():
    global last_report_time
    send_telegram("🤖 Бот запущен и работает на Render!")
    while True:
        try:
            now = datetime.now()
            for symbol in SYMBOLS:
                check_symbol(symbol)
                time.sleep(2)

            # Ежедневный отчёт
            current_time_str = now.strftime("%H:%M")
            if current_time_str == "20:47" and last_report_time != now.strftime("%Y-%m-%d"):
                send_telegram(f"📊 Ежедневный отчёт:\nПрибыль: {round(PROFIT, 2)}$\nСумма сделки: {round(TRADE_AMOUNT, 2)}$")
                last_report_time = now.strftime("%Y-%m-%d")

            time.sleep(30)
        except Exception as e:
            send_telegram(f"⚠️ Цикл остановлен с ошибкой: {e}")
            time.sleep(60)

# === FLASK для Render ===
@app.route("/")
def index():
    return "✅ Бот работает на Render"

if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
