# -*- coding: utf-8 -*-
"""
Bitget UMCBL Signal Bot — фильтры + EMA + Flask
— EMA50/200 + RSI confirmation, TP/SL (RR 2:1), PNG-chart, anti-duplicate, endpoints: /, /check_now, /status, /config, /debug_once
"""
import os, io, time, threading, math
from datetime import datetime, timezone
from collections import defaultdict

import requests
from flask import Flask, jsonify, request

# ==== Matplotlib (headless)
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ==== Данные Telegram (вписаны)
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"

# ==== Настройки
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT"]
FUT_SUFFIX = "_UMCBL"

# human labels -> bitget granularity (seconds)
TFMAP = {"5min":"300","15min":"900","1hour":"3600"}
BASE_TF_LABEL = "5min"
CONFIRM_TF_LABELS = ["15min","1hour"]

EMA_FAST, EMA_SLOW = 50, 200
RSI_PERIOD = 14

# фильтры/параметры
MIN_STRENGTH = 0.0005   # 0.05% strength threshold (мягкий)
CHECK_INTERVAL_SEC = 30
NO_SIGNAL_COOLDOWN_SEC = 60 * 10
ANTI_DUP_COOLDOWN_SEC = 60 * 1  # минимально 1 минута между сигнальными отправками по паре

CANDLES_LIMIT = 500
CHART_TAIL = 180

BITGET_MIX_HOST = "https://api.bitget.com"

# состояния
last_no_signal_ts  = defaultdict(lambda: 0)
last_cross_dir     = defaultdict(lambda: None)     # last strong dir ("long"|"short")
last_cross_ts      = defaultdict(lambda: 0)
last_filters_green = defaultdict(lambda: None)
last_sent_ts       = defaultdict(lambda: 0)        # last time any signal was sent for pair

# ==== Telegram
def send_telegram_text(text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=15)
    except Exception as e:
        print("TG text err:", e)

def send_telegram_photo(png_bytes: bytes, caption: str = ""):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        files = {"photo": ("chart.png", png_bytes, "image/png")}
        data  = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"}
        requests.post(url, data=data, files=files, timeout=30)
    except Exception as e:
        print("TG photo err:", e)

# ==== Bitget API
def tf2gran(label: str) -> str:
    return TFMAP.get(label, label)

