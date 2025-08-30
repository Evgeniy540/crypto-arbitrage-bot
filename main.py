# -*- coding: utf-8 -*-
"""
Bitget UMCBL Signal Bot (EMA 9/21) — super full version
- Корректная сортировка history-candles (новейшая в ответе идёт первой, мы сортируем по ts↑)
- Проверка кроссов только по закрытым свечам (-2 и -3)
- TP/SL расчёт от цены закрытия сигнальной свечи
- PNG-график: Close, EMA9, EMA21, горизонтальные линии TP/SL, маркер сигнала
- Анти-спам: "нет сигнала" не чаще 1/час на символ, сигналы не дублируются без нового кросса
- Опциональная много-ТФ логика подтверждения (confirm TF)
- Flask keep-alive + ручные эндпоинты
"""

import os
import io
import time
import threading
from datetime import datetime, timezone
from collections import defaultdict

import requests
from flask import Flask, jsonify, request

# ======== Matplotlib без дисплея ========
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ============ ТВОИ ДАННЫЕ ============
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "PASTE_YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "PASTE_YOUR_CHAT_ID")
# =====================================

# ============ НАСТРОЙКИ ============

# Пары для мониторинга (без суффикса)
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT",
    # Добавляй при желании: "PEPEUSDT", "BGBUSDT", ...
]

# Основной таймфрейм сигналов
BASE_TF = os.getenv("BASE_TF", "1min")   # 1min / 5min / 15min / 30min / 1hour ...
# Подтверждающий таймфрейм (например, тренд 5m для 1m сигналов). Отключить: ""
CONFIRM_TF = os.getenv("CONFIRM_TF", "5min")  # "" чтобы выключить

FUT_SUFFIX = "_UMCBL"         # USDT-M perpetual на Bitget
CANDLES_LIMIT = 300           # истории хватает для "гладких" EMA
EMA_FAST, EMA_SLOW = 9, 21

# Near-cross (мягкий сигнал), используется только как подсказка (в текст не шлём)
NEAR_EPS_PCT = 0.001          # 0.10%

# TP/SL от цены закрытия сигнальной свечи
TP_PCT = float(os.getenv("TP_PCT", "0.015"))   # +1.5%
SL_PCT = float(os.getenv("SL_PCT", "0.01"))    # -1.0%

CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL", "60"))   # период цикла
NO_SIGNAL_COOLDOWN_SEC = 60 * 60                               # 1 час "нет сигнала"

# Сколько последних баров рисовать на картинке
CHART_TAIL = int(os.getenv("CHART_TAIL", "180"))

# ============ ГЛОБ СОСТОЯНИЯ ============
last_no_signal_ts = defaultdict(lambda: 0)           # "нет сигнала" антиспам
last_cross_dir    = defaultdict(lambda: None)        # "long"/"short" — что уже отправляли
last_cross_ts     = defaultdict(lambda: 0)           # ts последнего отправленного кросса

# ============ УТИЛИТЫ ============
BITGET_MIX_HOST = "https://api.bitget.com"

def ts_now() -> int:
    return int(time.time())

def send_telegram_text(text: str):
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN.startswith("PASTE_"):
        print("[TELEGRAM DISABLED]", text)
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
        if resp.status_code != 200:
            print("Telegram error:", resp.text)
    except Exception as e:
        print("Telegram exception:", e)

def send_telegram_photo(png_bytes: bytes, caption: str = ""):
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN.startswith("PASTE_"):
        print("[TELEGRAM DISABLED] <photo>", caption[:120], "...")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        files = {"photo": ("chart.png", png_bytes, "image/png")}
        data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"}
        resp = requests.post(url, data=data, files=files, timeout=20)
        if resp.status_code != 200:
            print("Telegram photo error:", resp.text)
    except Exception as e:
        print("Telegram photo exception:", e)

