# -*- coding: utf-8 -*-
"""
EMA(9/21)+ATR сигнальный бот • KuCoin SPOT (мягкие фильтры)
Команды: /status, /mode, /setcooldown, /settf, /setsymbols, /candles, /report, /autoreport, /help
Фичи: авто-отчёт EMA/ATR раз в N минут (по умолчанию 60)
"""

import os, time, threading, requests, math
from datetime import datetime
from collections import defaultdict
from flask import Flask

# ==== ТВОИ ДАННЫЕ ====
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
# =====================

SYMBOLS = ["BTC-USDT","ETH-USDT","SOL-USDT","XRP-USDT","TRX-USDT"]

BASE_TF, FALLBACK_TF = "5m", "15m"
CHECK_INTERVAL_S     = 60
MIN_CANDLES          = 120
EMA_FAST, EMA_SLOW   = 9, 21
SIGNAL_COOLDOWN_S    = 300
NO_SIGNAL_COOLDOWN   = 1800
ERROR_COOLDOWN       = 900

# «мягкие» фильтры по умолчанию
DEFAULT_EPS   = 0.0012
DEFAULT_ATR_K = 0.15
DEFAULT_SLOPE = -0.0005

KUCOIN_BASE    = "https://api.kucoin.com"
KUCOIN_CANDLES = KUCOIN_BASE + "/api/v1/market/candles"
HEADERS        = {"User-Agent": "ema-kucoin-bot/1.1"}

app = Flask(__name__)
state = {
    "symbols": SYMBOLS[:],
    "base_tf": BASE_TF,
    "fallback_tf": FALLBACK_TF,
    "check_s": CHECK_INTERVAL_S,
    "min_candles": MIN_CANDLES,
    "ema_fast": EMA_FAST,
    "ema_slow": EMA_SLOW,
    "eps_pct": DEFAULT_EPS,
    "atr_k": DEFAULT_ATR_K,
    "slope_min": DEFAULT_SLOPE,
    "signal_cooldown_s": SIGNAL_COOLDOWN_S,
    "mode": "normal",
    # авто-отчёт
    "report_enabled": True,
    "report_every_min": 60,   # раз в 60 минут
}

cool_signal, cool_no, cool_err = defaultdict(float), defaultdict(float), defaultdict(float)

# ==== Утилиты ====
def now_ts(): return time.time()
def fmt_dt(ts=None): return datetime.fromtimestamp(ts or now_ts()).strftime("%Y-%m-%d %H:%M:%S")

def send_tg(txt):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID,"text":txt,"parse_mode":"HTML"},
                      timeout=12)
    except Exception as e:
        print("Telegram send error:", e)

def ema(series, period):
    if len(series)<period: return []
    k=2/(period+1); out=[None]*(period-1); prev=sum(series[:period])/period; out.append(prev)
    for x in series[period:]:
        prev=x*k+prev*(1-k); out.append(prev)
    return out

def atr(h,l,c,period=14):
    tr=[None]
    for i in range(1,len(c)):
        tr.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    vals=[x for x in tr if x]
    if len(vals)<period: return [None]*len(c)
    k=2/(period+1); prev=sum(vals[:period])/period
    out=[None]*(len(c)-len(vals))+[prev]
    for v in vals[period:]:
        prev=v*k+prev*(1-k); out.append(prev)
    return out

def tf_to_kucoin(tf):
    return {"1m":"1min","5m":"5min","15m":"15min","30m":"30min","1h":"1hour","4h":"4hour","1d":"1day"}.get(tf,"5min")

def fetch_candles(symbol, tf, want=300):
    try:
        r = requests.get(KUCOIN_CANDLES, params={"symbol":symbol,"type":tf_to_kucoin(tf)}, headers=HEADERS, timeout=12)
        j = r.json()
    except Exception as e: 
        return None,f"bad resp {e}"
    if j.get("code")!="200000": 
        return None,f"KuCoin error {j.get('msg')}"
    rows=[]
    for v in j.get("data",[]):
        try: rows.append((int(v[0]),float(v[1]),float(v[2]),float(v[3]),float(v[4])))
        except: pass
    if not rows: return None,"empty"
    rows.sort()
    t=[x[0] for x in rows][-want:]; o=[x[1] for x in rows][-want:]; c=[x[2] for x in rows][-want:]
    h=[x[3] for x in rows][-want:]; l=[x[4] for x in rows][-want:]
    return {"t":t,"o":o,"h":h,"l":l,"c":c},None

