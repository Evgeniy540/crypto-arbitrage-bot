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
TRADE_AMOUNT = 10  # в USDT
SYMBOLS = [
    "BTCUSDT_UMCBL", "ETHUSDT_UMCBL", "SOLUSDT_UMCBL", "XRPUSDT_UMCBL", "TRXUSDT_UMCBL",
    "APEUSDT_UMCBL", "ARBUSDT_UMCBL", "GALAUSDT_UMCBL", "DOGEUSDT_UMCBL", "SUIUSDT_UMCBL"
]
POSITIONS = {}
PROFIT = 0

# === TELEGRAM ===
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text})

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

# === BITGET ===
def get_candles(symbol):
    try:
        url = "https://api.bitget.com/api/mix/v1/market/candles"
        params = {"symbol": symbol, "granularity": "1min", "limit": "100"}
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, params=params, headers=headers)
        data = r.json()
        return data["data"] if "data" in data else None
    except Exception as e:
        send_telegram(f"❌ Ошибка свечей {symbol}: {e}")
        return None

def get_price(symbol):
    candles = get_candles(symbol)
    if candles and len(candles) > 0:
        return float(candles[0][4])
    return None

def place_order(symbol, side):
    url = "https://api.bitget.com/api/mix/v1/order/placeOrder"
    timestamp = str(int(time.time() * 1000))
    body = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "size": str(TRADE_AMOUNT),
        "side": side,
        "orderType": "market",
        "tradeSide": "long" if side == "buy" else "short",
        "productType": "umcbl"
    }
    prehash = f"{timestamp}POST/api/mix/v1/order/placeOrder{json.dumps(body)}"
    sign = hmac.new(BITGET_API_SECRET.encode(), prehash.encode(), hashlib.sha256).digest()
    sign_b64 = base64.b64encode(sign).decode()
    headers = {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign_b64,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json"
    }
    try:
        r = requests.post(url, headers=headers, data=json.dumps(body))
        resp = r.json()
        send_telegram(f"✅ Ордер {side.upper()} {symbol}: {resp}")
        return get_price(symbol)
    except Exception as e:
        send_telegram(f"❌ Ошибка ордера {symbol}: {e}")
        return None

# === ОСНОВНАЯ ЛОГИКА ===
def strategy():
    global PROFIT
    while True:
        for symbol in SYMBOLS:
            candles = get_candles(symbol)
            if not candles or len(candles) < 21:
                send_telegram(f"❗ Недостаточно данных по {symbol}")
                continue
            try:
                close_prices = [float(c[4]) for c in candles[::-1]]
                ema9 = calculate_ema(close_prices, 9)
                ema21 = calculate_ema(close_prices, 21)

                if ema9[-1] > ema21[-1]:
                    if symbol not in POSITIONS:
                        entry_price = place_order(symbol, "buy")
                        if entry_price:
                            POSITIONS[symbol] = {"entry": entry_price, "side": "long"}
                    else:
                        entry = POSITIONS[symbol]["entry"]
                        current = get_price(symbol)
                        if current >= entry * 1.015:
                            PROFIT += (current - entry) * TRADE_AMOUNT / entry
                            send_telegram(f"📈 TP {symbol} по LONG — +1.5%")
                            del POSITIONS[symbol]
                        elif current <= entry * 0.99:
                            PROFIT -= (entry - current) * TRADE_AMOUNT / entry
                            send_telegram(f"⚠️ SL {symbol} по LONG — -1%")
                            del POSITIONS[symbol]
                elif ema9[-1] < ema21[-1]:
                    if symbol not in POSITIONS:
                        entry_price = place_order(symbol, "sell")
                        if entry_price:
                            POSITIONS[symbol] = {"entry": entry_price, "side": "short"}
                    else:
                        entry = POSITIONS[symbol]["entry"]
                        current = get_price(symbol)
                        if current <= entry * 0.985:
                            PROFIT += (entry - current) * TRADE_AMOUNT / entry
                            send_telegram(f"📉 TP {symbol} по SHORT — +1.5%")
                            del POSITIONS[symbol]
                        elif current >= entry * 1.01:
                            PROFIT -= (current - entry) * TRADE_AMOUNT / entry
                            send_telegram(f"⚠️ SL {symbol} по SHORT — -1%")
                            del POSITIONS[symbol]
                else:
                    send_telegram(f"ℹ️ По {symbol} сейчас нет сигнала")
            except Exception as e:
                send_telegram(f"❌ Ошибка анализа {symbol}: {e}")
            time.sleep(5)
        time.sleep(30)

# === TELEGRAM HANDLER ===
app = Flask(__name__)
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json()
    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"]["text"]
        if text == "/profit":
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": chat_id, "text": f"📊 Общая прибыль: {round(PROFIT, 2)} USDT"}
            )
    return "ok"

@app.route("/")
def index():
    return "🤖 Бот работает на Render!"

if __name__ == "__main__":
    threading.Thread(target=strategy).start()
    app.run(host="0.0.0.0", port=10000)
