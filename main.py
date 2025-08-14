# -*- coding: utf-8 -*-
import os
import time
import threading
import traceback
from datetime import datetime, timezone

import requests
import numpy as np
import pandas as pd
from flask import Flask, jsonify

import ccxt

# ============= TELEGRAM =============
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"

def tg_send(text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
        requests.post(url, json=payload, timeout=10)
    except Exception:
        # не роняем бота, просто пишем в логи
        print("TELEGRAM ERROR:\n", traceback.format_exc())


# ============= FLASK (Render требует порт) =============
app = Flask(__name__)

@app.get("/")
def root():
    return "ok"

@app.get("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})


# ============= MARKET LOGIC (Bitget Spot) =============
SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "XRP/USDT",
    "TRX/USDT",
    "BGB/USDT",  # токен биржи
]

TIMEFRAME = "1m"
EMA_FAST = 9
EMA_SLOW = 21
MIN_CANDLES = 100             # запас истории
COOLDOWN_MINUTES = 10         # чтобы не спамить одним и тем же сигналом
MIN_EDGE = 0.001              # доп. фильтр: цена должна отходить от EMA21 минимум на 0.1%

# запоминаем последний момент, когда отправляли сигнал по символу
last_signal_ts = {}  # { "BTC/USDT": datetime }

def build_exchange():
    ex = ccxt.bitget({
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })
    return ex

def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def get_ohlcv_df(ex, symbol: str, tf: str, limit: int) -> pd.DataFrame:
    raw = ex.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
    if not raw or len(raw) < 10:
        raise RuntimeError(f"OHLCV empty for {symbol}")
    df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df

def format_price(p: float) -> str:
    # красивое отображение цены
    if p >= 1000:  return f"{p:,.2f}"
    if p >= 1:     return f"{p:,.4f}"
    return f"{p:.8f}".rstrip("0")

def make_signal_text(side: str, symbol: str, price: float, tf: str) -> str:
    now = datetime.now(timezone.utc).isoformat()
    return (
        f"🔔 {side} {symbol}\n"
        f"Цена: {format_price(price)}\n"
        f"EMA{EMA_FAST} vs EMA{EMA_SLOW} (TF {tf})\n"
        f"{now}"
    )

def allowed_by_cooldown(symbol: str) -> bool:
    t = last_signal_ts.get(symbol)
    if not t:
        return True
    return (datetime.now(timezone.utc) - t).total_seconds() >= COOLDOWN_MINUTES * 60

def mark_sent(symbol: str):
    last_signal_ts[symbol] = datetime.now(timezone.utc)

def scan_once(ex):
    for symbol in SYMBOLS:
        try:
            df = get_ohlcv_df(ex, symbol, TIMEFRAME, max(MIN_CANDLES, EMA_SLOW + 30))
            # расчёты
            df["ema_fast"] = ema(df["close"], EMA_FAST)
            df["ema_slow"] = ema(df["close"], EMA_SLOW)

            # берём две последние свечи, чтобы ловить именно "пересечение"
            c_prev = df.iloc[-2]
            c_curr = df.iloc[-1]

            crossed_up   = (c_prev["ema_fast"] <= c_prev["ema_slow"]) and (c_curr["ema_fast"] > c_curr["ema_slow"])
            crossed_down = (c_prev["ema_fast"] >= c_prev["ema_slow"]) and (c_curr["ema_fast"] < c_curr["ema_slow"])

            price = float(c_curr["close"])
            ema_slow_now = float(c_curr["ema_slow"])

            # лёгкий фильтр, чтобы срезать часть "пустых" пересечений
            edge = abs(price - ema_slow_now) / max(1e-12, ema_slow_now)

            if crossed_up and edge >= MIN_EDGE and allowed_by_cooldown(symbol):
                tg_send(make_signal_text("BUY", symbol, price, TIMEFRAME))
                mark_sent(symbol)

            elif crossed_down and edge >= MIN_EDGE and allowed_by_cooldown(symbol):
                tg_send(make_signal_text("SELL", symbol, price, TIMEFRAME))
                mark_sent(symbol)

            # лог для Render
            print(f"[{symbol}] close={price} ema{EMA_FAST}={c_curr['ema_fast']:.6f} ema{EMA_SLOW}={ema_slow_now:.6f} crossed_up={crossed_up} crossed_down={crossed_down} edge={edge:.5f}")

        except ccxt.NetworkError as e:
            print(f"[{symbol}] NETWORK ERROR: {e}")
        except ccxt.ExchangeError as e:
            print(f"[{symbol}] EXCHANGE ERROR: {e}")
        except Exception as e:
            print(f"[{symbol}] UNEXPECTED ERROR: {e}\n{traceback.format_exc()}")

def run_scanner_forever():
    ex = build_exchange()
    tg_send("🤖 Бот запущен! EMA {}/{}, TF {}, MIN_CANDLES={}. Сообщения — только по сигналам.".format(
        EMA_FAST, EMA_SLOW, TIMEFRAME, MIN_CANDLES
    ))
    while True:
        start = time.time()
        scan_once(ex)
        # итого ~каждые 15 секунд
        sleep_left = 15 - (time.time() - start)
        if sleep_left > 0:
            time.sleep(sleep_left)


# ============= ENTRYPOINT (Render) =============
if __name__ == "__main__":
    # запускаем сканер в отдельном потоке,
    # а Flask держит порт для Render
    t = threading.Thread(target=run_scanner_forever, daemon=True)
    t.start()

    port = int(os.environ.get("PORT", 5000))
    # ВАЖНО: host="0.0.0.0" — иначе Render не увидит порт
    app.run(host="0.0.0.0", port=port)
