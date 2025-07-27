import time
import requests
import hmac
import hashlib
import json
import base64
from flask import Flask
import threading

# === КЛЮЧИ И НАСТРОЙКИ ===
BITGET_API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"
TRADE_AMOUNT = 10
SYMBOLS = ["btcusdt_UMCBL", "ethusdt_UMCBL", "solusdt_UMCBL", "xrpusdt_UMCBL", "trxusdt_UMCBL"]
TP_PERCENT = 0.015
SL_PERCENT = 0.01
EMA_SHORT = 9
EMA_LONG = 21

positions = {}

# === Telegram уведомления ===
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=data)
    except:
        pass

# === Подпись Bitget ===
def sign_request(timestamp, method, path, body=""):
    prehash = f"{timestamp}{method}{path}{body}"
    sign = hmac.new(BITGET_API_SECRET.encode(), prehash.encode(), hashlib.sha256).hexdigest()
    return sign

# === Получение свечей ===
def get_candles(symbol):
    url = f"https://api.bitget.com/api/mix/v1/market/candles?symbol={symbol}&granularity=60"
    try:
        res = requests.get(url).json()
        return [[float(x[1]), float(x[4])] for x in res.get("data", [])][-EMA_LONG:]
    except:
        return []

# === EMA стратегия ===
def calculate_ema(prices, period):
    k = 2 / (period + 1)
    ema = prices[0]
    for price in prices[1:]:
        ema = price * k + ema * (1 - k)
    return ema

def check_signal(symbol):
    candles = get_candles(symbol)
    if len(candles) < EMA_LONG:
        return None, None
    closes = [c[1] for c in candles]
    ema_short = calculate_ema(closes[-EMA_SHORT:], EMA_SHORT)
    ema_long = calculate_ema(closes, EMA_LONG)
    print(f"{symbol} | EMA{EMA_SHORT}: {ema_short:.2f} | EMA{EMA_LONG}: {ema_long:.2f}")
    if ema_short > ema_long:
        return "long", closes[-1]
    elif ema_short < ema_long:
        return "short", closes[-1]
    return None, None

# === Размещение ордера ===
def place_order(symbol, side):
    timestamp = str(int(time.time() * 1000))
    path = "/api/mix/v1/order/placeOrder"
    body = {
        "symbol": symbol.upper(),
        "marginCoin": "USDT",
        "size": str(TRADE_AMOUNT),
        "side": side,
        "orderType": "market",
        "tradeSide": side,
        "productType": "UMCBL"
    }
    body_json = json.dumps(body)
    sign = sign_request(timestamp, "POST", path, body_json)
    headers = {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json"
    }
    url = "https://api.bitget.com" + path
    res = requests.post(url, headers=headers, data=body_json).json()
    print(f"Bitget response for {symbol} [{side.upper()}]: {res}")
    return res

# === Получение последней цены ===
def get_price(symbol):
    url = f"https://api.bitget.com/api/mix/v1/market/ticker?symbol={symbol}"
    try:
        res = requests.get(url).json()
        return float(res["data"]["last"])
    except:
        return None

# === Торговая логика ===
def trade():
    while True:
        for symbol in SYMBOLS:
            signal, price = check_signal(symbol)
            if not signal or not price:
                continue

            if symbol not in positions:
                print(f"{symbol}: signal {signal.upper()} | price: {price}")
                res = place_order(symbol, signal)
                if "code" in res:
                    if res["code"] == "00000":
                        positions[symbol] = {
                            "side": signal,
                            "entry": price
                        }
                        send_telegram(f"📈 Открыта {signal.upper()} сделка по {symbol} по цене {price}")
                    else:
                        send_telegram(f"❌ Ошибка при открытии сделки по {symbol}:\n{res}")
            else:
                current_price = get_price(symbol)
                if not current_price:
                    continue

                entry = positions[symbol]["entry"]
                side = positions[symbol]["side"]
                tp_price = entry * (1 + TP_PERCENT) if side == "long" else entry * (1 - TP_PERCENT)
                sl_price = entry * (1 - SL_PERCENT) if side == "long" else entry * (1 + SL_PERCENT)

                if (side == "long" and (current_price >= tp_price or current_price <= sl_price)) or \
                   (side == "short" and (current_price <= tp_price or current_price >= sl_price)):
                    close_side = "close_long" if side == "long" else "close_short"
                    res = place_order(symbol, close_side)
                    if "code" in res:
                        if res["code"] == "00000":
                            profit = round((current_price - entry) * TRADE_AMOUNT, 3)
                            send_telegram(f"✅ Сделка {side.upper()} по {symbol} закрыта по цене {current_price} (вход {entry})\n📊 Прибыль: {profit} USDT")
                            del positions[symbol]
                        else:
                            send_telegram(f"❌ Ошибка при закрытии сделки по {symbol}:\n{res}")
        time.sleep(60)

# === Flask-сервер ===
app = Flask(__name__)
@app.route("/")
def home():
    return "🤖 Bitget Futures Trading Bot работает!"

# === Запуск ===
def run_all():
    threading.Thread(target=trade).start()
    app.run(host="0.0.0.0", port=8080)

if __name__ == "__main__":
    send_telegram("🤖 Бот запущен и готов к торговле!")
    run_all()
