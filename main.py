# -*- coding: utf-8 -*-
"""
Сигнальный бот (EMA 9/21 + ATR фильтр) для Bitget UMCBL.
— Без pandas/numPy.
— Работает на Render: Flask + фоновые потоки.
— Супер-устойчивый сбор свечей: пробует /candles, затем /candles с limit,
  и если нужно — /history-candles со startTime/endTime.
— Гасит спам ошибок (одно сообщение об ошибке на символ раз в 30 мин).
"""

import os
import time
import threading
from datetime import datetime, timezone
from collections import defaultdict
import requests
from flask import Flask

# ========= ТВОИ ДАННЫЕ =========
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
# ===============================

# Полные фьючерсные символы Bitget (USDT-M perpetual)
SYMBOLS = [
    "BTCUSDT_UMCBL",
    "ETHUSDT_UMCBL",
    "SOLUSDT_UMCBL",
    "XRPUSDT_UMCBL",
    "TRXUSDT_UMCBL",
]

# Базовые настройки
BASE_TF = "5m"
FALLBACK_TF = "15m"
CHECK_INTERVAL_S = 60
MIN_CANDLES = 120
EMA_FAST, EMA_SLOW = 9, 21

# Фильтры
EPS_PCT    = 0.0008   # близость EMA (0.08%)
ATR_FACTOR = 0.25     # ATR-фильтр
SLOPE_MIN  = 0.0

# Анти-спам
SIGNAL_COOLDOWN_S   = 300     # после сигнала
NO_SIGNAL_COOLDOWN  = 3600    # "нет сигнала"
ERROR_COOLDOWN      = 1800    # одно сообщение об ошибке/символ/полчаса

# Bitget PUBLIC API
HEADERS = {"User-Agent": "Mozilla/5.0 (ema-signal-bot/1.0)"}
URL_MIX_CANDLES        = "https://api.bitget.com/api/mix/v1/market/candles"
URL_MIX_HISTORY        = "https://api.bitget.com/api/mix/v1/market/history-candles"

# Глобальное состояние
app = Flask(__name__)
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

cooldown_signal = defaultdict(float)
cooldown_no_sig = defaultdict(float)
cooldown_error  = defaultdict(float)

# ---------- Утилиты ----------
def now_ts(): return time.time()
def now_ms(): return int(now_ts() * 1000)
def fmt_dt(ts=None): return datetime.fromtimestamp(ts or now_ts()).strftime("%Y-%m-%d %H:%M:%S")

def send_tg(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=12,
        )
    except Exception as e:
        print("Telegram error:", e)

def ema(series, period):
    if len(series) < period: return []
    k = 2.0/(period+1.0)
    out = [None]*(period-1)
    s0 = sum(series[:period]) / period
    out.append(s0)
    prev = s0
    for x in series[period:]:
        prev = x*k + prev*(1-k)
        out.append(prev)
    return out

def true_range(h,l,c):
    out=[None]
    for i in range(1, len(c)):
        out.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    return out

def atr(h,l,c,period=14):
    tr = true_range(h,l,c)
    vals = [x for x in tr if x is not None]
    if len(vals) < period: return [None]*len(c)
    k = 2.0/(period+1.0)
    out=[None]
    prev = sum(vals[:period])/period
    out.extend([None]*(len(c)-len(vals)-1))
    for i in range(period, len(vals)):
        prev = vals[i]*k + prev*(1-k)
        out.append(prev)
    while len(out) < len(c): out.append(prev)
    return out

def tf_to_seconds(tf: str) -> int:
    return {
        "1m":60, "5m":300, "15m":900, "30m":1800,
        "1h":3600, "4h":14400, "1d":86400
    }.get(tf, 300)

def parse_candles(data):
    rows=[]
    for row in data:
        try:
            ts = int(row[0]) / 1000.0
            o  = float(row[1]); h=float(row[2]); l=float(row[3]); c=float(row[4])
            rows.append((ts, o, h, l, c))
        except:
            pass
    rows.sort(key=lambda x: x[0])
    t=[r[0] for r in rows]
    o=[r[1] for r in rows]
    h=[r[2] for r in rows]
    l=[r[3] for r in rows]
    c=[r[4] for r in rows]
    return t,o,h,l,c

def bitget_get(url, params):
    r = requests.get(url, params=params, headers=HEADERS, timeout=20)
    j = r.json()
    if not isinstance(j, dict):
        return None, "Bad response"
    if j.get("code") != "00000":
        return None, f"Bitget error {j.get('code')}: {j.get('msg')}"
    return j.get("data", []), None

