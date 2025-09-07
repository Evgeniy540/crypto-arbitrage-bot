# -*- coding: utf-8 -*-
"""
EMA(9/21)+ATR сигнальный бот • KuCoin SPOT (FEATHER+ — чуть строже FEATHER++)
- мягкие фильтры + проверка наклона EMA, немного ужат bounce и "почти-кросс"
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
HEADERS        = {"User-Agent": "ema-kucoin-bot/feather-plus"}

app = Flask(__name__)
state = {
    "symbols": DEFAULT_SYMBOLS[:],
    "base_tf": "5m",
    "fallback_tf": "1m",
    "min_candles": 120,          # было 100
    "ema_fast": 9,
    "ema_slow": 21,

    # тайминги
    "check_s": 10,               # было 8
    "signal_cooldown_s": 300,    # было 240
    "no_sig_cooldown_s": 1800,
    "error_cooldown_s": 400,

    # фильтры (чуть строже)
    "eps_pct": 0.0010,           # было 0.0007 — ужали "почти-кросс" (≈0.10%)
    "atr_k":   0.10,             # без изменений
    "slope_min": 0.0000,         # было -0.0005 — не пропускаем явную "противо-наклонную" ерунду
    "slope21_min": 0.000020,     # было 0.000015 — EMA21 пусть будет каплю «выше/ниже»
    "dead_pct": 0.0003,          # было 0.0002 — мёртвую зону сделали шире
    "bounce_k": 0.30,            # было 0.40 — отскок ближе к EMA21 (строже)
    "slope_window": 4,           # окно, по которому меряем наклон (EMA[-1] vs EMA[-4])

    # анти-лимиты
    "batch_size": 12,
    "per_req_sleep": 0.18,
    "rr_index": 0,
    "max_retries": 3,
    "backoff_base": 0.6,

    # отчёты
    "report_enabled": True,
    "report_every_min": 90,

    # «нет сигналов» сводка
    "nosig_all_enabled": True,
    "nosig_all_every_min": 90,
    "nosig_all_min_age_min": 60,  # было 45
    "mode": "feather+"
}

cool_signal = defaultdict(float)
cool_no     = defaultdict(float)
cool_err    = defaultdict(float)
last_sig    = defaultdict(float)

# ===== Утилиты =====
def now_ts(): return time.time()
def fmt_dt(ts=None): return datetime.fromtimestamp(ts or now_ts()).strftime("%Y-%m-%d %H:%M:%S")

def send_tg(txt):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": txt, "parse_mode": "HTML"},
            timeout=10
        )
    except:
        pass

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
        except:
            if tries>=state["max_retries"]: raise
            time.sleep(state["backoff_base"]*(2**(tries-1))+random.uniform(0,0.05))

def fetch_candles(symbol, tf, want=300, drop_last=True):
    try:
        r=kucoin_get(KUCOIN_CANDLES,{"symbol":symbol,"type":tf_to_kucoin(tf)},timeout=10)
        j=r.json()
    except Exception as e:
        return None,str(e)
    if j.get("code")!="200000": return None,f"KuCoin error {j}"
    rows=[]
    for v in j.get("data",[]):
        try:
            # ts, open, high, low, close (KuCoin: [t, o, c, h, l, v, q, ...])
            rows.append((int(v[0]),float(v[1]),float(v[3]),float(v[4]),float(v[2])))
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

# ===== Лёгкий тренд-фильтр (наклон EMA) =====
def rel_slope(arr, price, win):
    """ относительный наклон EMA за окно win: (EMA[-1] - EMA[-win]) / price """
    if not arr or len(arr) < win or arr[-1] is None or arr[-win] is None or price <= 0:
        return None
    return (arr[-1] - arr[-win]) / price

def trend_ok(side, e9, e21, price):
    win = state["slope_window"]
    s9  = rel_slope(e9,  price, win)
    s21 = rel_slope(e21, price, win)
    if s9 is None or s21 is None: 
        return True  # не душим, если данных по наклону мало

    # пороги
    s_min   = state["slope_min"]      # требуемая «минимальная» величина для EMA9
    s21_min = state["slope21_min"]    # и для EMA21 (слабее)

    if side == "LONG":
        # хотим, чтобы EMA9 не падала, а EMA21 хотя бы слегка вверх
        if s9 < s_min: return False
        if s21 < s21_min: return False
        return True
    else:  # SHORT
        # зеркально: EMA9 вниз, EMA21 слегка вниз
        if s9 > -s_min: return False
        if s21 > -s21_min: return False
        return True

# ===== Сигнальная логика =====
def cross_or_near(e9,e21,price,eps_abs,dead_abs):
    if len(e9)<2 or len(e21)<2: return None
    prev = None
    if e9[-2] is not None and e21[-2] is not None:
        prev = e9[-2]-e21[-2]
    if e9[-1] is None or e21[-1] is None: 
        return None
    curr = e9[-1]-e21[-1]

    # "мёртвая" зона
    if abs(curr) < dead_abs:
        return None

    # чистый кросс
    if prev is not None and prev <= 0 < curr:  return "LONG","кросс ↑"
    if prev is not None and prev >= 0 > curr:  return "SHORT","кросс ↓"

    # почти-кросс (ужали eps_abs)
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
        # применяем мягкий тренд-фильтр
        if trend_ok(side, e9, e21, price):
            return side, note

    # если «кросса» нет — пробуем отскок от EMA21 (ужатый)
    a = atr_arr[-1] if atr_arr and atr_arr[-1] else None
    v = bounce_signal(e9,e21,price,a)
    if v:
        side, note = v
        if trend_ok(side, e9, e21, price):
            return side, note

    return None, "нет"

def check_symbol(sym):
    if now_ts()<cool_signal[sym]: 
        return
    for tf in (state["base_tf"],state["fallback_tf"]):
        candles,err=fetch_candles(sym,tf)
        if not candles: 
            return
        c=candles["c"]; h=candles["h"]; l=candles["l"]
        if len(c)<state["min_candles"]: 
            return
        e9 = ema(c, state["ema_fast"])
        e21= ema(c, state["ema_slow"])
        atr_a= atr(h,l,c)

        side,note=decide_signal(e9,e21,atr_a,c[-1])
        if side:
            cool_signal[sym]=now_ts()+state["signal_cooldown_s"]
            last_sig[sym]=now_ts()
            send_tg(f"📣 {sym} {side} @ {c[-1]} ({note}) {fmt_dt()}")
            return

def next_batch():
    syms=state["symbols"]; n=state["batch_size"]; i=state["rr_index"]%len(syms)
    batch=(syms+syms)[i:i+n]; state["rr_index"]=(i+n)%len(syms)
    return batch

def signals_worker():
    send_tg("🤖 KuCoin EMA бот (FEATHER+) запущен — фильтры слегка ужаты")
    while True:
        for s in next_batch():
            try:
                check_symbol(s)
            except Exception as e:
                print("check_symbol",s,e)
        time.sleep(state["check_s"])

@app.route("/")
def root(): return "ok"

if __name__=="__main__":
    threading.Thread(target=signals_worker,daemon=True).start()
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","10000")))
