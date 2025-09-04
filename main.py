"""
EMA(9/21)+ATR сигнальный бот • KuCoin SPOT
— анти-лимиты KuCoin: батчи монет, троттлинг между запросами, ретраи при 429
— режимы/пресеты: /mode, /soft, /hard, /quiet
— тонкая настройка: /setfilters, /setbounce, /setcooldown, /setcheck, /settf
— отчёты и диагностика: /candles, /report, /autoreport, /status, /help
"""

import os, time, threading, requests, random
from datetime import datetime
from collections import defaultdict
from flask import Flask

# === ТВОИ ДАННЫЕ ===
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
# ===================

# Список символов KuCoin (формат с дефисом!)
DEFAULT_SYMBOLS = [
    "BTC-USDT","ETH-USDT","BNB-USDT","SOL-USDT","XRP-USDT","ADA-USDT","DOGE-USDT","TRX-USDT",
    "TON-USDT","LINK-USDT","LTC-USDT","DOT-USDT","ARB-USDT","OP-USDT","PEPE-USDT","SHIB-USDT"
]

# Базовые тайминги
CHECK_INTERVAL_S     = 20
SIGNAL_COOLDOWN_S    = 300
NO_SIGNAL_COOLDOWN   = 1800
ERROR_COOLDOWN       = 600
MIN_CANDLES          = 120
EMA_FAST, EMA_SLOW   = 9, 21
BASE_TF, FALLBACK_TF = "5m", "15m"

# Фильтры режимов
FILTERS = {
    "normal": {"eps": 0.0018, "atr_k": 0.18, "slope_min": -0.0001},
    "ultra":  {"eps": 0.0025, "atr_k": 0.10, "slope_min": -0.0005},
    "turbo":  {"eps": 0.0035, "atr_k": 0.07, "slope_min": -0.0015},
    "insane": {"eps": 0.0050, "atr_k": 0.05, "slope_min": -0.0030},
}

# KuCoin API
KUCOIN_BASE    = "https://api.kucoin.com"
KUCOIN_CANDLES = KUCOIN_BASE + "/api/v1/market/candles"
HEADERS        = {"User-Agent": "ema-kucoin-bot/2.4"}

# Flask + состояние
app = Flask(__name__)
state = {
    "symbols": DEFAULT_SYMBOLS[:],
    "base_tf": BASE_TF,
    "fallback_tf": FALLBACK_TF,
    "check_s": CHECK_INTERVAL_S,
    "min_candles": MIN_CANDLES,
    "ema_fast": EMA_FAST,
    "ema_slow": EMA_SLOW,

    # старт: quiet, но чуть мягче
    "eps_pct": 0.0012,
    "atr_k": 0.22,
    "slope_min": 0.0,
    "signal_cooldown_s": SIGNAL_COOLDOWN_S,
    "mode": "quiet",
    "bounce_k": 0.25,

    # авто-отчёт (каждый час)
    "report_enabled": True,
    "report_every_min": 60,

    # анти-лимиты
    "batch_size": 6,
    "per_req_sleep": 0.25,
    "rr_index": 0,
    "max_retries": 3,
    "backoff_base": 0.7,
}

cool_signal = defaultdict(float)
cool_no     = defaultdict(float)
cool_err    = defaultdict(float)

# ==== Утилиты ====
def now_ts(): return time.time()
def fmt_dt(ts=None): return datetime.fromtimestamp(ts or now_ts()).strftime("%Y-%m-%d %H:%M:%S")

def send_tg(txt: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": txt, "parse_mode": "HTML"},
            timeout=12
        )
    except Exception as e:
        print("TG send error:", e)

def ema(series, period):
    if len(series) < period: return []
    k = 2.0/(period+1.0)
    out = [None]*(period-1)
    prev = sum(series[:period])/period
    out.append(prev)
    for x in series[period:]:
        prev = x*k + prev*(1-k)
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
    return {
        "1m":"1min","5m":"5min","15m":"15min","30m":"30min",
        "1h":"1hour","4h":"4hour","1d":"1day"
    }.get(tf, "5min")

# ==== Обёртка с ретраями ====
def kucoin_get(url, params, timeout=10):
    tries=0
    while True:
        tries+=1
        try:
            r=requests.get(url,params=params,headers=HEADERS,timeout=timeout)
            if r.status_code==429:
                raise RuntimeError("429 Too many requests")
            return r
        except Exception as e:
            if tries>=state["max_retries"]: raise
            sleep_s=state["backoff_base"]*(2**(tries-1))+random.uniform(0.0,0.05)
            time.sleep(sleep_s)