# ============ BITGET API ============
def fetch_history_candles(symbol: str, granularity: str, limit: int = 300):
    """
    GET /api/mix/v1/market/history-candles?symbol=BTCUSDT_UMCBL&granularity=1min&limit=300
    Ответ Bitget: новейшая свеча идёт ПЕРВОЙ. Мы обязательно сортируем по ts ↑.
    Формат свечи: [ts, open, high, low, close, volume, turnover]
    """
    url = f"{BITGET_MIX_HOST}/api/mix/v1/market/history-candles"
    params = {
        "symbol": f"{symbol}{FUT_SUFFIX}",
        "granularity": granularity,
        "limit": str(min(max(limit, 50), 1000))
    }
    headers = {"User-Agent": "Mozilla/5.0 (SignalBot/2.0)"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("data"):
            return data["data"]
        if isinstance(data, list) and data and isinstance(data[0], list):
            return data
        return []
    except Exception as e:
        print(f"[{symbol}] error fetch candles:", e)
        return []

# ============ ТЕХАНАЛИЗ ============
def ema(series, span):
    k = 2 / (span + 1.0)
    res = []
    for i, v in enumerate(series):
        res.append(v if i == 0 else v * k + res[-1] * (1 - k))
    return res

def prepare_series(raw_candles):
    """
    Сортируем, вытягиваем closes и times
    Возвращаем (times, closes) или (None, None)
    """
    if not raw_candles:
        return None, None
    candles = sorted(raw_candles, key=lambda x: int(x[0]))   # old -> new
    closes  = [float(c[4]) for c in candles]
    times   = [int(c[0])   for c in candles]
    return times, closes

def last_closed_indexes_ok(closes, need=EMA_SLOW+3):
    return (closes is not None) and (len(closes) >= need)

def ema_signal_series(closes, ema_fast=EMA_FAST, ema_slow=EMA_SLOW):
    ema_f = ema(closes, ema_fast)
    ema_s = ema(closes, ema_slow)
    # -1 текущая (формируется), -2 и -3 — закрытые
    ef_now, es_now   = ema_f[-2], ema_s[-2]
    ef_prev, es_prev = ema_f[-3], ema_s[-3]
    price_closed     = closes[-2]

    cross_up   = (ef_now > es_now) and (ef_prev <= es_prev)
    cross_down = (ef_now < es_now) and (ef_prev >= es_prev)
    above_both = (price_closed > ef_now) and (price_closed > es_now)
    below_both = (price_closed < ef_now) and (price_closed < es_now)

    if cross_up and above_both:
        sig = "long"
    elif cross_down and below_both:
        sig = "short"
    elif cross_up:
        sig = "weak_long"
    elif cross_down:
        sig = "weak_short"
    else:
        sig = "none"

    # near-cross — как подсказка (не используем в алертах)
    near = None
    diff_pct = abs(ef_now - es_now) / price_closed if price_closed else 1.0
    if sig == "none" and diff_pct <= NEAR_EPS_PCT:
        near = "near_long" if ef_now >= es_now else "near_short"

    return {
        "signal": sig,
        "price": price_closed,
        "ema_fast": ef_now,
        "ema_slow": es_now,
        "ema_f_series": ema_f,
        "ema_s_series": ema_s,
        "near": near
    }

def confirm_direction_on_tf(symbol, expected_direction: str, tf: str) -> bool:
    """
    Подтверждение направлением на другом ТФ:
    - Для long ожидаем ef_now >= es_now на confirm TF
    - Для short ожидаем ef_now <= es_now на confirm TF
    """
    raw = fetch_history_candles(symbol, tf, CANDLES_LIMIT)
    times, closes = prepare_series(raw)
    if not last_closed_indexes_ok(closes):
        return False
    em = ema_signal_series(closes)
    if expected_direction == "long":
        return em["ema_fast"] >= em["ema_slow"]
    if expected_direction == "short":
        return em["ema_fast"] <= em["ema_slow"]
    return False

# ============ ВИЗУАЛИЗАЦИЯ ============
def make_chart_png(symbol: str, tf: str, times, closes, ema_f_series, ema_s_series,
                   signal: str, price: float, tp: float, sl: float, tail=CHART_TAIL):
    """
    Возвращает PNG (bytes) с графиком Close, EMA9, EMA21, горизонтальными линиями TP/SL и маркером сигнала.
    """
    times = times[-tail:]
    closes = closes[-tail:]
    ema_f_series = ema_f_series[-tail:]
    ema_s_series = ema_s_series[-tail:]

    # Индекс сигнальной закрытой свечи — последний из отсечённого хвоста
    sig_idx = len(closes) - 1  # это закрытая -2 в полном массиве, но в хвосте последняя точка — закрытая

    fig = plt.figure(figsize=(7.5, 3.3), dpi=150)
    ax = plt.gca()

    # Линии графика (цвета не задаём — пусть будут дефолтные)
    ax.plot(closes, label="Close")
    ax.plot(ema_f_series, label=f"EMA{EMA_FAST}")
    ax.plot(ema_s_series, label=f"EMA{EMA_SLOW}")

    # Горизонтальные уровни TP/SL
    ax.axhline(tp, linestyle="--", linewidth=1.2, label="TP")
    ax.axhline(sl, linestyle="--", linewidth=1.2, label="SL")

    # Маркер сигнальной свечи
    ax.scatter([sig_idx], [price], s=35)

    direction = {"long": "LONG", "short": "SHORT",
                 "weak_long": "weak LONG", "weak_short": "weak SHORT"}.get(signal, signal)

    ax.set_title(f"{symbol} {tf} | {direction}")
    ax.set_xlabel("bars (old → new)")
    ax.set_ylabel("price")
    ax.legend(loc="best")
    ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.45)

    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.read()

