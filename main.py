import time
import requests
import hmac
import hashlib
import base64
import json
import threading
from flask import Flask
from datetime import datetime
import logging

# === НАСТРОЙКИ ===
API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
API_PASSPHRASE = "Evgeniy84"
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT"]
TRADE_AMOUNT = 5
EMA_SHORT = 9
EMA_LONG = 21
TP_PERCENT = 1.5
SL_PERCENT = 1.0
CHECK_INTERVAL = 30
last_signal_time = {}
message_sent_time = {}
DAILY_REPORT_HOUR = 20
DAILY_REPORT_MINUTE = 47
profit_total = 0.0

app = Flask(__name__)

def send_telegram(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    except Exception as e:
        print("Ошибка Telegram:", e)

def sign_request(method, url, params=None, body=None):
    timestamp = str(int(time.time() * 1000))
    body_str = json.dumps(body) if body else ""
    prehash = timestamp + method + url + body_str
    sign = hmac.new(API_SECRET.encode(), prehash.encode(), hashlib.sha256).hexdigest()
    headers = {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": API_PASSPHRASE,
        "Content-Type": "application/json"
    }
    return headers

def get_candles(symbol):
    try:
        url = f"/api/spot/v1/market/candles?symbol={symbol}&granularity=1m&limit=100"
        base_url = "https://api.bitget.com"
        headers = sign_request("GET", url)
        response = requests.get(base_url + url, headers=headers)
        data = response.json()
        if "data" in data:
            candles = [[float(x[1]), float(x[4])] for x in data["data"]][::-1]
            return candles
        else:
            return []
    except Exception as e:
        return []

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
            ema.append(price * k + ema[-1] * (1 - k))
    return ema

def place_order(symbol, side, amount):
    try:
        url = "/api/spot/v1/trade/orders"
        base_url = "https://api.bitget.com"
        body = {
            "symbol": symbol,
            "side": side,
            "orderType": "market",
            "size": str(amount)
        }
        headers = sign_request("POST", url, body=body)
        response = requests.post(base_url + url, headers=headers, data=json.dumps(body))
        data = response.json()
        return data
    except Exception as e:
        return None

def get_balance():
    url = "/api/spot/v1/account/assets"
    base_url = "https://api.bitget.com"
    headers = sign_request("GET", url)
    response = requests.get(base_url + url, headers=headers)
    balances = response.json().get("data", [])
    result = {item["coin"]: float(item["available"]) for item in balances}
    return result

def check_and_trade():
    global TRADE_AMOUNT, profit_total
    now = datetime.utcnow()
    for symbol in SYMBOLS:
        candles = get_candles(symbol)
        if len(candles) < EMA_LONG:
            if message_sent_time.get(symbol, 0) < time.time() - 3600:
                send_telegram(f"Недостаточно данных по {symbol}")
                message_sent_time[symbol] = time.time()
            continue
        closes = [c[1] for c in candles]
        ema_short = calculate_ema(closes, EMA_SHORT)
        ema_long = calculate_ema(closes, EMA_LONG)
        if ema_short[-1] > ema_long[-1] and ema_short[-2] <= ema_long[-2]:
            balance = get_balance()
            if balance.get("USDT", 0) < TRADE_AMOUNT:
                send_telegram(f"Недостаточно USDT для торговли: {balance.get('USDT', 0):.2f}")
                continue
            entry_price = closes[-1]
            quantity = round(TRADE_AMOUNT / entry_price, 6)
            result = place_order(symbol, "buy", quantity)
            if result and result.get("code") == "00000":
                send_telegram(f"🟢 Куплено {quantity} {symbol} по {entry_price}")
                take_profit = entry_price * (1 + TP_PERCENT / 100)
                stop_loss = entry_price * (1 - SL_PERCENT / 100)
                for _ in range(120):  # максимум 1 час ожидания
                    candles_new = get_candles(symbol)
                    if not candles_new:
                        time.sleep(30)
                        continue
                    price_now = candles_new[-1][1]
                    if price_now >= take_profit:
                        result_sell = place_order(symbol, "sell", quantity)
                        if result_sell and result_sell.get("code") == "00000":
                            profit = (price_now - entry_price) * quantity
                            profit_total += profit
                            TRADE_AMOUNT += profit
                            send_telegram(f"✅ TP по {symbol} по {price_now:.4f}, прибыль: {profit:.4f} USDT")
                        break
                    elif price_now <= stop_loss:
                        result_sell = place_order(symbol, "sell", quantity)
                        if result_sell and result_sell.get("code") == "00000":
                            loss = (entry_price - price_now) * quantity
                            profit_total -= loss
                            send_telegram(f"🔴 SL по {symbol} по {price_now:.4f}, убыток: {loss:.4f} USDT")
                        break
                    time.sleep(30)
        else:
            if last_signal_time.get(symbol, 0) < time.time() - 3600:
                send_telegram(f"По {symbol} сейчас нет сигнала")
                last_signal_time[symbol] = time.time()

def schedule_loop():
    while True:
        try:
            check_and_trade()
        except Exception as e:
            send_telegram(f"Ошибка в check_and_trade: {e}")
        now = datetime.now()
        if now.hour == DAILY_REPORT_HOUR and now.minute == DAILY_REPORT_MINUTE:
            send_telegram(f"📊 Ежедневный отчёт: общая прибыль: {profit_total:.2f} USDT, сумма сделки: {TRADE_AMOUNT:.2f}")
            time.sleep(60)
        time.sleep(CHECK_INTERVAL)

@app.route('/')
def home():
    return "Bitget Spot Trading Bot is running!"

if __name__ == '__main__':
    threading.Thread(target=schedule_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=8080)