def fetch_candles(symbol, tf, want=300):
    try:
        r=kucoin_get(KUCOIN_CANDLES,{"symbol":symbol,"type":tf_to_kucoin(tf)},timeout=10)
        j=r.json()
    except Exception as e:
        return None,f"bad resp {e}"
    if j.get("code")!="200000": return None,f"KuCoin error {j.get('msg')}"
    rows=[]
    for v in j.get("data",[]):
        try: rows.append((int(v[0]),float(v[1]),float(v[2]),float(v[3]),float(v[4])))
        except: pass
    if not rows: return None,"empty"
    rows.sort()
    t=[x[0] for x in rows][-want:]; o=[x[1] for x in rows][-want:]
    c=[x[2] for x in rows][-want:]; h=[x[3] for x in rows][-want:]; l=[x[4] for x in rows][-want:]
    time.sleep(state["per_req_sleep"])
    return {"t":t,"o":o,"h":h,"l":l,"c":c},None

# ==== Сигналы ====
def cross_or_near(e9,e21,price,eps_abs):
    if len(e9)<2 or len(e21)<2 or e9[-1] is None or e21[-1] is None: return None
    prev=(e9[-2]-e21[-2]) if (e9[-2] and e21[-2]) else None
    curr=e9[-1]-e21[-1]
    if prev is not None and prev<=0<curr: return "LONG","кросс ↑"
    if prev is not None and prev>=0>curr: return "SHORT","кросс ↓"
    if abs(curr)<=eps_abs:
        slope=e9[-1]-(e9[-2] if e9[-2] else e9[-1])
        return ("LONG" if slope>=0 else "SHORT"),"почти кросс"
    return None

def bounce_signal(e9,e21,price,atr_val):
    if e9[-1] is None or e21[-1] is None or atr_val is None: return None
    diff=abs(price-e21[-1])
    if diff<=state["bounce_k"]*atr_val:
        return ("LONG","отскок ↑") if e9[-1]>=e21[-1] else ("SHORT","отскок ↓")
    return None

def decide_signal(e9,e21,atr_arr,price,eps_pct,atr_k,slope_min):
    if not e9 or not e21 or e9[-1] is None or e21[-1] is None: return None,"нет EMA"
    eps_abs=price*eps_pct
    v=cross_or_near(e9,e21,price,eps_abs)
    if v:
        side,note=v; slope=e9[-1]-(e9[-2] if len(e9)>=2 and e9[-2] else e9[-1])
        if slope<slope_min: return None,"slope"
        if atr_arr and atr_arr[-1]:
            a=atr_arr[-1]; diff=abs(e9[-1]-e21[-1])
            if diff<a*atr_k and abs(diff)>eps_abs: return None,"atr"
        return side,note
    a=atr_arr[-1] if atr_arr and atr_arr[-1] else None
    v=bounce_signal(e9,e21,price,a)
    if v: return v
    return None,"нет"

def maybe_no_signal(sym):
    if now_ts()-cool_no[sym]>=NO_SIGNAL_COOLDOWN:
        cool_no[sym]=now_ts(); send_tg(f"ℹ️ По {sym} пока нет сигнала ({fmt_dt()})")

def make_text(sym,side,price,tf,note):
    return (f"📣 <b>{sym}</b> | TF <b>{tf}</b>\n"
            f"{'🟢 LONG' if side=='LONG' else '🔴 SHORT'} @ <b>{price:.4f}</b>\n"
            f"{note}\n{fmt_dt()}")

def check_symbol(sym):
    if now_ts()<cool_signal[sym]: return
    for tf in (state["base_tf"],state["fallback_tf"]):
        candles,err=fetch_candles(sym,tf,240)
        if not candles:
            if now_ts()-cool_err[sym]>=ERROR_COOLDOWN:
                cool_err[sym]=now_ts(); send_tg(f"❌ {sym}: {err}")
            return
        c=candles["c"];h=candles["h"];l=candles["l"]
        if len(c)<state["min_candles"]: maybe_no_signal(sym); return
        e9=ema(c,state["ema_fast"]); e21=ema(c,state["ema_slow"]); atr_a=atr(h,l,c)
        side,note=decide_signal(e9,e21,atr_a,c[-1],state["eps_pct"],state["atr_k"],state["slope_min"])
        if side:
            cool_signal[sym]=now_ts()+state["signal_cooldown_s"]
            send_tg(make_text(sym,side,c[-1],tf,note)); return
        else:
            maybe_no_signal(sym); return

