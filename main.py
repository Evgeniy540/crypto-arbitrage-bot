# -*- coding: utf-8 -*-
"""
EMA(9/21)+ATR сигнальный бот • KuCoin SPOT
Команды: /status, /mode, /soft, /hard, /quiet, /setcooldown, /settf, /setsymbols,
         /setfilters, /setbounce, /setmincandles, /setcheck, /candles, /report,
         /autoreport, /help
"""

import os, time, threading, requests
from datetime import datetime
from collections import defaultdict
from flask import Flask

# === ТВОИ ДАННЫЕ ===
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
# ===================

# Расширенный список символов KuCoin (обрати внимание — формат с дефисом!)
DEFAULT_SYMBOLS = [
    "BTC-USDT","ETH-USDT","BNB-USDT","SOL-USDT","XRP-USDT","ADA-USDT","DOGE-USDT","TRX-USDT",
    "TON-USDT","LINK-USDT","LTC-USDT","DOT-USDT","ARB-USDT","OP-USDT","PEPE-USDT","SHIB-USDT"
]

# Строгие дефолт-тайминги (меньше сообщений из коробки)
CHECK_INTERVAL_S     = 20     # проверяем каждые 20с
SIGNAL_COOLDOWN_S    = 300    # минимум 5 минут между сигналами по монете
NO_SIGNAL_COOLDOWN   = 1800   # инфо «нет сигнала» не чаще, чем раз в 30 мин
ERROR_COOLDOWN       = 600
MIN_CANDLES          = 120
EMA_FAST, EMA_SLOW   = 9, 21
BASE_TF, FALLBACK_TF = "5m", "15m"  # дефолт строже

# Преднастроенные режимы
FILTERS = {
    "normal": {"eps": 0.0015, "atr_k": 0.20, "slope_min": -0.0002},
    "ultra":  {"eps": 0.0020, "atr_k": 0.10, "slope_min": -0.0010},
    "turbo":  {"eps": 0.0030, "atr_k": 0.06, "slope_min": -0.0020},
    "insane": {"eps": 0.0050, "atr_k": 0.04, "slope_min": -0.0040},
}

# KuCoin API
KUCOIN_BASE    = "https://api.kucoin.com"
KUCOIN_CANDLES = KUCOIN_BASE + "/api/v1/market/candles"
HEADERS        = {"User-Agent": "ema-kucoin-bot/2.2"}

# Flask и состояние
app = Flask(__name__)
state = {
    "symbols": DEFAULT_SYMBOLS[:],
    "base_tf": BASE_TF,
    "fallback_tf": FALLBACK_TF,
    "check_s": CHECK_INTERVAL_S,
    "min_candles": MIN_CANDLES,
    "ema_fast": EMA_FAST,
    "ema_slow": EMA_SLOW,
    # стартуем в строгом профиле «quiet» (см. apply_preset_quiet)
    "eps_pct": 0.0010,
    "atr_k": 0.25,
    "slope_min": 0.0,
    "signal_cooldown_s": SIGNAL_COOLDOWN_S,
    "mode": "quiet",
    "bounce_k": 0.20,          # доля ATR для «отскока»
    # авто-отчёт
    "report_enabled": True,
    "report_every_min": 60,
}

cool_signal = defaultdict(float)
cool_no     = defaultdict(float)
cool_err    = defaultdict(float)

# === Утилиты ===
def now_ts(): return time.time()
def fmt_dt(ts=None): return datetime.fromtimestamp(ts or now_ts()).strftime("%Y-%m-%d %H:%M:%S")

def send_tg(txt: str):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID,"text":txt,"parse_mode":"HTML"},
                      timeout=12)
    except Exception as e:
        print("TG send error:", e)

def ema(series, period):
    if len(series) < period: return []
    k=2.0/(period+1.0); out=[None]*(period-1)
    prev=sum(series[:period])/period; out.append(prev)
    for x in series[period:]:
        prev=x*k+prev*(1-k); out.append(prev)
    return out

def atr(h,l,c,period=14):
    tr=[None]
    for i in range(1,len(c)):
        tr.append(max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])))
    vals=[x for x in tr if x is not None]
    if len(vals)<period: return [None]*len(c)
    k=2.0/(period+1.0); prev=sum(vals[:period])/period
    out=[None]*(len(c)-len(vals))+[prev]
    for v in vals[period:]:
        prev=v*k+prev*(1-k); out.append(prev)
    return out

