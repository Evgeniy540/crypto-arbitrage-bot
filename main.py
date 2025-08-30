# -*- coding: utf-8 -*-
import os
import time
import threading
from datetime import datetime, timezone
from collections import defaultdict

import requests
from flask import Flask, jsonify, request

# ========= ТВОИ ДАННЫЕ =========
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"
# ===============================

# -------- Настройки --------
FUT_SUFFIX = "_UMCBL"  # USDT-M perpetual на Bitget

# РАСШИРЕННЫЙ СПИСОК МОНЕТ (25)
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT",
    "BNBUSDT", "ADAUSDT", "DOGEUSDT", "AVAXUSDT",
    "DOTUSDT", "LTCUSDT", "APTUSDT", "ARBUSDT", "OPUSDT",
    "LINKUSDT", "ATOMUSDT", "NEARUSDT", "FILUSDT", "SUIUSDT",
    "PEPEUSDT", "SHIBUSDT", "ETCUSDT", "ICPUSDT", "INJUSDT"
]

WORK_TF = "10min"  # рабочий ТФ для входов
HTF_TF = "15min"  # 1-й фильтр тренда
HTF2_TF = "1h"  # 2-й фильтр тренда

EMA_FAST, EMA_SLOW = 9, 21
EMA_DIR_PERIOD = 50  # фильтр направления 1 (средний тренд)
EMA_LONG_PERIOD = 200  # фильтр направления 2 (глобальный тренд)
EMA50_NEEDS_SLOPE = False  # требовать наклон EMA50 по направлению
EMA200_NEEDS_SLOPE = False  # требовать наклон EMA200 по направлению
CANDLES_LIMIT = 600  # глубокая история

STRENGTH_PCT = 0.002  # 0.20% мин. «сила» кросса
RSI_PERIOD = 14
RSI_MID = 50  # порог RSI

# --- ATR-фильтр волатильности ---
ATR_MIN_PCT = 0.0015  # 0.15% — тонко => блок
ATR_MAX_PCT = 0.03  # 3.00% — шторм => блок

ALERT_COOLDOWN_SEC = 15 * 60
HEARTBEAT_SEC = 60 * 60
REQUEST_TIMEOUT = 12

# Чуть увеличены интервалы, чтобы не упереться в лимиты при 25 парах
SLEEP_BETWEEN_SYMBOLS = 0.35
LOOP_SLEEP = 1.8

RECHECK_FAIL_SEC = 15 * 60

# --- ПРЕДСИГНАЛЫ ---
SETUP_COOLDOWN_SEC = 20 * 60

BASE_URL = "https://api.bitget.com"
_REQ_HEADERS = {"User-Agent": "futures-signal-bot/2.0", "Accept": "application/json"}

# -------- Служебные --------
last_alert_time = defaultdict(lambda: 0.0)
last_heartbeat_time = defaultdict(lambda: 0.0)
last_band_state = {}  # LONG/SHORT/NEUTRAL (5m)
accepted_params = {}  # (sym_base, tf) -> dict(...)
disabled_symbols = {}  # (sym_base, tf) -> dict(...)
last_candles_count = defaultdict(lambda: {"5m": 0, "15m": 0, "1h": 0})
last_filter_gate = defaultdict(lambda: "unknown")  # 'allow' | 'block' | 'unknown'
last_atr_info = defaultdict(lambda: {"atr": None, "atr_pct": None})
last_block_reasons = defaultdict(list)
last_setup_time = defaultdict(lambda: 0.0)

app = Flask(__name__)

# ========= Утилиты =========
_GRAN_TO_SEC = {
    "1": 60, "60": 60, "1min": 60,
    "3": 180, "180": 180, "3min": 180,
    "5": 300, "300": 300, "5min": 300,
    "15": 900, "900": 900, "15min": 900,
    "30": 1800, "1800": 1800, "30min": 1800,
    "60min": 3600, "1h": 3600, "3600": 3600,
    "240": 14400, "4h": 14400, "14400": 14400,
    "21600": 21600, "6h": 21600,
    "43200": 43200, "12h": 43200,
    "86400": 86400, "1day": 86400,
    "604800": 604800, "1week": 604800,
    "2592000": 2592000, "1M": 2592000,
}

# ========= Индикаторы =========
def ema_series(values, period):
    """Вычисление экспоненциальной скользящей средней (EMA)."""
    out, k, ema = [], 2.0 / (period + 1.0), None
    for v in values:
        ema = v if ema is None else (v * k + ema * (1 - k))
        out.append(ema)
    return out

def rsi_series(close, period=14):
    """Вычисление индикатора RSI."""
    if len(close) < period + 2:
        return [50.0] * len(close)
    gains = [max(0.0, close[i] - close[i - 1]) for i in range(1, len(close))]
    losses = [max(0.0, close[i - 1] - close[i]) for i in range(1, len(close))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsis = [50.0] * (period + 1)
    rs = (avg_gain / avg_loss) if avg_loss != 0 else 9999
    rsis.append(100 - 100 / (1 + rs))
    for i in range(period + 2, len(close) + 1):
        g = gains[i - 2]
        l = losses[i - 2]
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        rs = (avg_gain / avg_loss) if avg_loss != 0 else 9999
        rsis.append(100 - 100 / (1 + rs))
    return rsis[:len(close)]

def atr_series(high, low, close, period=14):
    """Вычисление Average True Range (ATR)."""
    trs = []
    for i in range(len(close)):
        if i == 0:
            trs.append(high[i] - low[i])
        else:
            trs.append(max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1])
            ))
    if len(trs) < period:
        return [None] * len(close)
    out = [None] * (period - 1) + [sum(trs[:period]) / period]
    for i in range(period, len(trs)):
        out.append((out[-1] * (period - 1) + trs[i]) / period)
    return out

