# -*- coding: utf-8 -*-
"""
EMA(9/21)+ATR сигнальный бот • KuCoin SPOT
— Свечи: GET /api/v1/market/candles  (symbol=BTC-USDT, type=5min|15min|1hour|4hour|1day, optional startAt/endAt)
— Telegram-команды: /status, /mode, /setcooldown, /settf, /setsymbols, /help
— Flask для Render keep-alive + два фоновых потока (воркер сигналов, обработчик команд)
— Зависимости: requests, Flask
"""

import os
import time
import threading
from datetime import datetime
from collections import defaultdict

import requests
from flask import Flask

# ========== ТВОИ ДАННЫЕ ==========
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
# =================================

# Символы KuCoin SPOT: ОБЯЗАТЕЛЬНО с дефисом!
SYMBOLS = [
    "BTC-USDT",
    "ETH-USDT",
    "SOL-USDT",
    "XRP-USDT",
    "TRX-USDT",
]

# Настройки стратегии / бота
BASE_TF, FALLBACK_TF = "5m", "15m"      # основной и резервный ТФ
CHECK_INTERVAL_S     = 60               # раз в сколько секунд проверяем символы
MIN_CANDLES          = 120              # минимум свечей для расчётов
EMA_FAST, EMA_SLOW   = 9, 21
EPS_PCT, ATR_FACTOR  = 0.0008, 0.25     # фильтры близости и волатильности
SLOPE_MIN            = 0.0              # минимальный наклон EMA9 для допуска
SIGNAL_COOLDOWN_S    = 300              # антиспам по сигналам
NO_SIGNAL_COOLDOWN   = 3600             # напоминание "сигнала нет"
ERROR_COOLDOWN       = 1800             # антиспам ошибок на символ

# KuCoin API
KUCOIN_BASE    = "https://api.kucoin.com"
KUCOIN_CANDLES = KUCOIN_BASE + "/api/v1/market/candles"
HEADERS        = {"User-Agent": "ema-kucoin-bot/1.0"}

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
def fmt_dt(ts=None): return datetime.fromtimestamp(ts or now_ts()).strftime("%Y-%m-%d %H:%M:%S")

def send_tg(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=12
        )
    except Exception as e:
        print("Telegram error:", e)

def ema(series, period):
    if len(series) < period: return []
    k = 2.0/(period+1.0)
    out = [None]*(period-1)
    s0 = sum(series[:period])/period
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
    # первый ATR как SMA, далее EMA
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

def tf_to_kucoin(tf: str) -> str:
    return {
        "1m": "1min",
        "5m": "5min",
        "15m": "15min",
        "30m": "30min",
        "1h": "1hour",
        "4h": "4hour",
        "1d": "1day",
    }.get(tf, "5min")

# ---------- KuCoin candles ----------
def fetch_candles(symbol: str, tf: str, want: int = 300):
    """
    Получаем свечи с KuCoin SPOT.
    symbol — 'BTC-USDT', type — '5min'/'1hour'/...
    Возвращаем t,o,h,l,c в возрастающем порядке времени.
    """
    params = {
        "symbol": symbol,
        "type": tf_to_kucoin(tf),
        # По документации startAt/endAt опциональны. По умолчанию KuCoin отдаёт
        # достаточное число последних свечей (до 1500).
    }
    try:
        r = requests.get(KUCOIN_CANDLES, params=params, headers=HEADERS, timeout=15)
        j = r.json()
    except Exception as e:
        return None, f"KuCoin bad response: {e}"

    if not isinstance(j, dict) or j.get("code") != "200000":
        # покажем кусок тела, если это не JSON от KuCoin
        body = ""
        try: body = r.text[:160]
        except: pass
        return None, f"KuCoin error {j.get('code') if isinstance(j, dict) else '??'}: {j.get('msg') if isinstance(j, dict) else body}"

    data = j.get("data", [])
    if not data:
        return None, "KuCoin: no candles"

    # формат KuCoin: [time, open, close, high, low, volume]
    rows = []
    for row in data:
        try:
            ts = int(row[0])                 # seconds
            o  = float(row[1]); c = float(row[2])
            h  = float(row[3]); l = float(row[4])
            rows.append((ts, o, h, l, c))
        except:
            pass

    if not rows:
        return None, "KuCoin: empty after parse"

    rows.sort(key=lambda x: x[0])
    t=[r[0] for r in rows]; o=[r[1] for r in rows]; h=[r[2] for r in rows]; l=[r[3] for r in rows]; c=[r[4] for r in rows]
    # обрежем хвост, если нужно строго want
    if len(c) > want:
        t, o, h, l, c = t[-want:], o[-want:], h[-want:], l[-want:], c[-want:]
    return {"t":t,"o":o,"h":h,"l":l,"c":c}, None

# ---------- Логика сигналов ----------
def cross_signal(efast, eslow, eps_pct, slope_min, atr_arr, atr_k):
    if not efast or not eslow or efast[-1] is None or eslow[-1] is None: return None, "нет EMA"
    if len(efast) < 3 or len(eslow) < 3: return None, "мало EMA"

    df_prev = efast[-2]-eslow[-2] if efast[-2] is not None and eslow[-2] is not None else None
    df_curr = efast[-1]-eslow[-1]
    price   = efast[-1]
    eps_abs = price*eps_pct
    slope   = (efast[-1] - (efast[-2] if efast[-2] is not None else efast[-1]))
    a = atr_arr[-1] if atr_arr and atr_arr[-1] is not None else None

    if slope < slope_min: return None, "slope низкий"
    if a is not None and abs(df_curr) < a*atr_k: return None, "diff < ATR*k"
    if df_prev is not None and (df_prev <= 0 < df_curr): return "LONG","кросс вверх"
    if df_prev is not None and (df_prev >= 0 > df_curr): return "SHORT","кросс вниз"
    if abs(df_curr) <= eps_abs: return ("LONG" if slope>0 else "SHORT"), "близко к кроссу"
    return None, "нет условия"