# ==== Сигналы ====
def cross_signal(e9,e21,eps,slope_min,atr_a,atr_k):
    if not e9 or not e21 or e9[-1] is None or e21[-1] is None: return None,"нет EMA"
    df_prev=(e9[-2]-e21[-2]) if (len(e9)>=2 and len(e21)>=2 and e9[-2] is not None and e21[-2] is not None) else None
    df=e9[-1]-e21[-1]; price=e9[-1]
    slope=e9[-1]- (e9[-2] if len(e9)>=2 and e9[-2] is not None else e9[-1])
    a=atr_a[-1] if atr_a and atr_a[-1] is not None else None
    if slope<slope_min: return None,"slope"
    if a and abs(df)<a*atr_k: return None,"atr"
    if df_prev is not None and (df_prev<=0<df): return "LONG","cross ↑"
    if df_prev is not None and (df_prev>=0>df): return "SHORT","cross ↓"
    if abs(df)<=price*eps: return ("LONG" if slope>0 else "SHORT"),"near"
    return None,"нет"

def maybe_no_signal(sym):
    if now_ts()-cool_no[sym]>=NO_SIGNAL_COOLDOWN:
        cool_no[sym]=now_ts(); send_tg(f"ℹ️ По {sym} сейчас нет сигнала ({fmt_dt()})")

def make_text(sym,side,price,tf,note):
    return f"📣 {sym} {tf}\n{'🟢 LONG' if side=='LONG' else '🔴 SHORT'} @ {price:.4f}\nПричина: {note}\n{fmt_dt()}"

def check_symbol(sym):
    if now_ts()<cool_signal[sym]: return
    for tf in (state["base_tf"],state["fallback_tf"]):
        candles,err=fetch_candles(sym,tf,300)
        if not candles:
            if now_ts()-cool_err[sym]>ERROR_COOLDOWN:
                cool_err[sym]=now_ts(); send_tg(f"❌ {sym}: {err}")
            return
        c=candles["c"]; h=candles["h"]; l=candles["l"]
        if len(c)<state["min_candles"]: maybe_no_signal(sym); return
        e9=ema(c,state["ema_fast"]); e21=ema(c,state["ema_slow"]); atr_a=atr(h,l,c)
        side,note=cross_signal(e9,e21,state["eps_pct"],state["slope_min"],atr_a,state["atr_k"])
        if side:
            cool_signal[sym]=now_ts()+state["signal_cooldown_s"]
            send_tg(make_text(sym,side,c[-1],tf,note)); return
        else: maybe_no_signal(sym); return

# ===== Диагностика (ручные отчёты) =====
def fmt_pct(x): 
    try: return f"{x*100:.3f}%"
    except: return "—"

def build_candles_report(sym,tf):
    cndl,err=fetch_candles(sym,tf,120)
    if not cndl: return f"❌ {sym}: {err}"
    c=cndl["c"]; h=cndl["h"]; l=cndl["l"]
    if len(c)<state["min_candles"]: return f"⚠️ {sym}: мало данных ({len(c)}<{state['min_candles']})"
    e9=ema(c,9); e21=ema(c,21); atr_a=atr(h,l,c)
    diff = (e9[-1]-e21[-1]) if (e9 and e21 and e9[-1] is not None and e21[-1] is not None) else None
    last = c[-1]
    lines = [
        f"🕯 <b>{sym}</b> | TF <b>{tf}</b>",
        f"Close: <b>{last:.4f}</b>",
        f"EMA9:  <b>{e9[-1]:.4f}</b>"  if e9 and e9[-1] is not None else "EMA9:  —",
        f"EMA21: <b>{e21[-1]:.4f}</b>" if e21 and e21[-1] is not None else "EMA21: —",
        f"ATR14: <b>{atr_a[-1]:.5f}</b>" if atr_a and atr_a[-1] is not None else "ATR14: —",
    ]
    if diff is not None:
        lines.append(f"Δ(9-21): <b>{diff:.5f}</b> ({fmt_pct(diff/last if last else 0)})")
    return "\n".join(lines)

def build_all_report(tf):
    """Общий отчёт по всем символам (чтобы уписаться в лимит Telegram, разбиваем на блоки)."""
    msgs=[]; block=[]
    for sym in state["symbols"]:
        txt = build_candles_report(sym, tf)
        block.append(txt)
        if len("\n\n".join(block)) > 3500:  # безопасный запас к лимиту 4096
            msgs.append("\n\n".join(block)); block=[]
    if block: msgs.append("\n\n".join(block))
    return msgs