def fetch_candles(symbol: str, tf: str, want: int = 300):
    """
    Стратегия:
      1) /candles (symbol + granularity)
      2) /candles + limit
      3) /history-candles + startTime/endTime (чтобы точно получить пачку)
    Возвращает dict{t,o,h,l,c} либо (None, err).
    """
    gran = str(tf_to_seconds(tf))

    # 1) /candles без limit
    data, err = bitget_get(URL_MIX_CANDLES, {"symbol": symbol, "granularity": gran})
    if not err and data:
        t,o,h,l,c = parse_candles(data)
        if len(c) >= 50:
            return {"t":t,"o":o,"h":h,"l":l,"c":c}, None

    # 2) /candles c limit (некоторым регионам отдают больше)
    data, err2 = bitget_get(URL_MIX_CANDLES, {"symbol": symbol, "granularity": gran, "limit": str(min(500, want))})
    if not err2 and data:
        t,o,h,l,c = parse_candles(data)
        if len(c) >= 50:
            return {"t":t,"o":o,"h":h,"l":l,"c":c}, None

    # 3) /history-candles с окном времени
    end_ms   = now_ms()
    start_ms = end_ms - tf_to_seconds(tf) * want * 1000
    data, err3 = bitget_get(URL_MIX_HISTORY, {
        "symbol": symbol,
        "granularity": gran,
        "startTime": str(start_ms),
        "endTime": str(end_ms),
    })
    if not err3 and data:
        t,o,h,l,c = parse_candles(data)
        if len(c) >= 50:
            return {"t":t,"o":o,"h":h,"l":l,"c":c}, None

    # если ни один способ не сработал — вернём самую полезную ошибку
    return None, err or err2 or err3 or "No candles"

def throttle_err(sym: str, text: str):
    ts = now_ts()
    if ts - cooldown_error[sym] >= ERROR_COOLDOWN:
        cooldown_error[sym] = ts
        send_tg(text)

def maybe_no_signal(sym: str):
    ts = now_ts()
    if ts - cooldown_no_sig[sym] >= NO_SIGNAL_COOLDOWN:
        cooldown_no_sig[sym] = ts
        send_tg(f"ℹ️ По {sym} сейчас нет сигнала ({fmt_dt()}).")

def cross_signal(efast, eslow, eps_pct, slope_min, atr_arr, atr_k):
    if not efast or not eslow or efast[-1] is None or eslow[-1] is None:
        return None, "нет EMA"
    if len(efast) < 3 or len(eslow) < 3:
        return None, "мало EMA"

    df_prev = efast[-2] - eslow[-2] if efast[-2] is not None and eslow[-2] is not None else None
    df_curr = efast[-1] - eslow[-1]
    price   = efast[-1]
    eps_abs = price * eps_pct
    slope   = efast[-1] - (efast[-2] if efast[-2] is not None else efast[-1])

    a = atr_arr[-1] if atr_arr and atr_arr[-1] is not None else None
    if slope < slope_min:
        return None, "slope низкий"
    if a is not None and abs(df_curr) < a*atr_k:
        return None, "diff < ATR*k"

    if df_prev is not None and (df_prev <= 0 < df_curr):
        return "LONG", "кросс вверх"
    if df_prev is not None and (df_prev >= 0 > df_curr):
        return "SHORT", "кросс вниз"
    if abs(df_curr) <= eps_abs:
        return ("LONG" if slope > 0 else "SHORT"), "близко к кроссу"
    return None, "нет условия"

def signal_text(sym, side, price, tf, note):
    arrow = "🟢 LONG" if side=="LONG" else "🔴 SHORT"
    return (f"📣 <b>{sym}</b> | TF <b>{tf}</b>\n"
            f"{arrow} | Цена ~ <b>{price:.4f}</b>\n"
            f"Причина: {note}\n{fmt_dt()}")

def check_symbol(sym: str):
    if now_ts() < cooldown_signal[sym]:
        return

    # пробуем базовый ТФ, потом запасной
    for tf in (state["base_tf"], state["fallback_tf"]):
        candles, err = fetch_candles(sym, tf, want=max(300, state["min_candles"]+50))
        if candles:
            t,o,h,l,c = candles["t"], candles["o"], candles["h"], candles["l"], candles["c"]
            if len(c) < state["min_candles"]:
                maybe_no_signal(sym); return
            efast = ema(c, state["ema_fast"])
            eslow = ema(c, state["ema_slow"])
            atr_a = atr(h, l, c, period=14)
            side, note = cross_signal(efast, eslow, state["eps_pct"], state["slope_min"], atr_a, state["atr_k"])
            if side:
                cooldown_signal[sym] = now_ts() + state["signal_cooldown_s"]
                send_tg(signal_text(sym, side, c[-1], tf, note))
            else:
                maybe_no_signal(sym)
            return
        else:
            # сообщим об ошибке, но без спама
            throttle_err(sym, f"❌ Ошибка {sym}: {err}")