# ============ ФОРМАТИРОВАНИЕ СООБЩЕНИЙ ============
def build_caption(symbol: str, tf: str, res: dict, confirmed: bool):
    p  = float(res["price"])
    ef = float(res["ema_fast"])
    es = float(res["ema_slow"])
    sig = res["signal"]

    if sig == "long":
        tp = round(p * (1 + TP_PCT), 6)
        sl = round(p * (1 - SL_PCT), 6)
        title = "🟢 LONG"
    elif sig == "short":
        tp = round(p * (1 - TP_PCT), 6)
        sl = round(p * (1 + SL_PCT), 6)
        title = "🔴 SHORT"
    elif sig == "weak_long":
        tp, sl = p, p  # чтобы не падало ниже, но не используем
        title = "🟡 Слабый LONG"
    elif sig == "weak_short":
        tp, sl = p, p
        title = "🟠 Слабый SHORT"
    else:
        tp, sl = p, p
        title = "⚪️ Нет сигнала"

    lines = [
        f"<b>{title} {symbol}</b>  ({tf})",
        f"Цена: <b>{p:.6f}</b>",
        f"EMA{EMA_FAST}: {ef:.6f} | EMA{EMA_SLOW}: {es:.6f}",
    ]
    if sig in ("long", "short"):
        lines += [f"🎯 TP: <b>{tp}</b>", f"🛑 SL: <b>{sl}</b>"]
        if CONFIRM_TF:
            lines += [f"Подтверждение {CONFIRM_TF}: <b>{'Да' if confirmed else 'Нет'}</b>"]

    return "\n".join(lines), tp, sl