# ===== Telegram =====
def apply_mode(m):
    if m=="ultra":
        state.update({"eps_pct":0.0020,"atr_k":0.10,"slope_min":-0.0010,"mode":"ultra"})
    else:
        state.update({"eps_pct":DEFAULT_EPS,"atr_k":DEFAULT_ATR_K,"slope_min":DEFAULT_SLOPE,"mode":"normal"})

def handle_cmd(text):
    if text.startswith("/mode"):
        parts=text.split(); apply_mode(parts[1] if len(parts)>1 else "normal")
        send_tg(f"mode={state['mode']} eps={state['eps_pct']} atr_k={state['atr_k']} slope_min={state['slope_min']}")
    elif text.startswith("/status"):
        send_tg(f"🩺 {state['symbols']}\ntf={state['base_tf']} check={state['check_s']}s\nmode={state['mode']}\nreport={'on' if state['report_enabled'] else 'off'} every={state['report_every_min']}m\n{fmt_dt()}")
    elif text.startswith("/setcooldown"):
        try:v=int(text.split()[1]); state["signal_cooldown_s"]=max(60,min(3600,v)); send_tg(f"cooldown={state['signal_cooldown_s']}")
        except: send_tg("формат /setcooldown 300")
    elif text.startswith("/settf"):
        try:v=text.split()[1]; state["base_tf"]=v; send_tg(f"TF={v}")
        except: send_tg("формат /settf 5m|15m|1h|4h|1d")
    elif text.startswith("/setsymbols"):
        try: syms=text.split()[1:]; state["symbols"]=[s.upper() for s in syms]; send_tg(f"symbols={state['symbols']}")
        except: send_tg("формат /setsymbols BTC-USDT ETH-USDT ...")
    elif text.startswith("/candles"):
        parts=text.split()
        sym=parts[1].upper() if len(parts)>1 else "BTC-USDT"
        tf=parts[2] if len(parts)>2 else state["base_tf"]
        send_tg(build_candles_report(sym,tf))
    elif text.startswith("/report"):
        parts=text.split()
        tf=parts[1] if len(parts)>1 else state["base_tf"]
        for m in build_all_report(tf): send_tg(m)
    elif text.startswith("/autoreport"):
        # /autoreport on|off [minutes]
        parts=text.split()
        if len(parts)<2: 
            send_tg(f"autoreport={'on' if state['report_enabled'] else 'off'} every={state['report_every_min']}m"); return
        mode=parts[1].lower()
        if mode=="on":
            mins=int(parts[2]) if len(parts)>2 else state["report_every_min"]
            state["report_enabled"]=True; state["report_every_min"]=max(5,min(1440,mins))
            send_tg(f"✅ autoreport ON, every {state['report_every_min']}m")
        elif mode=="off":
            state["report_enabled"]=False; send_tg("⛔ autoreport OFF")
        else:
            send_tg("формат: /autoreport on|off [минут]")
    elif text.startswith("/help"):
        send_tg("Команды:\n/status\n/mode ultra|normal\n/setcooldown N\n/settf TF\n/setsymbols A B C\n/candles SYMBOL [TF]\n/report [TF]\n/autoreport on|off [минут]")

def tg_loop():
    url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"; offset=None
    while True:
        try:
            j=requests.get(url,params={"timeout":20,"offset":offset},timeout=25).json()
            if j.get("ok"):
                for u in j["result"]:
                    offset=u["update_id"]+1; msg=u.get("message",{})
                    if str(msg.get("chat",{}).get("id"))!=TELEGRAM_CHAT_ID: continue
                    if "text" in msg: handle_cmd(msg["text"])
        except Exception as e: 
            print("tg loop error:", e)
        time.sleep(1)

# ===== Воркеры =====
def signals_worker():
    send_tg("🤖 KuCoin EMA бот запущен. /help")
    while True:
        for s in state["symbols"]:
            try: check_symbol(s)
            except Exception as e: print("check_symbol error", s, e)
        time.sleep(state["check_s"])

def report_worker():
    last_run = 0
    while True:
        try:
            if state["report_enabled"] and (now_ts()-last_run) >= state["report_every_min"]*60:
                last_run = now_ts()
                tf = state["base_tf"]
                for m in build_all_report(tf):
                    send_tg("🧾 Авто-отчёт EMA/ATR\n" + m)
        except Exception as e:
            print("report worker error:", e)
        time.sleep(5)

@app.route("/") 
def root(): return "ok"

if __name__=="__main__":
    threading.Thread(target=signals_worker,daemon=True).start()
    threading.Thread(target=tg_loop,daemon=True).start()
    threading.Thread(target=report_worker,daemon=True).start()
    port=int(os.environ.get("PORT","10000"))
    app.run(host="0.0.0.0",port=port)
