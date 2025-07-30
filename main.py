import os
import json
import time
import requests
import schedule
import threading
from flask import Flask, request
from telegram import Bot

# === НАСТРОЙКИ ===
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"
TRADE_AMOUNT = 5  # Начальная сумма сделки

bot = Bot(token=TELEGRAM_TOKEN)

app = Flask(__name__)

def send_telegram_message(text):
    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
    except Exception as e:
        print(f"Ошибка отправки Telegram: {e}")

def get_price(symbol):
    try:
        url = f"https://api.bitget.com/api/spot/v1/market/ticker?symbol={symbol}"
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
        data = response.json()
        return float(data["data"]["last"])
    except Exception as e:
        send_telegram_message(f"❌ Ошибка получения цены для {symbol}: {e}")
        return None

def trade_logic():
    symbol = "BTCUSDT"
    price = get_price(symbol)
    if not price:
        return

    # Пример: простая логика
    if price < 60000:
        send_telegram_message(f"🟢 Сигнал на покупку {symbol} по цене {price}")
        # Здесь могла бы быть реальная торговля
    else:
        send_telegram_message(f"🔴 Нет сигнала по {symbol}, цена: {price}")

def run_schedule():
    schedule.every(1).minutes.do(trade_logic)
    while True:
        schedule.run_pending()
        time.sleep(1)

@app.route('/')
def index():
    return '🤖 Crypto bot is running!'

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    print("Webhook data:", data)
    return '', 200

# Запуск потока с расписанием
threading.Thread(target=run_schedule, daemon=True).start()

# Запуск Flask
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