# ==== Отчёты ====
def fmt_pct(x): 
    try: return f"{x*100:.3f}%"
    except: return "—"

def build_candles_report(sym,tf):
    cndl,err=fetch_candles(sym,tf,120)
    if not cndl: return f"❌ {sym}: {err}"
    c=cndl["c"];h=cndl["h"];l=cndl["l"]
    if len(c)<state["min_candles"]: return f"⚠️ {sym}: мало данных ({len(c)}<{state['min_candles']})"
    e9=ema(c,9);e21=ema(c,21);atr_a=atr(h,l,c)
    diff=(e9[-1]-e21[-1]) if (e9 and e21 and e9[-1] and e21[-1]) else None
    last=c[-1]
    lines=[f"🕯 <b>{sym}</b> | TF <b>{tf}</b>",f"Close: <b>{last:.4f}</b>"]
    return "\n".join(lines)

def build_all_report(tf):
    msgs=[];block=[]
    for sym in state["symbols"]:
        block.append(build_candles_report(sym,tf))
        if len("\n\n".join(block))>3500: msgs.append("\n\n".join(block)); block=[]
    if block: msgs.append("\n\n".join(block))
    return msgs

# ==== Батчи ====
def next_symbols_batch():
    syms=state["symbols"]
    if not syms: return []
    n=max(1,min(len(syms),int(state.get("batch_size",6))))
    i=int(state.get("rr_index",0))%len(syms)
    batch=(syms+syms)[i:i+n]; state["rr_index"]=(i+n)%len(syms)
    return batch

# ==== Пресеты ====
def apply_mode(m):
    m=(m or "normal").lower()
    if m in FILTERS:
        f=FILTERS[m]; state.update({"eps_pct":f["eps"],"atr_k":f["atr_k"],"slope_min":f["slope_min"],"mode":m})

def apply_preset_soft():
    state.update({"eps_pct":0.0030,"atr_k":0.08,"slope_min":-0.0020,"mode":"soft","bounce_k":0.40})

def apply_preset_hard():
    state.update({"eps_pct":0.0015,"atr_k":0.20,"slope_min":-0.0002,"mode":"hard","bounce_k":0.25})

def apply_preset_quiet():
    state.update({"eps_pct":0.0012,"atr_k":0.22,"slope_min":0.0,"bounce_k":0.25,
                  "signal_cooldown_s":max(300,state.get("signal_cooldown_s",300)),"mode":"quiet"})
    state["base_tf"]="5m"; state["check_s"]=max(15,state.get("check_s",20))

# ==== Telegram-команды (сокращённо, без изменений) ====
# ... (оставляем из твоего кода, команды /mode /soft /hard /quiet /status /setcooldown /setcheck /settf /setsymbols /setfilters /setbounce /setmincandles /candles /report /autoreport /setbatch /setthrottle /help)

# ==== Потоки ====
def tg_loop():
    url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"; offset=None
    while True:
        try:
            j=requests.get(url,params={"timeout":20,"offset":offset},timeout=25).json()
            if j.get("ok"):
                for u in j.get("result", []):
                    offset=u["update_id"]+1
                    msg=u.get("message",{})
                    if str(msg.get("chat",{}).get("id"))!=TELEGRAM_CHAT_ID: continue
                    if "text" in msg: handle_cmd(msg["text"])
        except Exception as e: print("tg loop error:", e)
        time.sleep(1)

def signals_worker():
    send_tg("🤖 KuCoin EMA бот (quiet, мягче) запущен. /help")
    while True:
        try:
            for s in next_symbols_batch():
                try: check_symbol(s)
                except Exception as e: print("check_symbol error",s,e)
            time.sleep(max(1,int(state["check_s"])))
        except Exception as e: print("signals_worker loop error:",e); time.sleep(2)

def report_worker():
    last=0
    while True:
        try:
            if state["report_enabled"] and now_ts()-last>=state["report_every_min"]*60:
                last=now_ts(); tf=state["base_tf"]
                for m in build_all_report(tf): send_tg("🧾 Авто-отчёт EMA/ATR\n"+m)
        except Exception as e: print("report worker error:",e)
        time.sleep(5)

@app.route("/")
def root(): return "ok"

if __name__=="__main__":
    apply_preset_quiet()
    threading.Thread(target=signals_worker,daemon=True).start()
    threading.Thread(target=tg_loop,daemon=True).start()
    threading.Thread(target=report_worker,daemon=True).start()
    port=int(os.environ.get("PORT","10000")); app.run(host="0.0.0.0",port=port)