def tf_to_kucoin(tf):
    return {"1m":"1min","5m":"5min","15m":"15min","30m":"30min","1h":"1hour","4h":"4hour","1d":"1day"}.get(tf,"5min")

def fetch_candles(symbol, tf, want=300):
    try:
        r=requests.get(KUCOIN_CANDLES,params={"symbol":symbol,"type":tf_to_kucoin(tf)},
                       headers=HEADERS,timeout=10)
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
    t=[x[0] for x in rows][-want:]; o=[x[1] for x in rows][-want:]; c=[x[2] for x in rows][-want:]
    h=[x[3] for x in rows][-want:]; l=[x[4] for x in rows][-want:]
    return {"t":t,"o":o,"h":h,"l":l,"c":c},None

# === Логика сигналов ===
def cross_or_near(e9,e21,price,eps_abs):
    if len(e9)<2 or len(e21)<2 or e9[-1] is None or e21[-1] is None: return None
    prev=(e9[-2]-e21[-2]) if (e9[-2] is not None and e21[-2] is not None) else None
    curr=e9[-1]-e21[-1]
    if prev is not None and prev<=0<curr: return "LONG","кросс ↑"
    if prev is not None and prev>=0>curr: return "SHORT","кросс ↓"
    if abs(curr)<=eps_abs:
        slope=e9[-1]-(e9[-2] if e9[-2] is not None else e9[-1])
        return ("LONG" if slope>=0 else "SHORT"),"почти кросс"
    return None

def bounce_signal(e9,e21,price,atr_val):
    if e9[-1] is None or e21[-1] is None or atr_val is None: return None
    diff=abs(price-e21[-1])
    if diff<=state["bounce_k"]*atr_val:
        return ("LONG","отскок от EMA21 ↑") if e9[-1]>=e21[-1] else ("SHORT","отскок от EMA21 ↓")
    return None

def decide_signal(e9,e21,atr_arr,price,eps_pct,atr_k,slope_min):
    if not e9 or not e21 or e9[-1] is None or e21[-1] is None: return None,"нет EMA"
    eps_abs=price*eps_pct
    v=cross_or_near(e9,e21,price,eps_abs)
    if v:
        side,note=v
        slope=e9[-1]-(e9[-2] if len(e9)>=2 and e9[-2] is not None else e9[-1])
        if slope<slope_min: return None,"slope"
        # строгий ATR-фильтр — при quiet/normal он высокий (atr_k >= 0.20)
        if atr_arr and atr_arr[-1] is not None:
            a=atr_arr[-1]; diff=abs(e9[-1]-e21[-1])
            if diff < a*atr_k and abs(diff) > eps_abs:
                return None,"atr"
        return side,note
    a=atr_arr[-1] if atr_arr and atr_arr[-1] is not None else None
    v=bounce_signal(e9,e21,price,a)
    if v: 
        # немного ужмём отскоки в тихом режиме
        if atr_arr and atr_arr[-1] and atr_k>=0.20:
            return None,"atr-bounce"
        return v
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
        c=candles["c"]; h=candles["h"]; l=candles["l"]
        if len(c)<state["min_candles"]: maybe_no_signal(sym); return
        e9=ema(c,state["ema_fast"]); e21=ema(c,state["ema_slow"]); atr_a=atr(h,l,c)
        side,note=decide_signal(e9,e21,atr_a,c[-1],state["eps_pct"],state["atr_k"],state["slope_min"])
        if side:
            cool_signal[sym]=now_ts()+state["signal_cooldown_s"]
            send_tg(make_text(sym,side,c[-1],tf,note)); return
        else: maybe_no_signal(sym); return

# === Режимы/пресеты ===
def apply_mode(m):
    m=(m or "normal").lower()
    if m in FILTERS:
        f=FILTERS[m]
        state.update({"eps_pct":f["eps"],"atr_k":f["atr_k"],"slope_min":f["slope_min"],"mode":m})
    else:
        # по умолчанию останемся в quiet
        pass