# ========= Функции для работы с API Bitget =========
def get_closed_ohlcv(sym_base: str, tf: str, limit: int):
    """Получение свечей с API Bitget."""
    data = fetch_candles_exact(sym_base, tf, limit)
    if not data:
        return [], [], []
    gran_sec = _GRAN_TO_SEC.get(tf, 300)
    now_ms = int(time.time() * 1000)
    closed = [r for r in data if (now_ms - int(r[0])) >= gran_sec * 1000]
    if not closed:
        return [], [], []
    highs = [r[2] for r in closed]
    lows = [r[3] for r in closed]
    closes = [r[4] for r in closed]
    return highs, lows, closes

# ========= Логика сигналов =========
def analyze_and_alert(sym_base: str):
    """Анализируем данные и отправляем сигналы."""
    h5, l5, c5 = get_closed_ohlcv(sym_base, WORK_TF, CANDLES_LIMIT)
    h15, l15, c15 = get_closed_ohlcv(sym_base, HTF_TF, CANDLES_LIMIT // 2)
    h1h, l1h, c1h = get_closed_ohlcv(sym_base, HTF2_TF, max(200, CANDLES_LIMIT // 3))

    # Убедимся, что данных достаточно для анализа
    if len(c5) < max(EMA_SLOW + 5, 60) or len(c15) < max(EMA_SLOW + 5, 40) or len(c1h) < 60:
        return

    # EMA, RSI и ATR
    ema9_5, ema21_5 = ema_series(c5, EMA_FAST), ema_series(c5, EMA_SLOW)
    ema50_5 = ema_series(c5, EMA_DIR_PERIOD)
    ema200_5 = ema_series(c5, EMA_LONG_PERIOD)
    rsi5 = rsi_series(c5, RSI_PERIOD)
    atr5 = atr_series(h5, l5, c5, 14)

    i = len(c5) - 1
    if i < 2:
        return

    # Проверка пересечений EMA
    cross_up_prev = ema9_5[i - 2] <= ema21_5[i - 2] and ema9_5[i - 1] > ema21_5[i - 1]
    cross_down_prev = ema9_5[i - 2] >= ema21_5[i - 2] and ema9_5[i - 1] < ema21_5[i - 1]

    # Условия для лонга и шорта
    price_above = c5[i] > max(ema9_5[i], ema21_5[i])
    price_below = c5[i] < min(ema9_5[i], ema21_5[i])
    rsi_ok_long = rsi5[i] >= RSI_MID and rsi5[i] > rsi5[i - 1]
    rsi_ok_short = rsi5[i] <= RSI_MID and rsi5[i] < rsi5[i - 1]

    # Проверка на достаточную волатильность (ATR)
    entry = c5[i]
    this_atr = atr5[i] if atr5[i] else entry * 0.01
    atr_pct = this_atr / entry if entry > 0 else None
    atr_ok = ATR_MIN_PCT <= atr_pct <= ATR_MAX_PCT if atr_pct else False

    # Проверка направления тренда
    trend_up = ema9_5[i] > ema21_5[i] and ema9_5[i - 1] > ema21_5[i - 1]
    trend_down = ema9_5[i] < ema21_5[i] and ema9_5[i - 1] < ema21_5[i - 1]

    # Условия для лонга и шорта
    allow_long = cross_up_prev and trend_up and price_above and rsi_ok_long and atr_ok
    allow_short = cross_down_prev and trend_down and price_below and rsi_ok_short and atr_ok

    # Отправка сигнала
    if allow_long:
        msg = f"🔔 BUY/LONG {sym_base}\nЦена: {entry:.6f}\nТренды: 15m/1h OK\nRSI: {rsi5[i]:.1f} • ATR: {atr_pct*100:.2f}%"
        send_telegram(msg)
    elif allow_short:
        msg = f"🔔 SELL/SHORT {sym_base}\nЦена: {entry:.6f}\nТренды: 15m/1h OK\nRSI: {rsi5[i]:.1f} • ATR: {atr_pct*100:.2f}%"
        send_telegram(msg)

# ========= Основной цикл =========
def worker_loop():
    """Основной цикл работы бота."""
    for base in SYMBOLS:
        try:
            analyze_and_alert(base)
        except Exception as e:
            print(f"[{base}] analyze error: {e}")
        time.sleep(SLEEP_BETWEEN_SYMBOLS)

# Запуск рабочего цикла в отдельном потоке
def run():
    th = threading.Thread(target=worker_loop, daemon=True)
    th.start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=False)

if __name__ == "__main__":
    run()
