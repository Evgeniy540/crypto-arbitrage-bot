import requests
import time
import hmac
import hashlib
import base64
import json
import threading
from datetime import datetime
from flask import Flask
import schedule

# === НАСТРОЙКИ ===
API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
API_PASSPHRASE = "Evgeniy84"

TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

SYMBOLS = ["BTCUSDT_UMCBL", "ETHUSDT_UMCBL", "SOLUSDT_UMCBL", "XRPUSDT_UMCBL", "TRXUSDT_UMCBL"]
TRADE_AMOUNT = 10.0
TP_PERCENT = 1.5
SL_PERCENT = 1.0
EMA_SHORT = 9
EMA_LONG = 21

last_signal_time = {}
last_no_signal_time = {}
POSITION_DATA = {}
REPORT_HOUR = 20
REPORT_MINUTE = 47

app = Flask(__name__)

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    except:
        pass

def sign(params, timestamp):
    content = str(timestamp) + 'GET' + '/api/mix/v1/market/history-candles' + '?' + '&'.join([f"{k}={v}" for k, v in params.items()])
    signature = hmac.new(API_SECRET.encode(), content.encode(), hashlib.sha256).hexdigest()
    return signature

def get_klines(symbol):
    url = "https://api.bitget.com/api/mix/v1/market/history-candles"
    params = {
        "symbol": symbol.replace("_", ""),
        "granularity": "60",
        "limit": "100",
        "productType": "umcbl"
    }
    headers = {
        "Content-Type": "application/json",
        "USER-AGENT": "Mozilla/5.0"
    }
    try:
        r = requests.get(url, headers=headers, params=params)
        if r.status_code == 200:
            data = r.json()
            candles = data.get("data", [])
            if candles and isinstance(candles, list):
                close_prices = [float(c[4]) for c in candles[::-1]]  # [::-1] чтобы были в порядке от старых к новым
                send_telegram(f"📊 Пришло свечей для {symbol}: {len(close_prices)}")
                return close_prices
        send_telegram(f"❗Ошибка HTTP {r.status_code} для {symbol}")
        return []
    except Exception as e:
        send_telegram(f"❗Ошибка при получении свечей для {symbol}: {str(e)}")
        return []

def calculate_ema(prices, period):
    if len(prices) < period:
        return None
    ema = prices[0]
    multiplier = 2 / (period + 1)
    for price in prices[1:]:
        ema = (price - ema) * multiplier + ema
    return ema

def place_order(symbol, side, size):
    timestamp = str(int(time.time() * 1000))
    url = "https://api.bitget.com/api/mix/v1/order/placeOrder"
    body = {
        "symbol": symbol.replace("_", ""),
        "marginCoin": "USDT",
        "size": str(size),
        "side": side,
        "orderType": "market",
        "productType": "umcbl"
    }
    message = timestamp + "POST" + "/api/mix/v1/order/placeOrder" + json.dumps(body)
    sign_header = hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    headers = {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": sign_header,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": API_PASSPHRASE,
        "Content-Type": "application/json"
    }
    response = requests.post(url, headers=headers, data=json.dumps(body))
    return response.json()

def check_signal(symbol):
    global TRADE_AMOUNT
    candles = get_klines(symbol)
    if len(candles) < EMA_LONG:
        send_telegram(f"❗ Недостаточно данных для {symbol}")
        return

    ema9 = calculate_ema(candles[-EMA_SHORT:], EMA_SHORT)
    ema21 = calculate_ema(candles[-EMA_LONG:], EMA_LONG)

    if ema9 is None or ema21 is None:
        send_telegram(f"❗ EMA не рассчитано для {symbol}")
        return

    now = time.time()
    last_sent = last_no_signal_time.get(symbol, 0)
    if ema9 > ema21:
        if symbol not in POSITION_DATA:
            result = place_order(symbol, "open_long", TRADE_AMOUNT)
            send_telegram(f"✅ Открыт LONG по {symbol} на {TRADE_AMOUNT} USDT\nОтвет: {result}")
            POSITION_DATA[symbol] = {"entry_price": candles[-1], "side": "long"}
    else:
        if now - last_sent > 3600:
            send_telegram(f"ℹ️ По {symbol} сейчас нет сигнала")
            last_no_signal_time[symbol] = now

def daily_report():
    msg = "📅 Ежедневный отчёт:\n"
    for sym, data in POSITION_DATA.items():
        msg += f"{sym}: {data}\n"
    msg += f"Текущая сумма сделки: {TRADE_AMOUNT} USDT"
    send_telegram(msg)

def run_bot():
    while True:
        for symbol in SYMBOLS:
            try:
                check_signal(symbol)
            except Exception as e:
                send_telegram(f"Ошибка при проверке {symbol}: {str(e)}")
        time.sleep(30)

@app.route('/')
def index():
    return "Bot is running!"

if __name__ == '__main__':
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=10000), daemon=True).start()
    schedule.every().day.at(f"{REPORT_HOUR:02d}:{REPORT_MINUTE:02d}").do(daily_report)
    threading.Thread(target=run_bot, daemon=True).start()

    while True:
        schedule.run_pending()
        time.sleep(1)
