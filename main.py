import time
import hmac
import hashlib
import base64
import requests
import json
import logging
from flask import Flask
import threading

# === НАСТРОЙКИ ===
KUCOIN_API_KEY = "687d0016c714e80001eecdbe"
KUCOIN_API_SECRET = "d954b08b-7fbd-408e-a117-4e358a8a764d"
KUCOIN_API_PASSPHRASE = "Evgeniy@84"

TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

TRADE_SYMBOLS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "GALA-USDT"]
TRADE_AMOUNT = 28
COOLDOWN = 60 * 60 * 6  # 6 часов
TP_PERCENT = 1.5
SL_PERCENT = 1.0

last_trade_time = {}

# === Telegram ===
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    except Exception as e:
        print(f"Telegram error: {e}")

# === KuCoin API подписанный запрос ===
def kucoin_request(method, path, data=None):
    url = "https://api.kucoin.com" + path
    now = int(time.time() * 1000)
    body = json.dumps(data) if data else ""
    str_to_sign = f"{now}{method}{path}{body}"
    signature = base64.b64encode(
        hmac.new(KUCOIN_API_SECRET.encode(), str_to_sign.encode(), hashlib.sha256).digest()
    ).decode()
    passphrase = base64.b64encode(
        hmac.new(KUCOIN_API_SECRET.encode(), KUCOIN_API_PASSPHRASE.encode(), hashlib.sha256).digest()
    ).decode()
    headers = {
        "KC-API-KEY": KUCOIN_API_KEY,
        "KC-API-SIGN": signature,
        "KC-API-TIMESTAMP": str(now),
        "KC-API-PASSPHRASE": passphrase,
        "KC-API-KEY-VERSION": "2",
        "Content-Type": "application/json",
    }
    response = requests.request(method, url, headers=headers, data=body)
    return response.json()

# === Получение свечей ===
def get_klines(symbol):
    url = f"https://api.kucoin.com/api/v1/market/candles?type=1min&symbol={symbol}"
    r = requests.get(url)
    try:
        candles = r.json().get("data", [])
        closes = [float(c[2]) for c in candles][::-1]  # close price
        return closes
    except Exception as e:
        send_telegram(f"❌ Ошибка получения свечей {symbol}: {e}")
        return []

# === EMA ===
def ema(prices, period):
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema_val = prices[0]
    for price in prices[1:]:
        ema_val = price * k + ema_val * (1 - k)
    return ema_val

# === Торговая логика ===
def trader(symbol):
    if time.time() - last_trade_time.get(symbol, 0) < COOLDOWN:
        return

    closes = get_klines(symbol)
    if not closes or len(closes) < 22:
        return

    ema9 = ema(closes[-9:], 9)
    ema21 = ema(closes[-21:], 21)

    if ema9 and ema21 and ema9 > ema21:
        price = closes[-1]
        amount = round(TRADE_AMOUNT / price, 6)

        # Покупка
        order = kucoin_request("POST", "/api/v1/orders", {
            "clientOid": str(time.time()),
            "side": "buy",
            "symbol": symbol,
            "type": "market",
            "size": str(amount)
        })
        if "data" in order:
            send_telegram(f"✅ Куплено {symbol} по {price}")
            last_trade_time[symbol] = time.time()

            # Ждём и продаём по TP или SL
            target_price = price * (1 + TP_PERCENT / 100)
            stop_price = price * (1 - SL_PERCENT / 100)

            while True:
                current = get_klines(symbol)
                if not current:
                    break
                current_price = current[-1]
                if current_price >= target_price:
                    side = "sell"
                    msg = f"📈 Продано {symbol} по {current_price}, профит"
                    break
                elif current_price <= stop_price:
                    side = "sell"
                    msg = f"📉 Продано {symbol} по {current_price}, убыток"
                    break
                time.sleep(30)

            kucoin_request("POST", "/api/v1/orders", {
                "clientOid": str(time.time()),
                "side": side,
                "symbol": symbol,
                "type": "market",
                "size": str(amount)
            })
            send_telegram(msg)

# === Главный цикл ===
def main_loop():
    send_telegram("🤖 Бот успешно запущен на Render и готов торговать на KuCoin!")
    while True:
        for s in TRADE_SYMBOLS:
            threading.Thread(target=trader, args=(s,), daemon=True).start()
        time.sleep(60)

# === Flask Keep-alive ===
app = Flask(__name__)

@app.route('/')
def home():
    return "Crypto KuCoin Trader Running"

if __name__ == '__main__':
    threading.Thread(target=main_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=8080)