def maybe_no_signal(sym: str):
    ts=now_ts()
    if ts - cooldown_no_sig[sym] >= NO_SIGNAL_COOLDOWN:
        cooldown_no_sig[sym]=ts
        send_tg(f"ℹ️ По {sym} сейчас нет сигнала ({fmt_dt()}).")

def make_signal_text(sym, side, price, tf, note):
    arrow = "🟢 LONG" if side=="LONG" else "🔴 SHORT"
    return (f"📣 <b>{sym}</b> | TF <b>{tf}</b>\n"
            f"{arrow} | Цена ~ <b>{price:.4f}</b>\n"
            f"Причина: {note}\n{fmt_dt()}")

def check_symbol(sym: str):
    if now_ts() < cooldown_signal[sym]:
        return
    for tf in (state["base_tf"], state["fallback_tf"]):
        candles, err = fetch_candles(sym, tf, want=max(300, state["min_candles"]+50))
        if candles:
            t,o,h,l,c = candles["t"], candles["o"], candles["h"], candles["l"], candles["c"]
            if len(c) < state["min_candles"]:
                maybe_no_signal(sym)
                return
            efast = ema(c, state["ema_fast"])
            eslow = ema(c, state["ema_slow"])
            atr_a = atr(h, l, c, period=14)
            side, note = cross_signal(efast, eslow, state["eps_pct"], state["slope_min"], atr_a, state["atr_k"])
            if side:
                cooldown_signal[sym] = now_ts() + state["signal_cooldown_s"]
                send_tg(make_signal_text(sym, side, c[-1], tf, note))
            else:
                maybe_no_signal(sym)
            return
        else:
            ts=now_ts()
            if ts - cooldown_error[sym] >= ERROR_COOLDOWN:
                cooldown_error[sym]=ts
                send_tg(f"❌ Ошибка {sym}: {err}")

def apply_mode(mode: str):
    if str(mode).lower() == "ultra":
        state["eps_pct"]=0.0005; state["atr_k"]=0.35; state["mode"]="ultra"
    else:
        state["eps_pct"]=0.0008; state["atr_k"]=0.25; state["mode"]="normal"

# ---------- Telegram-команды ----------
def handle_command(text: str):
    t=text.strip()
    if t.startswith("/mode"):
        parts=t.split()
        apply_mode(parts[1] if len(parts)>=2 else "normal")
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
            v=int(t.split()[1]); state["signal_cooldown_s"]=max(60,min(3600,v))
            send_tg(f"🧊 cooldown={state['signal_cooldown_s']}s")
        except:
            send_tg("Формат: /setcooldown 300")
        return
    if t.startswith("/settf"):
        try:
            v=t.split()[1].lower()
            if v not in ("1m","5m","15m","30m","1h","4h","1d"): raise ValueError
            state["base_tf"]=v
            send_tg(f"⏱ TF = {state['base_tf']}")
        except:
            send_tg("Формат: /settf 5m | 15m | 1h | 4h | 1d")
        return
    if t.startswith("/setsymbols"):
        try:
            payload=t.split(None,1)[1]
            items=[x.strip().upper() for x in payload.replace(","," ").split() if x.strip()]
            # для KuCoin символы вида BTC-USDT
            state["symbols"]=items
            send_tg(f"✅ SYMBOLS:\n{', '.join(state['symbols'])}")
        except:
            send_tg("Формат: /setsymbols BTC-USDT ETH-USDT ...")
        return
    if t.startswith("/help"):
        send_tg("Команды: /status, /mode ultra|normal, /setcooldown N, /settf TF, /setsymbols ...")
        return

def tg_loop():
    url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    offset=None
    while True:
        try:
            params={"timeout":20}
            if offset is not None: params["offset"]=offset
            j=requests.get(url, params=params, timeout=25).json()
            if j.get("ok"):
                for upd in j.get("result", []):
                    offset=upd["update_id"]+1
                    msg=upd.get("message") or upd.get("edited_message")
                    if not msg: continue
                    if str(msg["chat"]["id"])!=TELEGRAM_CHAT_ID: continue
                    text=msg.get("text","")
                    if text: handle_command(text)
        except Exception as e:
            print("tg loop error:", e)
        time.sleep(1)

# ---------- Основной воркер + Flask ----------
def worker():
    send_tg("🤖 Бот запущен (EMA/ATR KuCoin SPOT). Команды: /status, /setcooldown, /settf, /setsymbols, /help")
    test, err = fetch_candles("BTC-USDT", state["base_tf"], 200)
    send_tg("✅ KuCoin: candles OK." if test else f"⚠️ Диагностика KuCoin: {err}")
    while True:
        start=now_ts()
        for sym in state["symbols"]:
            try:
                check_symbol(sym)
            except Exception as e:
                print("check_symbol error", sym, e)
        time.sleep(max(2.0, state["check_s"]-(now_ts()-start)))

@app.route("/")
def root(): return "ok"

@app.route("/ping")
def ping(): return {"ok": True, "time": fmt_dt()}

def run_threads():
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=tg_loop, daemon=True).start()

if __name__ == "__main__":
    run_threads()
    port=int(os.environ.get("PORT","10000"))
    app.run(host="0.0.0.0", port=port)
