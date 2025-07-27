import requests
import time
import hmac
import hashlib
import json
import schedule
from datetime import datetime
from flask import Flask
import threading

# === КЛЮЧИ И НАСТРОЙКИ ===
BITGET_API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"
SYMBOLS = ["BTCUSDT_UMCBL", "ETHUSDT_UMCBL", "SOLUSDT_UMCBL", "XRPUSDT_UMCBL", "TRXUSDT_UMCBL"]
TRADE_AMOUNT = 10.0  # начальная сумма сделки
TP_PERCENT = 1.5
SL_PERCENT = 1.0
last_signal_time = {}
last_profit = 0
no_signal_log_time = {}

# === ФУНКЦИИ ===
def send_telegram(text):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    except:
        pass

def get_server_time():
    try:
        res = requests.get("https://api.bitget.com/api/mix/v1/market/time")
        return int(res.json()["data"])
    except:
        return int(time.time() * 1000)

def get_candles(symbol):
    url = f"https://api.bitget.com/api/mix/v1/market/history-candles"
    params = {
        "symbol": symbol.replace("_UMCBL", ""),
        "granularity": "60",
        "limit": "100",
        "productType": "umcbl"
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, params=params, headers=headers)
        data = res.json()
        if 'data' in data and isinstance(data['data'], list):
            return [list(map(float, candle)) for candle in data['data']]
        else:
            return []
    except:
        return []

def calculate_ema(prices, period):
    multiplier = 2 / (period + 1)
    ema = prices[0]
    for price in prices[1:]:
        ema = (price - ema) * multiplier + ema
    return ema

def place_order(symbol, side):
    global TRADE_AMOUNT
    timestamp = str(get_server_time())
    body = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "size": str(TRADE_AMOUNT),
        "side": side,
        "orderType": "market",
        "productType": "umcbl"
    }
    body_json = json.dumps(body)
    message = timestamp + "POST" + "/api/mix/v1/order/placeOrder" + body_json
    signature = hmac.new(BITGET_API_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    headers = {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": signature,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json"
    }
    res = requests.post("https://api.bitget.com/api/mix/v1/order/placeOrder", headers=headers, data=body_json)
    send_telegram(f"📈 Открыта позиция {side} по {symbol} на {TRADE_AMOUNT} USDT\n\nОтвет: {res.text}")

def check_signal(symbol):
    global last_signal_time, last_profit, TRADE_AMOUNT
    candles = get_candles(symbol)
    if len(candles) < 21:
        send_telegram(f"❗ Недостаточно данных для {symbol} ({len(candles)} свечей)")
        return

    closes = [c[4] for c in candles][-100:]
    ema9 = calculate_ema(closes[-9:], 9)
    ema21 = calculate_ema(closes[-21:], 21)

    now = time.time()
    if symbol in last_signal_time and now - last_signal_time[symbol] < 3600:
        return  # Cooldown 1 час

    if ema9 > ema21:
        place_order(symbol, "open_long")
        last_signal_time[symbol] = now
        entry_price = closes[-1]
        tp_price = entry_price * (1 + TP_PERCENT / 100)
        sl_price = entry_price * (1 - SL_PERCENT / 100)
        send_telegram(f"🎯 EMA сигнал для {symbol}\nЦена входа: {entry_price:.4f}\nTP: {tp_price:.4f} (+{TP_PERCENT}%)\nSL: {sl_price:.4f} (-{SL_PERCENT}%)")
        TRADE_AMOUNT = round(TRADE_AMOUNT * 1.015, 2)
        last_profit += TRADE_AMOUNT * 0.015
    else:
        last_hour = no_signal_log_time.get(symbol, 0)
        if now - last_hour > 3600:
            send_telegram(f"ℹ️ По {symbol} сейчас нет сигнала")
            no_signal_log_time[symbol] = now

def run_strategy():
    for symbol in SYMBOLS:
        check_signal(symbol)

def send_daily_report():
    global last_profit
    send_telegram(f"📊 Ежедневный отчёт:\nСумма сделки: {TRADE_AMOUNT} USDT\nПрибыль: {last_profit:.2f} USDT")

# === FLASK СЕРВЕР ===
app = Flask(__name__)

@app.route('/')
def home():
    return '✅ Бот запущен и работает на Render!'

def start_flask():
    app.run(host='0.0.0.0', port=10000)

# === ЗАПУСК ===
def main():
    send_telegram("🤖 Бот успешно запущен на Render!")
    schedule.every(30).seconds.do(run_strategy)
    schedule.every().day.at("20:47").do(send_daily_report)
    threading.Thread(target=start_flask).start()
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
