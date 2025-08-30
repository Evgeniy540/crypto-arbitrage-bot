# -*- coding: utf-8 -*-
import os
import time
import threading
from datetime import datetime, timezone
from collections import defaultdict

import requests
from flask import Flask, jsonify, request

# ========= ТВОИ ДАННЫЕ =========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "ТОКЕН_ТУТ")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "ЧАТ_ID_ТУТ")

# ========= НАСТРОЙКИ =========
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]

BASE_TF     = "300"           # 5 минут
CONFIRM_TFS = ["900", "3600"] # 15 минут, 1 час

CHECK_INTERVAL = 30           # каждые 30 сек проверка
NO_SIGNAL_INTERVAL = 600      # "нет сигнала" раз в 10 мин

EMA_FAST = 50
EMA_SLOW = 200
RSI_PERIOD = 14

# ========= ФИЛЬТРЫ =========
MIN_STRENGTH = 0.001   # 0.1% (раньше было 0.2%)
MAX_ATR      = 0.02    # 2% (ослабил фильтр)
MIN_ATR      = 0.001   # 0.1%

# ========= ГЛОБАЛ =========
last_signal_time = defaultdict(lambda: 0)
last_none_time = defaultdict(lambda: 0)

# ========= УТИЛИТЫ =========
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})

def fetch_history_candles(symbol, granularity, limit=300):
    url = f"https://api.bitget.com/api/v2/market/history-candles"
    params = {"symbol": symbol, "granularity": granularity, "limit": str(limit)}
    r = requests.get(url, params=params, timeout=10)
    if r.status_code != 200:
        return []
    data = r.json().get("data", [])
    candles = [
        {
            "time": int(c[0]) // 1000,
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]),
        }
        for c in data
    ]
    return candles[::-1]

def ema(values, period):
    k = 2 / (period + 1)
    ema_val = values[0]
    res = []
    for v in values:
        ema_val = v * k + ema_val * (1 - k)
        res.append(ema_val)
    return res

def rsi(values, period=14):
    if len(values) < period + 1:
        return [50] * len(values)
    deltas = [values[i+1] - values[i] for i in range(len(values)-1)]
    seed = deltas[:period]
    up = sum(x for x in seed if x > 0) / period
    down = -sum(x for x in seed if x < 0) / period
    rs = up / down if down != 0 else 0
    r = [100 - 100 / (1 + rs)]
    upval, downval = up, down
    for delta in deltas[period:]:
        upval = (upval * (period - 1) + (delta if delta > 0 else 0)) / period
        downval = (downval * (period - 1) + (-delta if delta < 0 else 0)) / period
        rs = upval / downval if downval != 0 else 0
        r.append(100 - 100 / (1 + rs))
    return [50] * (period) + r

def atr(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i-1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if not trs:
        return 0
    atr_vals = []
    avg = sum(trs[:period]) / period
    atr_vals.append(avg)
    for tr in trs[period:]:
        avg = (avg * (period - 1) + tr) / period
        atr_vals.append(avg)
    return atr_vals[-1] / candles[-1]["close"]

# ========= СТРАТЕГИЯ =========
def analyze(candles):
    closes = [c["close"] for c in candles]
    ema_fast = ema(closes, EMA_FAST)
    ema_slow = ema(closes, EMA_SLOW)
    rsi_vals = rsi(closes, RSI_PERIOD)
    atr_val = atr(candles)

    last = closes[-1]
    strength = (last - ema_slow[-1]) / last

    signal = "none"
    reason = []
    if abs(strength) >= MIN_STRENGTH and MIN_ATR <= atr_val <= MAX_ATR:
        if ema_fast[-1] > ema_slow[-1] and rsi_vals[-1] > 50:
            signal = "long"
            reason.append("EMA↑, RSI>50")
        elif ema_fast[-1] < ema_slow[-1] and rsi_vals[-1] < 50:
            signal = "short"
            reason.append("EMA↓, RSI<50")
    return signal, strength, atr_val, rsi_vals[-1], reason

# ========= ЛОГИКА =========
def process_symbol(symbol):
    candles = fetch_history_candles(symbol, BASE_TF)
    if len(candles) < 210:
        return

    base_sig, strength, atr_val, rsi_val, reason = analyze(candles)

    if base_sig == "none":
        now = time.time()
        if now - last_none_time[symbol] > NO_SIGNAL_INTERVAL:
            send_telegram_message(f"⚪️ [{symbol}] нет сигнала (RSI={rsi_val:.1f}, ATR={atr_val:.3f})")
            last_none_time[symbol] = now
        return

    confirm_ok = True
    for tf in CONFIRM_TFS:
        conf_candles = fetch_history_candles(symbol, tf)
        if len(conf_candles) < 210:
            continue
        sig, *_ = analyze(conf_candles)
        if sig != base_sig:
            confirm_ok = False
            break

    if confirm_ok:
        now = time.time()
        if now - last_signal_time[symbol] > 60:  # не чаще раза в минуту
            emoji = "📈" if base_sig == "long" else "📉"
            msg = (
                f"{emoji} [{symbol}] {base_sig.upper()} сигнал\n"
                f"RSI={rsi_val:.1f}, ATR={atr_val:.3f}, strength={strength:.3%}\n"
                f"Причины: {', '.join(reason)}"
            )
            send_telegram_message(msg)
            last_signal_time[symbol] = now

def worker():
    send_telegram_message("🤖 Бот запущен (EMA+RSI+ATR)")
    while True:
        for s in SYMBOLS:
            try:
                process_symbol(s)
            except Exception as e:
                send_telegram_message(f"⚠️ Ошибка {s}: {e}")
        time.sleep(CHECK_INTERVAL)

# ========= FLASK =========
app = Flask(__name__)

@app.route("/")
def index():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})

@app.route("/debug_once")
def debug_once():
    out = {}
    for s in SYMBOLS:
        candles = fetch_history_candles(s, BASE_TF)
        if len(candles) < 210:
            out[s] = "no data"
            continue
        sig, strength, atr_val, rsi_val, reason = analyze(candles)
        out[s] = {"signal": sig, "strength": strength, "ATR": atr_val, "RSI": rsi_val, "reason": reason}
    return jsonify(out)

def start_worker():
    t = threading.Thread(target=worker, daemon=True)
    t.start()

if __name__ == "__main__":
    start_worker()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
