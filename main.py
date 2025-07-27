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

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print(f"Ошибка Telegram: {e}")

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

def get_candles(symbol):
    url = "https://api.bitget.com/api/mix/v1/market/history-candles"
    params = {
        "symbol": symbol,
        "granularity": "1min",
        "limit": "100",
        "productType": "umcbl"
    }
    try:
        response = requests.get(url, params=params)
        data = response.json()
        return data['data'] if 'data' in data else None
    except Exception as e:
        print(f"Ошибка при получении свечей {symbol}: {e}")
        return None

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

def place_order(symbol, side):
    url = "/api/mix/v1/order/placeOrder"
    full_url = "https://api.bitget.com" + url
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
    body_str = json.dumps(body)
    headers = get_bitget_headers(BITGET_API_KEY, BITGET_API_SECRET, BITGET_API_PASSPHRASE, "POST", url, body_str)
    try:
        response = requests.post(full_url, headers=headers, data=body_str)
        result = response.json()
        send_telegram_message(f"✅ Ордер {side.upper()} {symbol}: {result}")
    except Exception as e:
        send_telegram_message(f"⚠️ Ошибка ордера {side.upper()} {symbol}: {e}")

def strategy():
    while True:
        for symbol in SYMBOLS:
            candles = get_candles(symbol)
            if not candles:
                send_telegram_message(f"⚠️ Не удалось получить свечи по {symbol}.")
                continue

            try:
                close_prices = [float(c[4]) for c in candles if c[4] is not None]
                if len(close_prices) < 21:
                    send_telegram_message(f"⚠️ Недостаточно данных по {symbol}")
                    continue

                ema9 = calculate_ema(close_prices, 9)
                ema21 = calculate_ema(close_prices, 21)

                if ema9[-1] is None or ema21[-1] is None:
                    send_telegram_message(f"❌ EMA не рассчитан по {symbol}")
                    continue

                send_telegram_message(f"📊 {symbol}: EMA9={ema9[-1]:.4f}, EMA21={ema21[-1]:.4f}, Цена={close_prices[-1]:.4f}")

                if ema9[-1] > ema21[-1]:
                    send_telegram_message(f"📈 LONG сигнал по {symbol}")
                    place_order(symbol, "buy")
                elif ema9[-1] < ema21[-1]:
                    send_telegram_message(f"📉 SHORT сигнал по {symbol}")
                    place_order(symbol, "sell")
                else:
                    send_telegram_message(f"⚠️ Нет сигнала по {symbol}")

            except Exception as e:
                send_telegram_message(f"❌ Ошибка при анализе {symbol}: {e}")

            time.sleep(5)

        time.sleep(60)

@app.route('/')
def index():
    return "🤖 Bitget бот работает!"

if __name__ == '__main__':
    send_telegram_message("🤖 Бот успешно запущен на Render!")
    threading.Thread(target=strategy).start()
    app.run(host="0.0.0.0", port=10000)
