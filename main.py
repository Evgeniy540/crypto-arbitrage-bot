# -*- coding: utf-8 -*-
"""
main.py — сигнальный EMA-бот для Bitget UMCBL (фьючерсы, только сигналы)
"""

import os
import time
import threading
from datetime import datetime
from collections import defaultdict

import requests
from flask import Flask

# ==== ТВОИ ДАННЫЕ ====
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
# =====================

FUT_SUFFIX = "_UMCBL"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT"]

BASE_TF = "5m"
FALLBACK_TF = "15m"
CHECK_INTERVAL_S = 60
MIN_CANDLES = 120
EMA_FAST, EMA_SLOW = 9, 21

EPS_PCT = 0.0008
ATR_FACTOR = 0.25
SLOPE_MIN = 0.0
NO_SIGNAL_COOLDOWN_S = 3600
SIGNAL_COOLDOWN_S = 300

HEADERS = {"User-Agent": "Mozilla/5.0"}
BITGET_MIX_CANDLES = "https://api.bitget.com/api/mix/v1/market/history-candles"

app = Flask(__name__)
last_notif_no_signal = defaultdict(lambda: 0.0)
cooldown_per_symbol = defaultdict(lambda: 0.0)

state = {
    "symbols": SYMBOLS[:],
    "base_tf": BASE_TF,
    "fallback_tf": FALLBACK_TF,
    "check_s": CHECK_INTERVAL_S,
    "min_candles": MIN_CANDLES,
    "ema_fast": EMA_FAST,
    "ema_slow": EMA_SLOW,
    "eps_pct": EPS_PCT,
    "atr_k": ATR_FACTOR,
    "slope_min": SLOPE_MIN,
    "signal_cooldown_s": SIGNAL_COOLDOWN_S,
    "mode": "normal",
}

# ===== УТИЛИТЫ =====
def now_ts(): return time.time()
def fmt_dt(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def send_tg(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=10)
    except: pass

def ema(series, period):
    if len(series) < period: return []
    k = 2.0 / (period+1)
    out = []
    ema_prev = sum(series[:period]) / period
    out.extend([None]*(period-1))
    out.append(ema_prev)
    for x in series[period:]:
        ema_prev = x*k + ema_prev*(1-k)
        out.append(ema_prev)
    return out

def gran_ok(tf: str) -> str:
    mapping = {
        "1m": "60", "5m": "300", "15m": "900", "30m": "1800",
        "1h": "3600", "4h": "14400", "1d": "86400"
    }
    return mapping.get(tf, "300")

def fetch_candles(symbol, tf, limit=300):
    params = {"symbol": f"{symbol}{FUT_SUFFIX}", "granularity": gran_ok(tf), "limit": str(limit)}
    r = requests.get(BITGET_MIX_CANDLES, params=params, headers=HEADERS, timeout=15)
    j = r.json()
    if j.get("code") != "00000":
        return None, f"Bitget error {j.get('code')}: {j.get('msg')}"
    data = j.get("data", [])
    if not data: return None, "No candles"
    rows = []
    for row in data:
        try:
            rows.append(float(row[4]))  # close
        except: pass
    return rows[::-1], None

# ===== ЛОГИКА =====
def check_symbol(sym):
    if now_ts() < cooldown_per_symbol[sym]:
        return
    closes, err = fetch_candles(sym, state["base_tf"], 300)
    if err:
        send_tg(f"❌ Ошибка {sym}: {err}")
        return
    if len(closes) < state["min_candles"]:
        return
    efast = ema(closes, state["ema_fast"])
    eslow = ema(closes, state["ema_slow"])
    if not efast or not eslow: return
    diff_prev = efast[-2] - eslow[-2]
    diff_curr = efast[-1] - eslow[-1]
    if diff_prev <= 0 and diff_curr > 0:
        send_tg(f"🟢 LONG сигнал {sym}{FUT_SUFFIX} ({fmt_dt()})")
        cooldown_per_symbol[sym] = now_ts() + state["signal_cooldown_s"]
    elif diff_prev >= 0 and diff_curr < 0:
        send_tg(f"🔴 SHORT сигнал {sym}{FUT_SUFFIX} ({fmt_dt()})")
        cooldown_per_symbol[sym] = now_ts() + state["signal_cooldown_s"]

# ===== ПОТОКИ =====
def worker():
    send_tg("🤖 Бот запущен (сигнальный).")
    while True:
        for sym in state["symbols"]:
            check_symbol(sym)
        time.sleep(state["check_s"])

@app.route("/")
def root():
    return "ok"

def run_threads():
    threading.Thread(target=worker, daemon=True).start()

if __name__ == "__main__":
    run_threads()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
