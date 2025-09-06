# -*- coding: utf-8 -*-
"""
EMA(9/21)+ATR сигнальный бот • KuCoin SPOT (FEATHER++ — ультра-мягкие фильтры)
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
HEADERS        = {"User-Agent": "ema-kucoin-bot/featherpp"}

app = Flask(__name__)
state = {
    "symbols": DEFAULT_SYMBOLS[:],
    "base_tf": "5m",
    "fallback_tf": "1m",
    "min_candles": 100,
    "ema_fast": 9,
    "ema_slow": 21,

    # тайминги
    "check_s": 8,
    "signal_cooldown_s": 240,
    "no_sig_cooldown_s": 1800,
    "error_cooldown_s": 400,

    # фильтры ультра-мягкие
    "eps_pct": 0.0007,
    "atr_k":   0.10,
    "slope_min": -0.0005,
    "slope21_min": 0.000015,
    "dead_pct": 0.0002,
    "bounce_k": 0.40,

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
    "nosig_all_min_age_min": 45,
    "mode": "feather++"
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
    except: pass

def ema(series, period):
    if len(series)<period: return []
    k=2.0/(period+1.0)
    out=[None]*(period-1)
    prev=sum(series[:period])/period
    out.append(prev)
    for x in series[period:]:
        prev=x*k+prev*(1-k)
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
    except Exception as e: return None,str(e)
    if j.get("code")!="200000": return None,f"KuCoin error {j}"
    rows=[]
    for v in j.get("data",[]):
        try: rows.append((int(v[0]),float(v[1]),float(v[3]),float(v[4]),float(v[2])))
        except: pass
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

def cross_or_near(e9,e21,price,eps_abs,dead_abs):
    if len(e9)<2 or len(e21)<2: return None
    prev=e9[-2]-e21[-2] if e9[-2] and e21[-2] else None
    curr=e9[-1]-e21[-1]
    if abs(curr)<dead_abs: return None
    if prev is not None and prev<=0<curr: return "LONG","кросс ↑"
    if prev is not None and prev>=0>curr: return "SHORT","кросс ↓"
    if abs(curr)<=eps_abs: return ("LONG" if (e9[-1]-e9[-2]>=0) else "SHORT"),"почти кросс"
    return None

def bounce_signal(e9,e21,price,atr_val):
    if e21[-1] is None or atr_val is None: return None
    diff=abs(price-e21[-1])
    if diff<=state["bounce_k"]*atr_val:
        return ("LONG","отскок ↑") if e9[-1]>=e21[-1] else ("SHORT","отскок ↓")
    return None

def decide_signal(e9,e21,atr_arr,price):
    eps_abs=price*state["eps_pct"]; dead_abs=price*state["dead_pct"]
    v=cross_or_near(e9,e21,price,eps_abs,dead_abs)
    if v: return v
    a=atr_arr[-1] if atr_arr and atr_arr[-1] else None
    v=bounce_signal(e9,e21,price,a)
    if v: return v
    return None,"нет"

def check_symbol(sym):
    if now_ts()<cool_signal[sym]: return
    for tf in (state["base_tf"],state["fallback_tf"]):
        candles,err=fetch_candles(sym,tf)
        if not candles: return
        c=candles["c"]; h=candles["h"]; l=candles["l"]
        if len(c)<state["min_candles"]: return
        e9=ema(c,9); e21=ema(c,21); atr_a=atr(h,l,c)
        side,note=decide_signal(e9,e21,atr_a,c[-1])
        if side:
            cool_signal[sym]=now_ts()+state["signal_cooldown_s"]
            last_sig[sym]=now_ts()
            send_tg(f"📣 {sym} {side} @ {c[-1]} ({note}) {fmt_dt()}"); return

def next_batch():
    syms=state["symbols"]; n=state["batch_size"]; i=state["rr_index"]%len(syms)
    batch=(syms+syms)[i:i+n]; state["rr_index"]=(i+n)%len(syms)
    return batch

def signals_worker():
    send_tg("🤖 KuCoin EMA бот (FEATHER++) запущен")
    while True:
        for s in next_batch(): 
            try: check_symbol(s)
            except Exception as e: print("check_symbol",s,e)
        time.sleep(state["check_s"])

@app.route("/")
def root(): return "ok"

if __name__=="__main__":
    threading.Thread(target=signals_worker,daemon=True).start()
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","10000")))