def apply_mode(mode: str):
    if str(mode).lower() == "ultra":
        state["eps_pct"] = 0.0005
        state["atr_k"]   = 0.35
        state["mode"]    = "ultra"
    else:
        state["eps_pct"] = 0.0008
        state["atr_k"]   = 0.25
        state["mode"]    = "normal"

# ---------- Telegram команды (через getUpdates) ----------
def handle_command(text: str):
    t = text.strip()
    if t.startswith("/mode"):
        parts = t.split()
        apply_mode(parts[1] if len(parts) >= 2 else "normal")
        send_tg(f"✅ mode={state['mode']} | eps={state['eps_pct']} | atr_k={state['atr_k']}")
        return
    if t.startswith("/status"):
        send_tg("🩺 Статус:\n"
                f"symbols: {', '.join(state['symbols'])}\n"
                f"tf: {state['base_tf']} (fb {state['fallback_tf']})\n"
                f"check: {state['check_s']}s, min: {state['min_candles']}\n"
                f"eps: {state['eps_pct']}, atr_k: {state['atr_k']}\n"
                f"cooldown: {state['signal_cooldown_s']}s\n"
                f"time: {fmt_dt()}")
        return
    if t.startswith("/setcooldown"):
        try:
            v = int(t.split()[1]); state["signal_cooldown_s"] = max(60, min(3600, v))
            send_tg(f"🧊 signal_cooldown={state['signal_cooldown_s']}s")
        except: send_tg("Формат: /setcooldown 300")
        return
    if t.startswith("/settf"):
        try:
            v = t.split()[1].lower()
            if v not in ("1m","5m","15m","30m","1h","4h","1d"): raise ValueError
            state["base_tf"] = v
            send_tg(f"⏱ TF = {state['base_tf']}")
        except: send_tg("Формат: /settf 5m | 15m | 1h | 4h | 1d")
        return
    if t.startswith("/setsymbols"):
        try:
            payload = t.split(None,1)[1]
            items = [x.strip().upper() for x in payload.replace(",", " ").split() if x.strip()]
            # ожидаются полные названия *_UMCBL
            state["symbols"] = items
            send_tg(f"✅ SYMBOLS:\n{', '.join(state['symbols'])}")
        except: send_tg("Формат: /setsymbols BTCUSDT_UMCBL ETHUSDT_UMCBL ...")
        return
    if t.startswith("/help"):
        send_tg("Команды: /status, /mode ultra|normal, /setcooldown N, /settf TF, /setsymbols ...")
        return

def tg_loop():
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    offset = None
    while True:
        try:
            params = {"timeout": 20}
            if offset is not None: params["offset"] = offset
            j = requests.get(url, params=params, timeout=25).json()
            if j.get("ok"):
                for upd in j.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or upd.get("edited_message")
                    if not msg: continue
                    if str(msg["chat"]["id"]) != TELEGRAM_CHAT_ID: continue
                    text = msg.get("text","")
                    if text: handle_command(text)
        except Exception as e:
            print("tg loop error:", e)
        time.sleep(1)

# ---------- Основные потоки ----------
def worker():
    send_tg("🤖 Бот запущен (EMA/RSI/ATR сигнальный). Доступные команды: /status, /setcooldown, /settf, /setsymbols, /help")
    # стартовая диагностика
    test, err = fetch_candles("BTCUSDT_UMCBL", state["base_tf"], 120)
    send_tg("✅ Bitget: candles OK." if test else f"⚠️ Диагностика Bitget: {err}")

    while True:
        start = now_ts()
        for sym in state["symbols"]:
            try:
                check_symbol(sym)
            except Exception as e:
                print("check_symbol error", sym, e)
        # удерживаем периодичность
        time.sleep(max(2.0, state["check_s"] - (now_ts() - start)))

# ---------- Flask (для Render) ----------
app = Flask(__name__)

@app.route("/")
def root():
    return "ok"

@app.route("/ping")
def ping():
    return {"ok": True, "time": fmt_dt()}

def run_threads():
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=tg_loop, daemon=True).start()

if __name__ == "__main__":
    run_threads()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