# ============ ЛОГИКА ПРОВЕРКИ ============
def process_symbol(symbol: str):
    # 1) Получаем свечи
    raw = fetch_history_candles(symbol, BASE_TF, CANDLES_LIMIT)
    times, closes = prepare_series(raw)
    if not last_closed_indexes_ok(closes):
        send_telegram_text(f"{symbol} {BASE_TF}\n❗️Недостаточно данных")
        return

    # 2) Считаем EMA и сигнал по закрытым свечам
    comp = ema_signal_series(closes)
    sig = comp["signal"]
    if sig == "none":
        # Отправляем "нет сигнала" не чаще раза в час
        now = ts_now()
        if now - last_no_signal_ts[symbol] >= NO_SIGNAL_COOLDOWN_SEC:
            last_no_signal_ts[symbol] = now
            send_telegram_text(f"⚪️ Нет сигнала ({symbol} {BASE_TF})")
        return

    # 3) Анти-дублирование: не слать тот же long/short, пока новый кросс не случился
    # Считаем "новым" кроссом смену направления относительно последнего отправленного
    if sig in ("long", "short"):
        if last_cross_dir[symbol] == sig:
            # Уже отправляли такой же сигнал и нового кросса не было
            return
        # Если weak_* — просто индикация; ниже на них не триггеримся для антидубля
    # Weak-сигналы шлём, но они не сбрасывают last_cross_dir (иначе будет шум)

    # 4) Подтверждающий ТФ (если включён) — для сильных сигналов
    confirmed = True
    if sig in ("long", "short") and CONFIRM_TF:
        confirmed = confirm_direction_on_tf(symbol, sig, CONFIRM_TF)
        # Можно требовать обязательного подтверждения. Если хочешь — раскомментируй:
        # if not confirmed:
        #     return

    # 5) Картинка + подпись
    caption, tp, sl = build_caption(symbol, BASE_TF, comp, confirmed)
    png = make_chart_png(
        symbol, BASE_TF, times, closes,
        comp["ema_f_series"], comp["ema_s_series"],
        sig, comp["price"], tp, sl, tail=CHART_TAIL
    )
    send_telegram_photo(png, caption)

    # 6) Обновим метки для анти-дубля только на сильных сигналах
    if sig in ("long", "short"):
        last_cross_dir[symbol] = sig
        last_cross_ts[symbol]  = ts_now()

def worker_loop():
    send_telegram_text("🤖 Запуск: Bitget UMCBL сигнальный бот (EMA 9/21) — SUPER версия.")
    while True:
        started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"[{started}] tick")
        for sym in SYMBOLS:
            try:
                process_symbol(sym)
                time.sleep(0.5)  # щадим API
            except Exception as e:
                print(f"[{sym}] exception:", e)
        time.sleep(CHECK_INTERVAL_SEC)

# ============ FLASK KEEP-ALIVE + ручные эндпоинты ============
app = Flask(__name__)

@app.route("/")
def health():
    return jsonify({
        "ok": True,
        "service": "bitget-signal-bot",
        "symbols": SYMBOLS,
        "base_tf": BASE_TF,
        "confirm_tf": CONFIRM_TF,
        "ema": [EMA_FAST, EMA_SLOW],
        "tp_pct": TP_PCT,
        "sl_pct": SL_PCT
    })

@app.route("/check_now", methods=["POST"])
def check_now():
    payload = request.json or {}
    sym = payload.get("symbol")
    if sym:
        process_symbol(sym)
        return jsonify({"ok": True, "forced": sym})
    for s in SYMBOLS:
        process_symbol(s)
    return jsonify({"ok": True, "forced": "all"})

@app.route("/status")
def status():
    now = ts_now()
    info = {
        s: {
            "last_no_signal_minutes": round((now - last_no_signal_ts[s]) / 60, 1) if last_no_signal_ts[s] else None,
            "last_cross": last_cross_dir[s],
            "last_cross_ago_min": round((now - last_cross_ts[s]) / 60, 1) if last_cross_ts[s] else None
        }
        for s in SYMBOLS
    }
    return jsonify({"ok": True, "base_tf": BASE_TF, "confirm_tf": CONFIRM_TF, "symbols": SYMBOLS, "info": info})

@app.route("/config")
def config_view():
    return jsonify({
        "symbols": SYMBOLS,
        "base_tf": BASE_TF,
        "confirm_tf": CONFIRM_TF,
        "tp_pct": TP_PCT,
        "sl_pct": SL_PCT,
        "near_eps_pct": NEAR_EPS_PCT,
        "check_interval_sec": CHECK_INTERVAL_SEC,
        "chart_tail": CHART_TAIL
    })

def run_flask():
    port = int(os.environ.get("PORT", "5000"))
    # host=0.0.0.0 для Render/VPS
    app.run(host="0.0.0.0", port=port)

# ============ MAIN ============
if __name__ == "__main__":
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()
    run_flask()
