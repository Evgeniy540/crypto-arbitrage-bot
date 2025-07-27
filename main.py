import requests
import time
import hmac
import hashlib
import json
from flask import Flask
import threading

# === КЛЮЧИ И НАСТРОЙКИ ===
API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
API_PASSPHRASE = "Evgeniy84"
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"
TRADE_AMOUNT = 10  # USDT
SYMBOLS = ["BTCUSDT_UMCBL", "ETHUSDT_UMCBL", "SOLUSDT_UMCBL", "XRPUSDT_UMCBL", "TRXUSDT_UMCBL"]

positions = {}
last_message_time = 0

# === Telegram ===
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=data)
    except:
        pass

# === Получение свечей ===
def get_candles(symbol):
    try:
        url = f"https://api.bitget.com/api/mix/v1/market/candles?symbol={symbol}&granularity=1min&limit=100"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            send_telegram(f"❗Ошибка HTTP {r.status_code} для {symbol}")
            return []
        data = r.json()
        if not data or "data" not in data or not isinstance(data["data"], list):
            send_telegram(f"⚠️ Пустой или некорректный ответ от Bitget для {symbol}")
            return []
        candles = data["data"]
        return list(reversed(candles)) if len(candles) >= 21 else []
    except Exception as e:
        send_telegram(f"⚠️ Ошибка получения свечей {symbol}: {str(e)}")
        return []

# === EMA ===
def calculate_ema(data, period):
    prices = [float(c[4]) for c in data]
    k = 2 / (period + 1)
    ema = prices[0]
    for price in prices[1:]:
        ema = price * k + ema * (1 - k)
    return ema

# === Подпись запроса ===
def sign_request(timestamp, method, path, body=''):
    message = f"{timestamp}{method}{path}{body}"
    signature = hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    return signature

# === Открытие позиции ===
def open_position(symbol, side):
    timestamp = str(int(time.time() * 1000))
    path = "/api/mix/v1/order/placeOrder"
    url = "https://api.bitget.com" + path
    order = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "size": str(TRADE_AMOUNT),
        "side": side,
        "orderType": "market",
        "tradeSide": "open",
        "productType": "umcbl"
    }
    body = json.dumps(order)
    headers = {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": sign_request(timestamp, "POST", path, body),
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": API_PASSPHRASE,
        "Content-Type": "application/json"
    }
    try:
        r = requests.post(url, headers=headers, data=body)
        res = r.json()
        if res.get("code") == "00000":
            entry_price = float(get_last_price(symbol))
            positions[symbol] = {"side": side, "entry": entry_price}
            send_telegram(f"✅ Открыта позиция {side} по {symbol} по цене {entry_price}")
        else:
            send_telegram(f"❌ Ошибка открытия позиции {symbol}: {res}")
    except Exception as e:
        send_telegram(f"❌ Ошибка открытия {symbol}: {str(e)}")

# === Последняя цена ===
def get_last_price(symbol):
    try:
        url = f"https://api.bitget.com/api/mix/v1/market/ticker?symbol={symbol}"
        r = requests.get(url, timeout=5)
        data = r.json()
        return data["data"]["last"]
    except:
        return "0"

# === Закрытие позиции ===
def close_position(symbol):
    if symbol not in positions:
        return
    side = "close_long" if positions[symbol]["side"] == "buy" else "close_short"
    timestamp = str(int(time.time() * 1000))
    path = "/api/mix/v1/order/placeOrder"
    url = "https://api.bitget.com" + path
    order = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "size": str(TRADE_AMOUNT),
        "side": "sell" if positions[symbol]["side"] == "buy" else "buy",
        "orderType": "market",
        "tradeSide": "close",
        "productType": "umcbl"
    }
    body = json.dumps(order)
    headers = {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": sign_request(timestamp, "POST", path, body),
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": API_PASSPHRASE,
        "Content-Type": "application/json"
    }
    try:
        r = requests.post(url, headers=headers, data=body)
        res = r.json()
        if res.get("code") == "00000":
            exit_price = float(get_last_price(symbol))
            entry = positions[symbol]["entry"]
            profit = round((exit_price - entry) / entry * 100, 2) if positions[symbol]["side"] == "buy" else round((entry - exit_price) / entry * 100, 2)
            send_telegram(f"💰 Закрыта позиция по {symbol} на цене {exit_price}. Прибыль: {profit}%")
            del positions[symbol]
        else:
            send_telegram(f"❌ Ошибка закрытия {symbol}: {res}")
    except Exception as e:
        send_telegram(f"❌ Ошибка закрытия {symbol}: {str(e)}")

# === Основная логика ===
def run_bot():
    global last_message_time
    while True:
        now = time.time()
        for symbol in SYMBOLS:
            candles = get_candles(symbol)
            if len(candles) < 21:
                if now - last_message_time > 3600:
                    send_telegram(f"⚠️ Недостаточно данных для {symbol} ({len(candles)} свечей)")
                    last_message_time = now
                continue
            ema9 = calculate_ema(candles[-9:], 9)
            ema21 = calculate_ema(candles[-21:], 21)
            last = float(candles[-1][4])
            if symbol not in positions:
                if ema9 > ema21:
                    open_position(symbol, "buy")
                elif ema9 < ema21:
                    open_position(symbol, "sell")
                else:
                    if now - last_message_time > 3600:
                        send_telegram(f"⏸ По {symbol} сейчас нет сигнала")
                        last_message_time = now
            else:
                entry = positions[symbol]["entry"]
                if positions[symbol]["side"] == "buy":
                    if last >= entry * 1.015 or last <= entry * 0.99:
                        close_position(symbol)
                elif positions[symbol]["side"] == "sell":
                    if last <= entry * 0.985 or last >= entry * 1.01:
                        close_position(symbol)
        time.sleep(30)

# === Flask для Render ===
app = Flask(__name__)
@app.route('/')
def home():
    return '🤖 Bitget bot is running!'

# === Стартуем ===
if __name__ == '__main__':
    threading.Thread(target=run_bot).start()
    send_telegram("🤖 Бот запущен на Render!")
    app.run(host='0.0.0.0', port=10000)
