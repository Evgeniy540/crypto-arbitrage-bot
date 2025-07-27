import time
import hmac
import hashlib
import base64
import requests
import json
import logging
from flask import Flask
import threading
import numpy as np
import os

# === API ключи ===
API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
API_PASSPHRASE = "Evgeniy84"
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

# === Настройки ===
TRADE_SYMBOLS = ["btcusdt_UMCBL", "ethusdt_UMCBL", "solusdt_UMCBL", "xrpusdt_UMCBL", "trxusdt_UMCBL"]
INTERVAL = "1m"
TRADE_AMOUNT = 10
EMA_FAST = 9
EMA_SLOW = 21
TP_PERCENT = 1.5
SL_PERCENT = 1.0
COOLDOWN = 60 * 60 * 6
last_trade_time = {}

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        logging.error(f"Ошибка отправки Telegram: {e}")

def get_klines(symbol):
    url = f"https://api.bitget.com/api/mix/v1/market/candles?symbol={symbol}&granularity={INTERVAL}&limit=100"
    try:
        res = requests.get(url).json()
        if 'data' not in res or not res['data']:
            return None
        closes = [float(k[4]) for k in res['data']]
        return closes[::-1]
    except Exception as e:
        logging.error(f"Ошибка получения свечей: {e}")
        return None

def calc_ema(data, period):
    if len(data) < period:
        return None
    return np.convolve(data, np.ones(period)/period, mode='valid')

def place_order(symbol, side):
    timestamp = str(int(time.time() * 1000))
    url = "https://api.bitget.com/api/mix/v1/order/place"
    body = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "size": str(TRADE_AMOUNT),
        "side": side,
        "orderType": "market",
        "tradeSide": "open",
        "productType": "umcbl"
    }
    msg = timestamp + "POST" + "/api/mix/v1/order/place" + json.dumps(body)
    sign = base64.b64encode(hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).digest()).decode()
    headers = {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": API_PASSPHRASE,
        "Content-Type": "application/json"
    }
    try:
        res = requests.post(url, json=body, headers=headers).json()
        logging.info(res)
        return res
    except Exception as e:
        logging.error(f"Ошибка размещения ордера: {e}")
        return None

def trade_logic():
    while True:
        for symbol in TRADE_SYMBOLS:
            now = time.time()
            if symbol in last_trade_time and now - last_trade_time[symbol] < COOLDOWN:
                continue
            klines = get_klines(symbol)
            if not klines or len(klines) < EMA_SLOW + 1:
                logging.warning(f"Недостаточно данных для {symbol}")
                send_telegram(f"⚠️ Нет данных по {symbol.upper()}")
                continue
            ema_fast = calc_ema(klines, EMA_FAST)
            ema_slow = calc_ema(klines, EMA_SLOW)
            if ema_fast is None or ema_slow is None:
                continue
            if ema_fast[-1] > ema_slow[-1] and ema_fast[-2] <= ema_slow[-2]:
                side = "buy"
                response = place_order(symbol, side)
                send_telegram(f"📈 LONG-сигнал по {symbol.upper()}! Ордер: {response}")
                last_trade_time[symbol] = now
            elif ema_fast[-1] < ema_slow[-1] and ema_fast[-2] >= ema_slow[-2]:
                side = "sell"
                response = place_order(symbol, side)
                send_telegram(f"📉 SHORT-сигнал по {symbol.upper()}! Ордер: {response}")
                last_trade_time[symbol] = now
        time.sleep(30)

@app.route('/')
def home():
    return "Bitget Futures Trading Bot is running!"

def start_bot():
    send_telegram("🤖 Бот запущен и ждёт сигнала по фьючерсам Bitget!")
    trade_logic()

if __name__ == "__main__":
    threading.Thread(target=start_bot).start()
    app.run(host="0.0.0.0", port=10000)
