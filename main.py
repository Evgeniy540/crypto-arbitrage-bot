# -*- coding: utf-8 -*-
"""
Bitget UMCBL Signal Bot (EMA 9/21) — расширенная версия
— Правильная сортировка history-candles (новейшая свеча приходит первой -> сортируем по ts ↑)
— Сигналы по закрытым свечам (-2/-3)
— Strong (long/short), Weak (weak_long/weak_short) и Near (near_long/near_short)
— TP/SL от цены закрытия сигнальной свечи
— PNG-график: Close, EMA9, EMA21, TP/SL, маркер сигнальной свечи
— Антидубли для strong-сигналов
— Подтверждающий ТФ (по умолчанию 5min), можно выключить
— Flask: /, /check_now, /status, /config, /debug_once
"""

import os
import io
import time
import threading
from datetime import datetime, timezone
from collections import defaultdict

import requests
from flask import Flask, jsonify, request

# === Matplotlib без дисплея ===
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ============ ТВОИ ДАННЫЕ ============
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
# =====================================

# ============ НАСТРОЙКИ ============
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT"]  # добавляй по желанию
FUT_SUFFIX = "_UMCBL"

BASE_TF    = "1min"     # основной ТФ
CONFIRM_TF = "5min"     # подтверждение трендом; "" чтобы выключить

CANDLES_LIMIT = 300
EMA_FAST, EMA_SLOW = 9, 21

TP_PCT = 0.015  # +1.5%
SL_PCT = 0.01   # -1.0%

CHECK_INTERVAL_SEC      = 60
NO_SIGNAL_COOLDOWN_SEC  = 60 * 10   # чаще видеть пульс (каждые 10 минут)

# Дополнительные сигналы
SEND_WEAK_SIGNALS = True
SEND_NEAR_SIGNALS = True
NEAR_EPS_PCT      = 0.001  # 0.10% близости EMA

CHART_TAIL = 180

