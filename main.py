# -*- coding: utf-8 -*-
"""
Bitget EMA Signal Bot (только сигналы, без торговли)
Фильтры: EMA, RSI, ATR
Команды в Telegram: /status, /setcooldown, /settf, /setsymbols, /help
"""

import time
import requests
import pandas as pd
import numpy as np
from flask import Flask, request
from threading import Thread

# ==== ТВОИ ДАННЫЕ ====
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

# ==== НАСТРОЙКИ ====
BITGET_CANDLES_URL = "https://api.bitget.com/api/mix/v1/market/candles"
SYMBOLS = ["BTCUSDT_UMCBL", "ETHUSDT_UMCBL", "XRPUSDT_UMCBL",
           "SOLUSDT_UMCBL", "TRXUSDT_UMCBL"]
TIMEFRAMES = {"5m": 300, "15m": 900, "1h": 3600}
SLEEP = 60  # проверка раз в минуту
SIGNAL_COOLDOWN = 300  # кулдаун сигналов (5 минут)

last_signals = {}  # { "BTCUSDT_UMCBL_5m": timestamp }

# ==== ФУНКЦИИ ====
def send_telegram(msg: str):
    """Отправка сообщения в телеграм"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    except Exception as e:
        print(f"Ошибка отправки в Telegram: {e}")


def get_candles(symbol: str, tf: str, limit: int = 200):
    """Получение свечей с Bitget"""
    params = {"symbol": symbol, "granularity": TIMEFRAMES[tf], "limit": limit}
    try:
        r = requests.get(BITGET_CANDLES_URL, params=params, timeout=10)
        data = r.json()
        if "data" not in data:
            print("Ошибка Bitget:", data)
            return None
        df = pd.DataFrame(data["data"],
                          columns=["ts", "open", "high", "low", "close", "volume", "baseVolume"])
        df["close"] = df["close"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df = df.iloc[::-1].reset_index(drop=True)
        return df
    except Exception as e:
        print(f"Ошибка загрузки свечей {symbol}: {e}")
        return None


def rsi(series, period=14):
    """RSI"""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(df, period=14):
    """ATR"""
    high_low = df["high"] - df["low"]
    high_close = np.abs(df["high"] - df["close"].shift())
    low_close = np.abs(df["low"] - df["close"].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def filters(df):
    """Фильтры рынка"""
    close = df["close"].iloc[-1]
    ema50 = df["close"].ewm(span=50).mean().iloc[-1]
    ema200 = df["close"].ewm(span=200).mean().iloc[-1]
    rsi_val = rsi(df["close"]).iloc[-1]
    atr_val = atr(df).iloc[-1] / close * 100
    trend_ok = ema50 > ema200
    rsi_ok = 40 < rsi_val < 70
    atr_ok = atr_val > 0.5
    all_green = trend_ok and rsi_ok and atr_ok
    return all_green, trend_ok, rsi_val, atr_val


def ema_strategy(symbol: str, tf: str):
    """EMA стратегия с фильтрами"""
    df = get_candles(symbol, tf)
    if df is None or len(df) < 200:
        return None

    df["EMA9"] = df["close"].ewm(span=9).mean()
    df["EMA21"] = df["close"].ewm(span=21).mean()

    signal = None
    if df["EMA9"].iloc[-2] < df["EMA21"].iloc[-2] and df["EMA9"].iloc[-1] > df["EMA21"].iloc[-1]:
        signal = "🟢 Возможен LONG"
    elif df["EMA9"].iloc[-2] > df["EMA21"].iloc[-2] and df["EMA9"].iloc[-1] < df["EMA21"].iloc[-1]:
        signal = "🔴 Возможен SHORT"

    if signal:
        key = f"{symbol}_{tf}"
        now = time.time()
        if key in last_signals and now - last_signals[key] < SIGNAL_COOLDOWN:
            return None  # кулдаун
        last_signals[key] = now

        all_green, trend_ok, rsi_val, atr_val = filters(df)
        status = "✅ Фильтры ЗЕЛЁНЫЕ" if all_green else "❌ Фильтры КРАСНЫЕ"
        return f"{signal}\n{symbol} {tf}\n{status}\nRSI={rsi_val:.1f} | ATR={atr_val:.2f}%"
    return None


def main_loop():
    send_telegram(
        "🤖 Бот запущен (EMA/RSI/ATR)\n"
        "Доступные команды: /status, /setcooldown, /settf, /setsymbols, /help"
    )
    while True:
        for symbol in SYMBOLS:
            for tf in TIMEFRAMES:
                signal = ema_strategy(symbol, tf)
                if signal:
                    send_telegram(signal)
                time.sleep(1)  # чтобы не спамить API
        time.sleep(SLEEP)


# ==== Flask для keep-alive и команд ====
app = Flask(__name__)

@app.route("/", methods=["GET", "POST"])
def home():
    return "EMA Signal Bot работает!"


@app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    """Webhook для обработки команд в Telegram"""
    global SIGNAL_COOLDOWN, TIMEFRAMES, SYMBOLS

    data = request.get_json()
    if not data or "message" not in data:
        return "ok"

    chat_id = str(data["message"]["chat"]["id"])
    text = data["message"].get("text", "")

    if chat_id != TELEGRAM_CHAT_ID:
        return "ok"

    if text.strip().lower() == "/status":
        report = []
        for symbol in SYMBOLS:
            for tf in TIMEFRAMES:
                df = get_candles(symbol, tf)
                if df is None:
                    continue
                all_green, trend_ok, rsi_val, atr_val = filters(df)
                status = "✅" if all_green else "❌"
                report.append(f"{symbol} {tf}: {status} | RSI={rsi_val:.1f} | ATR={atr_val:.2f}%")
        send_telegram("📊 Статус фильтров:\n" + "\n".join(report))

    elif text.startswith("/setcooldown"):
        try:
            value = int(text.split()[1])
            SIGNAL_COOLDOWN = value
            send_telegram(f"✅ Кулдаун сигналов установлен: {SIGNAL_COOLDOWN} сек.")
        except:
            send_telegram("⚠️ Используй: /setcooldown 300")

    elif text.startswith("/settf"):
        try:
            parts = text.split()[1].split(",")
            new_tfs = {}
            for p in parts:
                p = p.strip()
                if p == "5m":
                    new_tfs["5m"] = 300
                elif p == "15m":
                    new_tfs["15m"] = 900
                elif p == "1h":
                    new_tfs["1h"] = 3600
            if new_tfs:
                TIMEFRAMES = new_tfs
                send_telegram(f"✅ Таймфреймы изменены: {','.join(TIMEFRAMES.keys())}")
            else:
                send_telegram("⚠️ Ошибка формата. Пример: /settf 5m,15m")
        except:
            send_telegram("⚠️ Ошибка. Пример: /settf 5m,15m")

    elif text.startswith("/setsymbols"):
        try:
            parts = text.split()[1].split(",")
            new_syms = [p.strip() for p in parts if p.strip()]
            if new_syms:
                SYMBOLS = new_syms
                send_telegram(f"✅ Список монет изменён: {','.join(SYMBOLS)}")
            else:
                send_telegram("⚠️ Ошибка формата. Пример: /setsymbols BTCUSDT_UMCBL,ETHUSDT_UMCBL")
        except:
            send_telegram("⚠️ Ошибка. Пример: /setsymbols BTCUSDT_UMCBL,ETHUSDT_UMCBL")

    elif text.strip().lower() == "/help":
        help_msg = (
            "📖 Доступные команды:\n"
            "/status → показать фильтры (RSI/ATR/EMA)\n"
            "/setcooldown X → кулдаун сигналов (сек.)\n"
            "/settf 5m,15m,1h → задать таймфреймы\n"
            "/setsymbols BTCUSDT_UMCBL,ETHUSDT_UMCBL → выбрать монеты\n"
            "/help → показать список команд"
        )
        send_telegram(help_msg)

    return "ok"


if __name__ == "__main__":
    Thread(target=main_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
