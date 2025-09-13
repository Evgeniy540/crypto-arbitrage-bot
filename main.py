# -*- coding: utf-8 -*-
"""
EMA(9/21)+ATR сигнальный бот • KuCoin SPOT (FEATHER++)
— ультра-мягкие фильтры + лёгкий тренд-фильтр, heartbeat «нет сигнала»
— готов для Render (держим процесс живым через Flask app.run)
"""

import os, time, threading, requests, random
from datetime import datetime
from collections import defaultdict
from flask import Flask

# === ТВОИ ДАННЫЕ ===
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
# ===================

DEFAULT_SYMBOLS = [
    "BTC-USDT","ETH-USDT","BNB-USDT","SOL-USDT","XRP-USDT","ADA-USDT","DOGE-USDT","TRX-USDT",
    "TON-USDT","LINK-USDT","LTC-USDT","DOT-USDT","ARB-USDT","OP-USDT","PEPE-USDT","SHIB-USDT"
]

KUCOIN_BASE    = "https://api.kucoin.com"
KUCOIN_CANDLES = KUCOIN_BASE + "/api/v1/market/candles"
HEADERS        = {"User-Agent": "ema-kucoin-bot/feather-plus-plus"}

app = Flask(__name__)

state = {
    "symbols": DEFAULT_SYMBOLS[:],

    # Таймфреймы
    "base_tf": "5m",          # основной ТФ
    "fallback_tf": "1m",      # запасной ТФ

    # История и EMA
    "min_candles": 120,       # >=21 для EMA21, запас для сглаживания
    "ema_fast": 9,
    "ema_slow": 21,

    # Тайминги
    "check_s": 10,                 # пауза между итерациями
    "signal_cooldown_s": 180,      # антиспам по символу (сек)
    "error_cooldown_s": 400,

    # Мягкие фильтры FEATHER++ (ослаблено)
    "eps_pct": 0.0007,        # «почти-кросс» ±0.07% от цены
    "dead_pct": 0.0002,       # мёртвая зона
    "bounce_k": 0.40,         # отскок от EMA21
    "atr_k":   0.10,          # ATR-фактор (оставлен)

    # Лёгкий тренд-фильтр (наклоны EMA)
    "slope_window": 4,        # сравниваем EMA[-1] vs EMA[-4]
    "slope_min":   -0.0005,   # допускаем очень слабый/чуть отриц. наклон EMA9
    "slope21_min":  0.000015, # требование к EMA21 смягчено

    # Анти-лимиты
    "batch_size": 12,
    "per_req_sleep": 0.18,
    "rr_index": 0,
    "max_retries": 3,
    "backoff_base": 0.6,

    # Heartbeat «нет сигналов»
    "nosig_all_enabled": True,
    "nosig_all_every_min": 60,     # как часто можно слать «нет сигнала»
    "nosig_all_min_age_min": 45,   # минимум с прошлого реального сигнала

    # Периодический отчёт
    "report_enabled": True,
    "report_every_min": 120,

    "mode": "feather++"
}

# Кулдауны/таймстемпы
cool_signal = defaultdict(float)
cool_err    = defaultdict(float)
last_sig    = defaultdict(float)
last_nosig  = defaultdict(float)

# ---------- Утилиты ----------
def now_ts(): return time.time()
def fmt_dt(ts=None): return datetime.fromtimestamp(ts or now_ts()).strftime("%Y-%m-%d %H:%M:%S")

def send_tg(txt):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": txt, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print("send_tg error:", e)

def ema(series, period):
    if len(series) < period: return []
    k = 2.0 / (period + 1.0)
    out = [None] * (period - 1)
    prev = sum(series[:period]) / period
    out.append(prev)
    for x in series[period:]:
        prev = x * k + prev * (1 - k)
        out.append(prev)
    return out

def atr(h,l,c,period=14):
    tr=[None]
    for i in range(1,len(c)):
        tr.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    vals=[x for x in tr if x is not None]
    if len(vals)<period: return [None]*len(c)
    k=2.0/(period+1.0); prev=sum(vals[:period])/period
    out=[None]*(len(c)-len(vals))+[prev]
    for v in vals[period:]:
        prev=v*k+prev*(1-k); out.append(prev)
    return out