# ============ ГЛОБ. СОСТОЯНИЕ ============
last_no_signal_ts = defaultdict(lambda: 0)     # антиспам "нет сигнала"
last_cross_dir    = defaultdict(lambda: None)  # последний strong ("long"/"short")
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
    Ответ Bitget: новейшая свеча ПЕРВОЙ -> мы сортируем по ts (возрастающе).
    Формат: [ts, open, high, low, close, volume, turnover]
    """
    url = f"{BITGET_MIX_HOST}/api/mix/v1/market/history-candles"
    params = {"symbol": f"{symbol}{FUT_SUFFIX}", "granularity": granularity, "limit": str(min(max(limit, 50), 1000))}
    headers = {"User-Agent": "Mozilla/5.0 (SignalBot/2.1)"}
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
    candles = sorted(raw_candles, key=lambda x: int(x[0]))  # old -> new
    closes  = [float(c[4]) for c in candles]
    times   = [int(c[0])   for c in candles]
    return times, closes

def last_closed_ok(closes, need=EMA_SLOW + 3) -> bool:
    return bool(closes) and len(closes) >= need

def ema_signal_series(closes):
    ema_f = ema(closes, EMA_FAST)
    ema_s = ema(closes, EMA_SLOW)

    # -1 текущая (формируется), -2/-3 — закрытые
    ef_now, es_now   = ema_f[-2], ema_s[-2]
    ef_prev, es_prev = ema_f[-3], ema_s[-3]
    price_closed     = closes[-2]

    cross_up   = (ef_now > es_now) and (ef_prev <= es_prev)
    cross_down = (ef_now < es_now) and (ef_prev >= es_prev)

    # фильтр для strong
    above_both = (price_closed > ef_now) and (price_closed > es_now)
    below_both = (price_closed < ef_now) and (price_closed < es_now)

    # базовый сигнал
    sig = "none"
    if cross_up:
        sig = "weak_long"
        if above_both:
            sig = "long"
    elif cross_down:
        sig = "weak_short"
        if below_both:
            sig = "short"

    # near-cross (почти пересеклись) — подсказка
    near = None
    diff_pct = abs(ef_now - es_now) / price_closed if price_closed else 1.0
    if sig == "none" and diff_pct <= NEAR_EPS_PCT:
        near = "near_long" if ef_now >= es_now else "near_short"

    return {
        "signal": sig,                  # long/short/weak_long/weak_short/none
        "near": near,                   # near_long/near_short/None
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

    if signal in ("long", "short"):
        ax.axhline(tp, linestyle="--", linewidth=1.2, label="TP")
        ax.axhline(sl, linestyle="--", linewidth=1.2, label="SL")
    ax.scatter([sig_idx], [price], s=35)

    direction = {"long": "LONG", "short": "SHORT",
                 "weak_long": "weak LONG", "weak_short": "weak SHORT",
                 "near_long": "near LONG", "near_short": "near SHORT"}.get(signal, signal)

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

# ============ ОСНОВНОЙ ЦИКЛ ============
def process_symbol(symbol: str):
    # 1) свечи
    raw = fetch_history_candles(symbol, BASE_TF, CANDLES_LIMIT)
    times, closes = prepare_series(raw)
    if not last_closed_ok(closes):
        print(f"[{symbol}] Недостаточно данных")
        return

    # 2) сигнал
    comp = ema_signal_series(closes)
    sig  = comp["signal"]
    near = comp.get("near")

    # 3) если вообще нечего — шлём "нет сигнала" по расписанию
    if sig == "none" and not (SEND_NEAR_SIGNALS and near):
        now = ts_now()
        if now - last_no_signal_ts[symbol] >= NO_SIGNAL_COOLDOWN_SEC:
            last_no_signal_ts[symbol] = now
            send_telegram_text(f"⚪️ Нет сигнала ({symbol} {BASE_TF})")
        return

    # 4) антидубль только на strong
    if sig in ("long", "short") and last_cross_dir[symbol] == sig:
        return

    # 5) подтверждение трендом для strong
    confirmed = True
    if sig in ("long", "short") and CONFIRM_TF:
        confirmed = confirm_direction_on_tf(symbol, sig, CONFIRM_TF)

    # 6) фильтрация weak/near по настройкам
    if sig in ("weak_long", "weak_short") and not SEND_WEAK_SIGNALS:
        return
    if sig == "none" and near and not SEND_NEAR_SIGNALS:
        return

    # 7) подпись и график
    # для near используем формат weak (без TP/SL)
    caption, tp, sl = build_caption(symbol, BASE_TF, comp, confirmed)
    sig_for_plot = sig if sig != "none" else near
    if sig == "none" and near:
        note = "🔷 near LONG" if near == "near_long" else "♦️ near SHORT"
        caption = f"{caption}\n{note}"

    png = make_chart_png(
        symbol, BASE_TF, times, closes,
        comp["ema_f_series"], comp["ema_s_series"],
        sig_for_plot, comp["price"], tp, sl, tail=CHART_TAIL
    )
    send_telegram_photo(png, caption)

    # 8) отметим последний strong
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
        "sl_pct": SL_PCT,
        "weak": SEND_WEAK_SIGNALS,
        "near": SEND_NEAR_SIGNALS
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
        "chart_tail": CHART_TAIL,
        "weak": SEND_WEAK_SIGNALS,
        "near": SEND_NEAR_SIGNALS
    })

@app.route("/debug_once")
def debug_once():
    lines = []
    for s in SYMBOLS:
        try:
            raw = fetch_history_candles(s, BASE_TF, CANDLES_LIMIT)
            times, closes = prepare_series(raw)
            if not last_closed_ok(closes):
                lines.append(f"{s}: нет данных")
                continue
            comp = ema_signal_series(closes)
            sig  = comp["signal"]
            near = comp.get("near")
            p    = comp["price"]
            ef   = comp["ema_fast"]
            es   = comp["ema_slow"]
            lines.append(f"{s}: close={p:.6f} EMA{EMA_FAST}={ef:.6f} EMA{EMA_SLOW}={es:.6f} sig={sig} near={near}")
        except Exception as e:
            lines.append(f"{s}: error {e}")
    send_telegram_text("🔎 DEBUG\n" + "\n".join(lines))
    return jsonify({"ok": True, "sent_lines": len(lines), "lines": lines})

def run_flask():
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)

# ============ MAIN ============
if __name__ == "__main__":
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()
    run_flask()
