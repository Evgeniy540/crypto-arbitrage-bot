# -*- coding: utf-8 -*-
"""
Bitget UMCBL Signal Bot (EMA 9/21) — SUPER версия
- Корректная сортировка history-candles (новейшая свеча в ответе первая — мы сортируем по ts↑)
- Сигналы только по закрытым свечам (-2/-3)
- TP/SL от цены закрытия сигнальной свечи
- PNG-график: Close, EMA9, EMA21, линии TP/SL, маркер сигнала
- Анти-дублирование сигналов
- Опциональное подтверждение трендом на другом ТФ (по умолчанию 5min)
- Flask keep-alive + /, /check_now, /status, /config
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

# ============ ТВОИ ДАННЫЕ (ВПИСАНО) ============
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
# ==============================================

# ============ НАСТРОЙКИ ============
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT"]  # можно расширить
FUT_SUFFIX = "_UMCBL"

BASE_TF    = "1min"   # основной ТФ сигналов
CONFIRM_TF = "5min"   # подтверждающий ТФ тренда ("" чтобы отключить)

CANDLES_LIMIT = 300
EMA_FAST, EMA_SLOW = 9, 21

TP_PCT = 0.015  # +1.5%
SL_PCT = 0.01   # -1.0%

CHECK_INTERVAL_SEC      = 60
NO_SIGNAL_COOLDOWN_SEC  = 60 * 60
CHART_TAIL              = 180

# ============ ГЛОБ СОСТОЯНИЯ ============
last_no_signal_ts = defaultdict(lambda: 0)     # антиспам "нет сигнала"
last_cross_dir    = defaultdict(lambda: None)  # последний отправленный "long"/"short"
last_cross_ts     = defaultdict(lambda: 0)

# ============ ВСПОМОГАТЕЛЬНОЕ ============
BITGET_MIX_HOST = "https://api.bitget.com"

def ts_now() -> int:
    return int(time.time())

def send_telegram_text(text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=12)
    except Exception as e:
        print("Telegram text exception:", e)

def send_telegram_photo(png_bytes: bytes, caption: str = ""):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        files = {"photo": ("chart.png", png_bytes, "image/png")}
        data  = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"}
        requests.post(url, data=data, files=files, timeout=20)
    except Exception as e:
        print("Telegram photo exception:", e)

def fetch_history_candles(symbol: str, granularity: str, limit: int = 300):
    """
    GET /api/mix/v1/market/history-candles?symbol=BTCUSDT_UMCBL&granularity=1min&limit=300
    Ответ: новейшая свеча ПЕРВОЙ. Мы сортируем по ts (возрастающе).
    Формат свечи: [ts, open, high, low, close, volume, turnover]
    """
    url = f"{BITGET_MIX_HOST}/api/mix/v1/market/history-candles"
    params = {"symbol": f"{symbol}{FUT_SUFFIX}", "granularity": granularity, "limit": str(min(max(limit, 50), 1000))}
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
        print(f"[{symbol}] fetch candles error:", e)
        return []

def ema(series, span):
    k = 2 / (span + 1.0)
    res = []
    for i, v in enumerate(series):
        res.append(v if i == 0 else v * k + res[-1] * (1 - k))
    return res

def prepare_series(raw_candles):
    if not raw_candles:
        return None, None
    candles = sorted(raw_candles, key=lambda x: int(x[0]))  # old->new
    closes  = [float(c[4]) for c in candles]
    times   = [int(c[0])   for c in candles]
    return times, closes

def last_closed_ok(closes, need=EMA_SLOW + 3) -> bool:
    return bool(closes) and len(closes) >= need

def ema_signal_series(closes):
    ema_f = ema(closes, EMA_FAST)
    ema_s = ema(closes, EMA_SLOW)
    # -1 текущая (формируется), -2/-3 закрытые
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

    return {
        "signal": sig,
        "price": price_closed,
        "ema_fast": ef_now,
        "ema_slow": es_now,
        "ema_f_series": ema_f,
        "ema_s_series": ema_s
    }

def confirm_direction_on_tf(symbol: str, expected: str, tf: str) -> bool:
    raw = fetch_history_candles(symbol, tf, CANDLES_LIMIT)
    times, closes = prepare_series(raw)
    if not last_closed_ok(closes):
        return False
    comp = ema_signal_series(closes)
    if expected == "long":
        return comp["ema_fast"] >= comp["ema_slow"]
    if expected == "short":
        return comp["ema_fast"] <= comp["ema_slow"]
    return False

def make_chart_png(symbol: str, tf: str, times, closes, ema_f_series, ema_s_series,
                   signal: str, price: float, tp: float, sl: float, tail=CHART_TAIL):
    times = times[-tail:]
    closes = closes[-tail:]
    ema_f_series = ema_f_series[-tail:]
    ema_s_series = ema_s_series[-tail:]
    sig_idx = len(closes) - 1  # последняя закрытая точка в хвосте

    fig = plt.figure(figsize=(7.5, 3.3), dpi=150)
    ax = plt.gca()

    ax.plot(closes, label="Close")
    ax.plot(ema_f_series, label=f"EMA{EMA_FAST}")
    ax.plot(ema_s_series, label=f"EMA{EMA_SLOW}")

    ax.axhline(tp, linestyle="--", linewidth=1.2, label="TP")
    ax.axhline(sl, linestyle="--", linewidth=1.2, label="SL")
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

def build_caption(symbol: str, tf: str, comp: dict, confirmed: bool):
    p  = float(comp["price"])
    ef = float(comp["ema_fast"])
    es = float(comp["ema_slow"])
    sig = comp["signal"]

    if sig == "long":
        tp = round(p * (1 + TP_PCT), 6)
        sl = round(p * (1 - SL_PCT), 6)
        title = "🟢 LONG"
    elif sig == "short":
        tp = round(p * (1 - TP_PCT), 6)
        sl = round(p * (1 + SL_PCT), 6)
        title = "🔴 SHORT"
    elif sig == "weak_long":
        title, tp, sl = "🟡 Слабый LONG", p, p
    elif sig == "weak_short":
        title, tp, sl = "🟠 Слабый SHORT", p, p
    else:
        title, tp, sl = "⚪️ Нет сигнала", p, p

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

def process_symbol(symbol: str):
    # 1) свечи основного ТФ
    raw = fetch_history_candles(symbol, BASE_TF, CANDLES_LIMIT)
    times, closes = prepare_series(raw)
    if not last_closed_ok(closes):
        # не спамим — только полезные сообщения; в лог консолью
        print(f"[{symbol}] Недостаточно данных")
        return

    # 2) сигнал
    comp = ema_signal_series(closes)
    sig = comp["signal"]

    if sig == "none":
        now = ts_now()
        if now - last_no_signal_ts[symbol] >= NO_SIGNAL_COOLDOWN_SEC:
            last_no_signal_ts[symbol] = now
            send_telegram_text(f"⚪️ Нет сигнала ({symbol} {BASE_TF})")
        return

    # 3) анти-дублирование на сильных сигналах
    if sig in ("long", "short") and last_cross_dir[symbol] == sig:
        return

    # 4) подтверждение трендом (если включено)
    confirmed = True
    if sig in ("long", "short") and CONFIRM_TF:
        confirmed = confirm_direction_on_tf(symbol, sig, CONFIRM_TF)

    # 5) отправка картинки
    caption, tp, sl = build_caption(symbol, BASE_TF, comp, confirmed)
    png = make_chart_png(
        symbol, BASE_TF, times, closes,
        comp["ema_f_series"], comp["ema_s_series"],
        sig, comp["price"], tp, sl, tail=CHART_TAIL
    )
    send_telegram_photo(png, caption)

    # 6) метки для анти-дубля
    if sig in ("long", "short"):
        last_cross_dir[symbol] = sig
        last_cross_ts[symbol]  = ts_now()

def worker_loop():
    send_telegram_text("🤖 Бот запущен на Render! (Bitget UMCBL, EMA 9/21, графики)")
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

# ============ FLASK ============
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

def run_flask():
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)

# ============ MAIN ============
if __name__ == "__main__":
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()
    run_flask()