def apply_preset_soft():
    state.update({"eps_pct":0.0030,"atr_k":0.08,"slope_min":-0.0020,"mode":"soft","bounce_k":0.40})
def apply_preset_hard():
    state.update({"eps_pct":0.0015,"atr_k":0.20,"slope_min":-0.0002,"mode":"hard","bounce_k":0.25})
def apply_preset_quiet():
    # очень строгий профиль (минимум сигналов)
    state.update({
        "eps_pct": 0.0010,
        "atr_k":   0.25,
        "slope_min": 0.0,
        "bounce_k": 0.20,
        "signal_cooldown_s": max(300, state.get("signal_cooldown_s", 300)),
        "mode": "quiet",
    })
    state["base_tf"] = "5m"
    state["check_s"] = 20

# === Telegram команды ===
def handle_cmd(text):
    if text.startswith("/mode"):
        parts=text.split(); apply_mode(parts[1] if len(parts)>1 else "normal")
        send_tg(f"mode={state['mode']} eps={state['eps_pct']} atr_k={state['atr_k']} slope_min={state['slope_min']}")
    elif text.startswith("/soft"):  apply_preset_soft();  send_tg("🎛 SOFT preset (мягко)")
    elif text.startswith("/hard"):  apply_preset_hard();  send_tg("🎛 HARD preset (строже)")
    elif text.startswith("/quiet"): apply_preset_quiet(); send_tg("🤫 QUIET preset (минимум сигналов)")
    elif text.startswith("/status"):
        send_tg(f"🩺 symbols={state['symbols']}\n"
                f"tf={state['base_tf']} (fb {state['fallback_tf']}) check={state['check_s']}s\n"
                f"mode={state['mode']} eps={state['eps_pct']} atr_k={state['atr_k']} slope_min={state['slope_min']} bounce_k={state['bounce_k']}\n"
                f"cooldown={state['signal_cooldown_s']}s report={'on' if state['report_enabled'] else 'off'} every={state['report_every_min']}m\n{fmt_dt()}")
    elif text.startswith("/setcooldown"):
        try: v=int(text.split()[1]); v=max(60,min(3600,v)); state["signal_cooldown_s"]=v; send_tg(f"cooldown={v}")
        except: send_tg("формат /setcooldown 60..3600")
    elif text.startswith("/setcheck"):
        try: v=int(text.split()[1]); state["check_s"]=max(5,min(60,v)); send_tg(f"check interval = {state['check_s']}s")
        except: send_tg("формат: /setcheck 20")
    elif text.startswith("/settf"):
        try: v=text.split()[1]; state["base_tf"]=v; send_tg(f"TF={v}")
        except: send_tg("формат /settf 1m|5m|15m|1h|4h|1d")
    elif text.startswith("/setsymbols"):
        try: syms=text.split()[1:]; state["symbols"]=[s.upper() for s in syms]; send_tg(f"symbols={state['symbols']}")
        except: send_tg("формат /setsymbols BTC-USDT ETH-USDT ...")
    elif text.startswith("/setfilters"):
        parts=text.split()
        if len(parts)<4: send_tg(f"текущие: eps={state['eps_pct']} atr_k={state['atr_k']} slope_min={state['slope_min']}")
        else:
            try:
                eps=float(parts[1]); ak=float(parts[2]); sm=float(parts[3])
                state.update({"eps_pct":eps,"atr_k":ak,"slope_min":sm})
                send_tg(f"ok: eps={eps} atr_k={ak} slope_min={sm}")
            except: send_tg("формат: /setfilters 0.0012 0.20 -0.0002")
    elif text.startswith("/setbounce"):
        try: v=float(text.split()[1]); v=max(0.1,min(1.0,v)); state["bounce_k"]=v; send_tg(f"bounce_k={v}")
        except: send_tg("формат: /setbounce 0.25  (0.1..1.0)")
    elif text.startswith("/setmincandles"):
        try: v=int(text.split()[1]); v=max(60,min(300,v)); state["min_candles"]=v; send_tg(f"min_candles={v}")
        except: send_tg("формат /setmincandles 120")
    elif text.startswith("/candles"):
        parts=text.split(); sym=parts[1].upper() if len(parts)>1 else "BTC-USDT"; tf=parts[2] if len(parts)>2 else state["base_tf"]
        send_tg(build_candles_report(sym,tf))
    elif text.startswith("/report"):
        parts=text.split(); tf=parts[1] if len(parts)>1 else state["base_tf"]
        for m in build_all_report(tf): send_tg("🧾 Отчёт EMA/ATR\n"+m)
    elif text.startswith("/autoreport"):
        parts=text.split()
        if len(parts)<2: send_tg(f"autoreport={'on' if state['report_enabled'] else 'off'} every={state['report_every_min']}m"); return
        mode=parts[1].lower()
        if mode=="on":
            mins=int(parts[2]) if len(parts)>2 else state["report_every_min"]
            state["report_enabled"]=True; state["report_every_min"]=max(10,min(1440,mins))
            send_tg(f"✅ autoreport ON, every {state['report_every_min']}m")
        elif mode=="off":
            state["report_enabled"]=False; send_tg("⛔ autoreport OFF")
        else: send_tg("формат: /autoreport on|off [минут]")
    elif text.startswith("/help"):
        send_tg("Команды:\n"
                "/status\n/mode insane|turbo|ultra|normal\n/soft | /hard | /quiet\n"
                "/setcooldown N\n/setcheck N\n/settf TF\n/setsymbols A B C\n"
                "/setfilters eps atr_k slope_min\n/setbounce K\n/setmincandles N\n"
                "/candles SYMBOL [TF]\n/report [TF]\n/autoreport on|off [минут]")