def tf_to_kucoin(tf):
    return {"1m":"1min","5m":"5min","15m":"15min","30m":"30min",
            "1h":"1hour","4h":"4hour","1d":"1day"}.get(tf,"5min")

def tf_seconds(tf):
    return {"1m":60,"5m":300,"15m":900,"30m":1800,"1h":3600,"4h":14400,"1d":86400}.get(tf,300)

def kucoin_get(url, params, timeout=10):
    tries=0
    while True:
        tries+=1
        try:
            r=requests.get(url,params=params,headers=HEADERS,timeout=timeout)
            if r.status_code==429: raise RuntimeError("429 Too many requests")
            return r
        except Exception as e:
            if tries>=state["max_retries"]: raise
            time.sleep(state["backoff_base"]*(2**(tries-1))+random.uniform(0,0.05))

def fetch_candles(symbol, tf, want=300, drop_last=True):
    """KuCoin candles: data = [[t, o, c, h, l, v, q, ...], ...]"""
    try:
        r=kucoin_get(KUCOIN_CANDLES,{"symbol":symbol,"type":tf_to_kucoin(tf)},timeout=10)
        j=r.json()
    except Exception as e:
        return None,f"req:{e}"
    if j.get("code")!="200000": return None,f"KuCoin error {j}"
    rows=[]
    for v in j.get("data",[]):
        try:
            rows.append((int(v[0]),float(v[1]),float(v[3]),float(v[4]),float(v[2])))  # t,o,h,l,c
        except:
            pass
    if not rows: return None,"empty"
    rows.sort()
    if drop_last and len(rows)>1:
        t_last=rows[-1][0]//1000
        if now_ts()-t_last<tf_seconds(tf): rows=rows[:-1]
    if not rows: return None,"only-unclosed"
    rows=rows[-want:]
    t=[x[0] for x in rows]; o=[x[1] for x in rows]; h=[x[2] for x in rows]; l=[x[3] for x in rows]; c=[x[4] for x in rows]
    time.sleep(state["per_req_sleep"])
    return {"t":t,"o":o,"h":h,"l":l,"c":c},None

# ---------- Лёгкий тренд-фильтр ----------
def rel_slope(arr, price, win):
    """ Относительный наклон EMA за окно win: (EMA[-1] - EMA[-win]) / price """
    if not arr or len(arr) < win or arr[-1] is None or arr[-win] is None or price <= 0:
        return None
    return (arr[-1] - arr[-win]) / price

def trend_ok(side, e9, e21, price):
    win = state["slope_window"]
    s9  = rel_slope(e9,  price, win)
    s21 = rel_slope(e21, price, win)
    if s9 is None or s21 is None:
        return True  # не душим, если по наклону данных мало
    s_min   = state["slope_min"]
    s21_min = state["slope21_min"]
    if side == "LONG":
        if s9 < s_min:     return False
        if s21 < s21_min:  return False
        return True
    else:  # SHORT
        if s9 > -s_min:    return False
        if s21 > -s21_min: return False
        return True

# ---------- Сигналы ----------
def cross_or_near(e9,e21,price,eps_abs,dead_abs):
    if len(e9)<2 or len(e21)<2: return None
    prev = None
    if e9[-2] is not None and e21[-2] is not None:
        prev = e9[-2]-e21[-2]
    if e9[-1] is None or e21[-1] is None:
        return None
    curr = e9[-1]-e21[-1]

    # мёртвая зона
    if abs(curr) < dead_abs:
        return None

    # кросс
    if prev is not None and prev <= 0 < curr:  return "LONG","кросс ↑"
    if prev is not None and prev >= 0 > curr:  return "SHORT","кросс ↓"

    # почти-кросс (мягко)
    if abs(curr) <= eps_abs:
        side = "LONG" if (e9[-1] - (e9[-2] if e9[-2] is not None else e9[-1])) >= 0 else "SHORT"
        return side, "почти кросс"

    return None

def bounce_signal(e9,e21,price,atr_val):
    if e21[-1] is None or atr_val is None: return None
    diff=abs(price-e21[-1])
    if diff<=state["bounce_k"]*atr_val:
        return ("LONG","отскок ↑") if e9[-1]>=e21[-1] else ("SHORT","отскок ↓")
    return None

