import time
import hmac
import hashlib
import base64
import json
import requests
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

# === TELEGRAM ===
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print("Telegram error:", e)

# === ПОДПИСЬ ДЛЯ BITGET ===
def get_bitget_headers(api_key, secret_key, passphrase, method, endpoint, body=""):
    timestamp = str(int(time.time() * 1000))
    prehash = f"{timestamp}{method.upper()}{endpoint}{body}"
    sign = hmac.new(secret_key.encode(), prehash.encode(), hashlib.sha256).digest()
    sign_b64 = base64.b64encode(sign).decode()
    return {
        "ACCESS-KEY": api_key,
        "ACCESS-SIGN": sign_b64,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json"
    }

# === СВЕЧИ ===
def get_candles(symbol):
    try:
        url = "https://api.bitget.com/api/mix/v1/market/candles"
        params = {
            "symbol": symbol,
            "granularity": "1min",
            "limit": "100",
            "productType": "umcbl"
        }
        res = requests.get(url, params=params)
        data = res.json()
        return data.get("data", [])
    except Exception as e:
        print(f"Ошибка свечей {symbol}: {e}")
        return []

# === EMA ===
def calculate_ema(prices, period):
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = prices[0]
    for price in prices[1:]:
        ema = price * k + ema * (1 - k)
    return ema

# === РАЗМЕЩЕНИЕ ОРДЕРА ===
def place_order(symbol, side):
    endpoint = "/api/mix/v1/order/placeOrder"
    url = "https://api.bitget.com" + endpoint
    timestamp = str(int(time.time() * 1000))

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
    headers = get_bitget_headers(BITGET_API_KEY, BITGET_API_SECRET, BITGET_API_PASSPHRASE, "POST", endpoint, body)

    try:
        res = requests.post(url, headers=headers, data=body)
        response = res.json()
        send_telegram(f"✅ Открыт ордер {side.upper()} по {symbol}:\n{response}")
    except Exception as e:
        send_telegram(f"❌ Ошибка размещения ордера по {symbol}: {e}")

# === АНАЛИЗ И ТОРГОВЛЯ ===
def check_signal(symbol):
    candles = get_candles(symbol)
    if not candles or len(candles) < 21:
        send_telegram(f"⚠️ Недостаточно данных по {symbol}")
        return

    try:
        closes = [float(c[4]) for c in candles[::-1]]
        ema9 = calculate_ema(closes[-9:], 9)
        ema21 = calculate_ema(closes[-21:], 21)

        if ema9 is None or ema21 is None:
            send_telegram(f"⚠️ Не удалось рассчитать EMA по {symbol}")
            return

        if ema9 > ema21:
            send_telegram(f"📈 LONG сигнал по {symbol}")
            place_order(symbol, "buy")
        elif ema9 < ema21:
            send_telegram(f"📉 SHORT сигнал по {symbol}")
            place_order(symbol, "sell")
        else:
            send_telegram(f"ℹ️ По {symbol} сейчас нет сигнала")
    except Exception as e:
        send_telegram(f"❌ Ошибка при анализе {symbol}: {e}")

# === ЗАПУСК БОТА ===
def start_bot():
    send_telegram("🤖 Бот запущен и анализирует рынок...")
    while True:
        for symbol in SYMBOLS:
            check_signal(symbol)
            time.sleep(5)
        time.sleep(30)

# === FLASK ===
app = Flask(__name__)
@app.route('/')
def index():
    return "🤖 Бот работает!"

if __name__ == '__main__':
    threading.Thread(target=start_bot).start()
    app.run(host="0.0.0.0", port=10000)