def fetch_history_candles(symbol: str, tf_label: str, limit: int = CANDLES_LIMIT):
    gran = tf2gran(tf_label)
    url = f"{BITGET_MIX_HOST}/api/mix/v1/market/history-candles"
    params = {"symbol": f"{symbol}{FUT_SUFFIX}", "granularity": gran, "limit": str(min(max(limit, 50), 1000))}
    headers = {"User-Agent": "SignalBot/filters+ema"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        # bitget may return {"data":[...]} or list
        if isinstance(data, dict):
            data = data.get("data", [])
        if isinstance(data, list) and data and isinstance(data[0], list):
            # format: [ts, open, high, low, close, volume]
            # sort asc by ts
            candles = sorted(data, key=lambda x: int(x[0]))
            return candles
        return []
    except Exception as e:
        print(f"[{symbol}] candles err:", e)
        return []

# ==== TA helpers
def ema(series, span):
    k = 2 / (span + 1.0)
    out=[]
    for i,v in enumerate(series):
        out.append(v if i==0 else v*k + out[-1]*(1-k))
    return out

def rsi14(closes, period=RSI_PERIOD):
    if len(closes) < period + 2:
        return None
    gains, losses = [], []
    for i in range(1, period+1):
        ch = closes[i] - closes[i-1]
        gains.append(max(ch, 0.0)); losses.append(max(-ch, 0.0))
    avg_g, avg_l = sum(gains)/period, sum(losses)/period
    rs = (avg_g / avg_l) if avg_l != 0 else 1e9
    rsi_vals = [100 - 100/(1+rs)]
    for i in range(period+1, len(closes)):
        ch = closes[i] - closes[i-1]
        g = max(ch, 0.0); l = max(-ch, 0.0)
        avg_g = (avg_g*(period-1) + g) / period
        avg_l = (avg_l*(period-1) + l) / period
        rs = (avg_g / avg_l) if avg_l != 0 else 1e9
        rsi_vals.append(100 - 100/(1+rs))
    return [None]*(len(closes)-len(rsi_vals)) + rsi_vals

def prepare_ohlc(raw):
    if not raw: return None
    candles = sorted(raw, key=lambda x: int(x[0]))
    ts    = [int(c[0]) for c in candles]
    opens = [float(c[1]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows  = [float(c[3]) for c in candles]
    closes= [float(c[4]) for c in candles]
    return ts, opens, highs, lows, closes

def ema_block(closes, f=EMA_FAST, s=EMA_SLOW):
    ef, es = ema(closes, f), ema(closes, s)
    # closed candle = -2
    ef_now, es_now = ef[-2], es[-2]
    ef_prev, es_prev = ef[-3], es[-3]
    price = closes[-2]
    cross_up   = (ef_now > es_now) and (ef_prev <= es_prev)
    cross_down = (ef_now < es_now) and (ef_prev >= es_prev)
    above_both = (price > ef_now) and (price > es_now)
    below_both = (price < ef_now) and (price < es_now)
    sig = "none"
    if cross_up:
        sig = "weak_long";  sig = "long"  if above_both else sig
    elif cross_down:
        sig = "weak_short"; sig = "short" if below_both else sig
    near=None
    if sig=="none" and price:
        diff = abs(ef_now - es_now)/price
        if diff <= 0.001:
            near = "near_long" if ef_now>=es_now else "near_short"
    return dict(sig=sig, near=near, price=price, ef=ef_now, es=es_now, ef_series=ef, es_series=es)

def ema_dir(closes):
    ef = ema(closes, EMA_FAST); es = ema(closes, EMA_SLOW)
    if ef[-2] >= es[-2]: return "long"
    return "short"

def ema50_200_ok(closes):
    e50 = ema(closes, EMA_FAST); e200 = ema(closes, EMA_SLOW)
    return e50[-2] >= e200[-2]

# ==== SL/TP calculation (simple local swing)
def calc_sl_tp(closes, lows, highs, price, side, lookback=20, buffer_pct=0.001):
    # lookback uses last `lookback` bars (including the closing bar)
    lb = min(len(lows), lookback)
    recent_lows = lows[-lb:]
    recent_highs = highs[-lb:]
    if side == "long":
        sl_base = min(recent_lows)
        sl = sl_base * (1 - buffer_pct)
        tp = price + (price - sl) * 2.0  # RR 2:1
    else:
        sl_base = max(recent_highs)
        sl = sl_base * (1 + buffer_pct)
        tp = price - (sl - price) * 2.0
    return sl, tp

# ==== Chart
def chart_png(symbol, tf_label, closes, ef_series, es_series, signal, price, tp, sl, tail=CHART_TAIL):
    closes = closes[-tail:]; ef_series = ef_series[-tail:]; es_series = es_series[-tail:]
    sig_idx = len(closes)-1
    fig = plt.figure(figsize=(8, 3.6), dpi=140); ax = plt.gca()
    ax.plot(closes, label="Close"); ax.plot(ef_series, label=f"EMA{EMA_FAST}"); ax.plot(es_series, label=f"EMA{EMA_SLOW}")
    if signal in ("long","short"):
        ax.axhline(tp, linestyle="--", linewidth=1.2, label="TP")
        ax.axhline(sl, linestyle="--", linewidth=1.2, label="SL")
    ax.scatter([sig_idx],[price], s=35)
    name = {"long":"LONG","short":"SHORT","weak_long":"weak LONG","weak_short":"weak SHORT",
            "near_long":"near LONG","near_short":"near SHORT"}.get(signal, signal)
    ax.set_title(f"{symbol} {tf_label} | {name}")
    ax.set_xlabel("bars (old → new)"); ax.set_ylabel("price")
    ax.legend(loc="best"); ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.45)
    buf=io.BytesIO(); fig.tight_layout(); fig.savefig(buf, format="png"); plt.close(fig); buf.seek(0); return buf.read()

def fmt_pct(x): return f"{x*100:.2f}%"

def filters_message(symbol, tf, side, price, strength_pct, rsi, ok15, ok1h, ema50_200_ok_flag):
    dot = "🟢" if side=="long" else "🔴"
    lines = [
        f"{dot} <b>{symbol}_{FUT_SUFFIX[1:]}:</b> фильтры {'ЗЕЛЁНЫЕ' if side=='long' else 'КРАСНЫЕ'}",
        f"{tf}: <b>{side.upper()}</b> • тренды 15m/1h {'OK' if (ok15 and ok1h) else '—'} • сила ≥ {fmt_pct(MIN_STRENGTH)}",
        f"Цена: {price:.6f} • RSI(14) {('≥' if side=='long' else '≤')}50 • EMA50/EMA200 {'OK' if ema50_200_ok_flag else '—'}",
        f"Текущее: сила={fmt_pct(strength_pct)} • RSI={rsi:.1f}"
    ]
    return "\n".join(lines)

def possible_entry_message(symbol, tf, side, price, strength_pct, rsi, ok15, ok1h, ema50_200_ok_flag):
    bolt = "⚡"
    lines = [
        f"{bolt} <b>Возможен вход {side.upper()}</b> по <b>{symbol}</b>",
        f"Цена: {price:.6f} • {tf}: <b>{side.upper()}</b>",
        f"Тренды 15m/1h: {'OK' if (ok15 and ok1h) else '—'} • Сила={fmt_pct(strength_pct)}",
        f"RSI(14)={'≥' if side=='long' else '≤'}50 → {rsi:.1f} • EMA50/EMA200 {'OK' if ema50_200_ok_flag else '—'}",
        "⏳ ждём подтверждения кросса EMA " + ("↑" if side=="long" else "↓"),
    ]
    return "\n".join(lines)

# ==== Основная логика
def process_symbol(symbol: str):
    raw = fetch_history_candles(symbol, BASE_TF_LABEL, CANDLES_LIMIT)
    pack = prepare_ohlc(raw)
    if not pack:
        print(f"[{symbol}] no data"); return
    ts, op, hi, lo, cl = pack
    if len(cl) < EMA_SLOW + 5:
        print(f"[{symbol}] not enough len {len(cl)}"); return

    em = ema_block(cl)
    sig, near, price = em["sig"], em["near"], em["price"]
    ef, es = em["ef"], em["es"]

    strength_pct = abs(ef - es) / price if price else 0.0
    rsi_series = rsi14(cl)
    rsi_val = rsi_series[-2] if rsi_series else None
    base_dir = "long" if ef >= es else "short"

    # confirmations on higher TFs
    ok15 = (ema_dir_on_tf(symbol, "15min") == base_dir)
    ok1h = (ema_dir_on_tf(symbol, "1hour") == base_dir)
    midbig_ok = ema50_200_ok(cl)

    # determine filters green
    filters_green = (
        ok15 and ok1h and
        strength_pct >= MIN_STRENGTH and
        (rsi_val is not None and ((rsi_val >= 50 and base_dir=="long") or (rsi_val <= 50 and base_dir=="short"))) and
        midbig_ok
    )

    # message when filters state changed
    if last_filters_green[symbol] is not filters_green:
        last_filters_green[symbol] = filters_green
        side = base_dir
        text = filters_message(symbol, BASE_TF_LABEL, side, price, strength_pct, rsi_val, ok15, ok1h, midbig_ok)
        send_telegram_text(text)

    # possible entry (filters ok but cross not yet)
    if filters_green and (sig in ("none", "weak_long", "weak_short") or (sig=="none" and near)):
        side = base_dir
        text = possible_entry_message(symbol, BASE_TF_LABEL, side, price, strength_pct, rsi_val, ok15, ok1h, midbig_ok)
        send_telegram_text(text)

    # if no signal and not near -> periodic heartbeat (optional)
    if sig == "none" and not near:
        now = int(time.time())
        if now - last_no_signal_ts[symbol] >= NO_SIGNAL_COOLDOWN_SEC:
            last_no_signal_ts[symbol] = now
            # comment out the next line if you don't want heartbeat messages:
            # send_telegram_text(f"⚪️ Нет сигнала ({symbol} {BASE_TF_LABEL})")
        return

    # anti-duplicate: don't resend same strong dir
    if sig in ("long","short") and last_cross_dir[symbol] == sig:
        return

    # Build caption and chart
    tp = price*(1+0.015) if sig=="long" else price*(1-0.015)  # fallback simple TP/SL
    sl = price*(1-0.01) if sig=="long" else price*(1+0.01)
    # Better SL/TP using local swing
    sl_calc, tp_calc = calc_sl_tp(cl, lo, hi, price, "long" if (sig=="long" or (near=="near_long")) else "short", lookback=20, buffer_pct=0.001)
    # choose if realistic (non-zero)
    if sl_calc and tp_calc:
        sl, tp = sl_calc, tp_calc

    cap = []
    if sig == "long":
        cap = [f"🟢 LONG <b>{symbol}</b> ({BASE_TF_LABEL})",
               f"Цена: <b>{price:.6f}</b> • EMA{EMA_FAST}={ef:.6f} | EMA{EMA_SLOW}={es:.6f}",
               f"🎯 TP: <b>{tp:.6f}</b> • 🛑 SL: <b>{sl:.6f}</b>"]
    elif sig == "short":
        cap = [f"🔴 SHORT <b>{symbol}</b> ({BASE_TF_LABEL})",
               f"Цена: <b>{price:.6f}</b> • EMA{EMA_FAST}={ef:.6f} | EMA{EMA_SLOW}={es:.6f}",
               f"🎯 TP: <b>{tp:.6f}</b> • 🛑 SL: <b>{sl:.6f}</b>"]
    elif sig in ("weak_long","weak_short") or near:
        tag = "🟡 Слабый LONG" if (sig=="weak_long" or near=="near_long") else "🟠 Слабый SHORT"
        cap = [f"{tag} <b>{symbol}</b> ({BASE_TF_LABEL})",
               f"Цена: <b>{price:.6f}</b> • EMA{EMA_FAST}={ef:.6f} | EMA{EMA_SLOW}={es:.6f}"]
    caption = "\n".join(cap)

    picture = chart_png(symbol, BASE_TF_LABEL, cl, em["ef_series"], em["es_series"],
                        sig if sig!="none" else near, price, tp, sl, tail=CHART_TAIL)
    send_telegram_photo(picture, caption)

    if sig in ("long","short"):
        last_cross_dir[symbol] = sig
        last_cross_ts[symbol]  = int(time.time())
        last_sent_ts[symbol] = int(time.time())

# helper to get ema dir for higher TFs
def ema_dir_on_tf(symbol, tf_label):
    raw = fetch_history_candles(symbol, tf_label, CANDLES_LIMIT)
    p = prepare_ohlc(raw)
    if not p: return None
    _,_,_,_,cl = p
    if len(cl) < EMA_SLOW + 3: return None
    return ema_dir(cl)

# ==== loop & Flask
def loop():
    send_telegram_text("🤖 Бот запущен (фильтры + EMA50/200 + RSI + TP/SL)")
    while True:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"[{stamp}] tick")
        for s in SYMBOLS:
            try:
                process_symbol(s); time.sleep(0.5)
            except Exception as e:
                print(f"[{s}] ex:", e)
        time.sleep(CHECK_INTERVAL_SEC)

app = Flask(__name__)

@app.route("/")
def health():
    return jsonify({"ok": True, "service":"filters+ema", "symbols":SYMBOLS, "base_tf":BASE_TF_LABEL, "confirm_tfs":CONFIRM_TF_LABELS})

@app.route("/check_now", methods=["POST","GET"])
def check_now():
    try:
        payload = request.json or {}
    except Exception:
        payload = {}
    sym = (payload.get("symbol") if isinstance(payload, dict) else None) or request.args.get("symbol")
    if sym:
        process_symbol(sym)
        return jsonify({"ok": True, "forced": sym})
    for s in SYMBOLS: process_symbol(s)
    return jsonify({"ok": True, "forced":"all"})

@app.route("/status")
def status():
    now=int(time.time())
    info={s:{
        "last_no_signal_min": round((now-last_no_signal_ts[s])/60,1) if last_no_signal_ts[s] else None,
        "last_cross": last_cross_dir[s],
        "last_cross_ago_min": round((now-last_cross_ts[s])/60,1) if last_cross_ts[s] else None,
        "last_sent_ago_min": round((now-last_sent_ts[s])/60,1) if last_sent_ts[s] else None,
        "filters_green": last_filters_green[s]
    } for s in SYMBOLS}
    return jsonify({"ok": True, "info": info})

@app.route("/config")
def config():
    return jsonify({
        "EMA": [EMA_FAST, EMA_SLOW],
        "min_strength_pct": MIN_STRENGTH,
        "check_interval_sec": CHECK_INTERVAL_SEC,
        "tfmap": TFMAP, "base_tf_label": BASE_TF_LABEL, "confirm_tfs": CONFIRM_TF_LABELS
    })

@app.route("/debug_once")
def debug_once():
    lines=[]
    for s in SYMBOLS:
        raw = fetch_history_candles(s, BASE_TF_LABEL, CANDLES_LIMIT)
        p = prepare_ohlc(raw)
        if not p: lines.append(f"{s}: no data"); continue
        ts,op,hi,lo,cl = p
        em = ema_block(cl); rsi_s = rsi14(cl)
        rsi_val = rsi_s[-2] if rsi_s else None
        strength = abs(em['ef']-em['es'])/em['price'] if em['price'] else 0
        lines.append(f"{s}: close={em['price']:.6f} ef={em['ef']:.6f} es={em['es']:.6f} sig={em['sig']} near={em['near']} strength={strength:.4%} RSI={None if rsi_val is None else round(rsi_val,1)}")
    send_telegram_text("🔎 DEBUG\n" + "\n".join(lines))
    return jsonify({"ok": True, "lines": lines})

def run_flask():
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    t = threading.Thread(target=loop, daemon=True); t.start()
    run_flask()