def decide_signal(e9,e21,atr_arr,price):
    eps_abs = price * state["eps_pct"]
    dead_abs= price * state["dead_pct"]

    v = cross_or_near(e9,e21,price,eps_abs,dead_abs)
    if v:
        side, note = v
        if trend_ok(side, e9, e21, price):
            return side, note

    # если кросса нет — пробуем отскок от EMA21
    a = atr_arr[-1] if atr_arr and atr_arr[-1] else None
    v = bounce_signal(e9,e21,price,a)
    if v:
        side, note = v
        if trend_ok(side, e9, e21, price):
            return side, note

    return None, "нет"

# ---------- Проверка символа ----------
def heartbeat_no_signal(sym, tf_used, candles_count):
    """Шлём «нет сигнала» не чаще nosig_all_every_min
       и только если с реального сигнала прошло >= nosig_all_min_age_min."""
    if not state["nosig_all_enabled"]:
        return
    now = now_ts()
    # антиспам по «нет сигналов»
    if now < last_nosig[sym] + state["nosig_all_every_min"]*60:
        return
    # ждём после реального сигнала
    if now < last_sig[sym] + state["nosig_all_min_age_min"]*60:
        return
    last_nosig[sym] = now
    send_tg(f"ℹ️ {sym}: нет сигнала (tf={tf_used}, свечей={candles_count}) {fmt_dt()}")

def check_symbol_on_tf(sym, tf):
    candles, err = fetch_candles(sym, tf)
    if not candles:
        return None, f"no_candles:{err}", 0
    c=candles["c"]; h=candles["h"]; l=candles["l"]
    if len(c) < state["min_candles"]:
        return None, "few_candles", len(c)
    e9  = ema(c, state["ema_fast"])
    e21 = ema(c, state["ema_slow"])
    atr_a = atr(h,l,c)
    side, note = decide_signal(e9, e21, atr_a, c[-1])
    if side:
        return (side, note, c[-1]), None, len(c)
    return None, "no_signal", len(c)

def check_symbol(sym):
    now = now_ts()
    if now < cool_signal[sym]:
        return

    for tf in (state["base_tf"], state["fallback_tf"]):
        try:
            res, reason, n_candles = check_symbol_on_tf(sym, tf)
        except Exception as e:
            if now > cool_err[sym]:
                send_tg(f"⚠️ Ошибка {sym} ({tf}): {e} {fmt_dt()}")
                cool_err[sym] = now + state["error_cooldown_s"]
            return

        if res:
            side, note, px = res
            cool_signal[sym] = now + state["signal_cooldown_s"]
            last_sig[sym]    = now
            send_tg(f"📣 {sym} {side} @ {px} ({note}, tf={tf}) {fmt_dt()}")
            return
        else:
            # если на этом ТФ нет — возможно пришлём heartbeat
            heartbeat_no_signal(sym, tf, n_candles)

def next_batch():
    syms=state["symbols"]; n=state["batch_size"]; i=state["rr_index"]%len(syms)
    batch=(syms+syms)[i:i+n]; state["rr_index"]=(i+n)%len(syms)
    return batch

# ---------- Воркеры ----------
def signals_worker():
    send_tg(f"🤖 KuCoin EMA бот ({state['mode'].upper()}) запущен — фильтры мягкие, heartbeat включён")
    while True:
        for s in next_batch():
            try:
                check_symbol(s)
            except Exception as e:
                print("check_symbol", s, e)
        time.sleep(state["check_s"])

def reporter_worker():
    if not state["report_enabled"]:
        return
    last_report = 0.0
    while True:
        time.sleep(5)
        if now_ts() - last_report < state["report_every_min"]*60:
            continue
        last_report = now_ts()
        alive = len(state["symbols"])
        send_tg(f"🩺 Отчёт: символов={alive}, tf={state['base_tf']}→{state['fallback_tf']}, "
                f"cooldown={state['signal_cooldown_s']}s, режим={state['mode']} {fmt_dt()}")

# ---------- HTTP ----------
@app.route("/")
def root():
    return "ok"

# ---------- MAIN ----------
if __name__=="__main__":
    threading.Thread(target=signals_worker, daemon=True).start()
    threading.Thread(target=reporter_worker, daemon=True).start()
    # Держим процесс живым на Render/Replit
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