# === Диагностика/отчёты ===
def fmt_pct(x): 
    try: return f"{x*100:.3f}%"
    except: return "—"

def build_candles_report(sym, tf):
    cndl,err=fetch_candles(sym,tf,120)
    if not cndl: return f"❌ {sym}: {err}"
    c=cndl["c"]; h=cndl["h"]; l=cndl["l"]
    if len(c)<state["min_candles"]: return f"⚠️ {sym}: мало данных ({len(c)}<{state['min_candles']})"
    e9=ema(c,9); e21=ema(c,21); atr_a=atr(h,l,c)
    diff=(e9[-1]-e21[-1]) if (e9 and e21 and e9[-1] is not None and e21[-1] is not None) else None
    last=c[-1]
    lines=[f"🕯 <b>{sym}</b> | TF <b>{tf}</b>",
           f"Close: <b>{last:.4f}</b>",
           f"EMA9:  <b>{e9[-1]:.4f}</b>" if e9 and e9[-1] is not None else "EMA9: —",
           f"EMA21: <b>{e21[-1]:.4f}</b>" if e21 and e21[-1] is not None else "EMA21: —",
           f"ATR14: <b>{atr_a[-1]:.5f}</b>" if atr_a and atr_a[-1] is not None else "ATR14: —"]
    if diff is not None: lines.append(f"Δ(9-21): <b>{diff:.5f}</b> ({fmt_pct(diff/last if last else 0)})")
    return "\n".join(lines)

def build_all_report(tf):
    msgs=[]; block=[]
    for sym in state["symbols"]:
        block.append(build_candles_report(sym, tf))
        if len("\n\n".join(block))>3500: msgs.append("\n\n".join(block)); block=[]
    if block: msgs.append("\n\n".join(block))
    return msgs

# === Потоки ===
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
    send_tg("🤖 KuCoin EMA бот (quiet) запущен. /help")
    while True:
        for s in state["symbols"]:
            try: check_symbol(s)
            except Exception as e: print("check_symbol error", s, e)
        time.sleep(state["check_s"])

def report_worker():
    last=0
    while True:
        try:
            if state["report_enabled"] and now_ts()-last >= state["report_every_min"]*60:
                last=now_ts()
                tf=state["base_tf"]
                for m in build_all_report(tf): send_tg("🧾 Авто-отчёт EMA/ATR\n"+m)
        except Exception as e: print("report worker error:", e)
        time.sleep(5)

@app.route("/")
def root(): return "ok"

if __name__=="__main__":
    # применим строгий пресет при старте
    apply_preset_quiet()
    threading.Thread(target=signals_worker,daemon=True).start()
    threading.Thread(target=tg_loop,daemon=True).start()
    threading.Thread(target=report_worker,daemon=True).start()
    port=int(os.environ.get("PORT","10000"))
    app.run(host="0.0.0.0",port=port)
