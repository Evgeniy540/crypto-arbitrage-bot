# -*- coding: utf-8 -*-
"""
EMA(9/21)+ATR сигнальный бот • KuCoin SPOT (Ultra-Quiet by default)
— анти-лимиты KuCoin: батчи, троттлинг, ретраи при 429
— пресеты: /mode, /quietpp, /quiet, /soft, /hard, /night
— тонкая настройка: /setfilters, /setbounce, /setcooldown, /setcheck, /settf, /setsymbols, /setnosig
— отчёты/диагностика: /candles, /report, /autoreport, /status, /help
"""

import os, time, threading, requests, random
from datetime import datetime
from collections import defaultdict
from flask import Flask

# === ТВОИ ДАННЫЕ ===
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
# ===================

# Символы KuCoin (формат с дефисом!)
DEFAULT_SYMBOLS = [
    "BTC-USDT","ETH-USDT","BNB-USDT","SOL-USDT","XRP-USDT","ADA-USDT","DOGE-USDT","TRX-USDT",
    "TON-USDT","LINK-USDT","LTC-USDT","DOT-USDT","ARB-USDT","OP-USDT","PEPE-USDT","SHIB-USDT"
]

# Базовые параметры
EMA_FAST, EMA_SLOW   = 9, 21
BASE_TF, FALLBACK_TF = "15m", "5m"   # ← по умолчанию работаем тише на 15m
MIN_CANDLES          = 120

# KuCoin API
KUCOIN_BASE    = "https://api.kucoin.com"
KUCOIN_CANDLES = KUCOIN_BASE + "/api/v1/market/candles"
HEADERS        = {"User-Agent": "ema-kucoin-bot/3.0"}

# Flask + состояние
app = Flask(__name__)
state = {
    # рынoк / расчёты
    "symbols": DEFAULT_SYMBOLS[:],
    "base_tf": BASE_TF,
    "fallback_tf": FALLBACK_TF,
    "min_candles": MIN_CANDLES,
    "ema_fast": EMA_FAST,
    "ema_slow": EMA_SLOW,

    # тайминги
    "check_s": 20,            # пауза между батчами
    "signal_cooldown_s": 1200,# 20 мин между сигналами по одной паре
    "no_sig_cooldown_s": 7200,# «нет сигнала» не чаще, чем раз в 2 часа
    "error_cooldown_s": 600,

    # чувствительность (Ultra-Quiet, ещё мягче)
    "eps_pct": 0.0021,        # требуем больший допуск для «почти-кросс»
    "atr_k":   0.35,          # и больший разлёт EMA относительно ATR
    "slope_min": 0.0003,      # минимальный наклон EMA9
    "bounce_k": 0.16,         # ужатие отскоков от EMA21
    "mode": "ultra-quiet",

    # отчёты
    "report_enabled": True,
    "report_every_min": 60,

    # анти-лимиты
    "batch_size": 6,
    "per_req_sleep": 0.30,
    "rr_index": 0,
    "max_retries": 3,
    "backoff_base": 0.7,
}

cool_signal = defaultdict(float)
cool_no     = defaultdict(float)
cool_err    = defaultdict(float)

# ===== Утилиты =====
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
    return {"1m":"1min","5m":"5min","15m":"15min","30m":"30min",
            "1h":"1hour","4h":"4hour","1d":"1day"}.get(tf,"15min")

# ===== HTTP с ретраями =====
def kucoin_get(url, params, timeout=10):
    tries=0
    while True:
        tries+=1
        try:
            r=requests.get(url,params=params,headers=HEADERS,timeout=timeout)
            if r.status_code==429: raise RuntimeError("429 Too many requests")
            return r
        except Exception:
            if tries>=state["max_retries"]: raise
            sleep_s=state["backoff_base"]*(2**(tries-1))+random.uniform(0.0,0.05)
            time.sleep(sleep_s)

def fetch_candles(symbol, tf, want=300):
    try:
        r=kucoin_get(KUCOIN_CANDLES,{"symbol":symbol,"type":tf_to_kucoin(tf)},timeout=10)
        j=r.json()
    except Exception as e: return None,f"bad resp {e}"
    if j.get("code")!="200000": return None,f"KuCoin error {j.get('msg')}"
    rows=[]
    for v in j.get("data",[]):
        try: rows.append((int(v[0]),float(v[1]),float(v[2]),float(v[3]),float(v[4])))
        except: pass
    if not rows: return None,"empty"
    rows.sort()
    t=[x[0] for x in rows][-want:]; o=[x[1] for x in rows][-want:]
    c=[x[2] for x in rows][-want:]; h=[x[3] for x in rows][-want:]
    l=[x[4] for x in rows][-want:]
    time.sleep(state["per_req_sleep"])
    return {"t":t,"o":o,"h":h,"l":l,"c":c},None

# ===== Сигналы =====
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
        slope=e9[-1]-(e9[-2] if e9[-2] is not None else e9[-1])
        if slope<slope_min: return None,"slope"
        if atr_arr and atr_arr[-1] is not None:
            a=atr_arr[-1]; diff=abs(e9[-1]-e21[-1])
            if diff < a*atr_k and abs(diff) > eps_abs:
                return None,"atr"
        return side,note

    a=atr_arr[-1] if atr_arr and atr_arr[-1] is not None else None
    v=bounce_signal(e9,e21,price,a)
    if v: return v
    return None,"нет"

def maybe_no_signal(sym):
    if now_ts()-cool_no[sym] >= state["no_sig_cooldown_s"]:
        cool_no[sym]=now_ts()
        send_tg(f"ℹ️ По {sym} пока нет сигнала ({fmt_dt()})")

def make_text(sym,side,price,tf,note):
    return (f"📣 <b>{sym}</b> | TF <b>{tf}</b>\n"
            f"{'🟢 LONG' if side=='LONG' else '🔴 SHORT'} @ <b>{price:.4f}</b>\n"
            f"{note}\n{fmt_dt()}")

def check_symbol(sym):
    if now_ts()<cool_signal[sym]: return
    for tf in (state["base_tf"],state["fallback_tf"]):
        candles,err=fetch_candles(sym,tf,240)
        if not candles:
            if now_ts()-cool_err[sym] >= state["error_cooldown_s"]:
                cool_err[sym]=now_ts(); send_tg(f"❌ {sym}: {err}")
            return
        c=candles["c"]; h=candles["h"]; l=candles["l"]
        if len(c)<state["min_candles"]:
            maybe_no_signal(sym); return

        e9=ema(c,state["ema_fast"]); e21=ema(c,state["ema_slow"]); atr_a=atr(h,l,c)
        side,note=decide_signal(e9,e21,atr_a,c[-1],state["eps_pct"],state["atr_k"],state["slope_min"])
        if side:
            cool_signal[sym]=now_ts()+state["signal_cooldown_s"]
            send_tg(make_text(sym,side,c[-1],tf,note)); return
        else:
            maybe_no_signal(sym); return

# ===== Отчёты =====
def fmt_pct(x):
    try: return f"{x*100:.3f}%"
    except: return "—"

def build_candles_report(sym, tf):
    cndl, err = fetch_candles(sym, tf, 120)
    if not cndl: return f"❌ {sym}: {err}"
    c=cndl["c"]; h=cndl["h"]; l=cndl["l"]
    if len(c)<state["min_candles"]: return f"⚠️ {sym}: мало данных ({len(c)}<{state['min_candles']})"
    e9=ema(c,9); e21=ema(c,21); atr_a=atr(h,l,c)
    last=c[-1]; d=(e9[-1]-e21[-1]) if (e9 and e21 and e9[-1] is not None and e21[-1] is not None) else None
    lines=[
        f"🕯 <b>{sym}</b> | TF <b>{tf}</b>",
        f"Close: <b>{last:.4f}</b>",
        f"EMA9:  <b>{e9[-1]:.4f}</b>"  if e9 and e9[-1] is not None else "EMA9: —",
        f"EMA21: <b>{e21[-1]:.4f}</b>" if e21 and e21[-1] is not None else "EMA21: —",
        f"ATR14: <b>{atr_a[-1]:.5f}</b>" if atr_a and atr_a[-1] is not None else "ATR14: —",
    ]
    if d is not None and last:
        lines.append(f"Δ(9-21): <b>{d:.5f}</b> ({fmt_pct(d/last)})")
    return "\n".join(lines)

def build_all_report(tf):
    msgs=[]; block=[]
    for sym in state["symbols"]:
        block.append(build_candles_report(sym, tf))
        if len("\n\n".join(block))>3500:
            msgs.append("\n\n".join(block)); block=[]
    if block: msgs.append("\n\n".join(block))
    return msgs

# ===== Пресеты/режимы и команды =====
def next_batch():
    syms=state["symbols"]
    if not syms: return []
    n=max(1,min(len(syms),int(state.get("batch_size",6))))
    i=int(state.get("rr_index",0))%len(syms)
    batch=(syms+syms)[i:i+n]
    state["rr_index"]=(i+n)%len(syms)
    return batch

def apply_preset_quietpp():
    # «чуть мягче» твоего предыдущего quiet
    state.update({"eps_pct":0.0019,"atr_k":0.32,"slope_min":0.0002,"bounce_k":0.18,
                  "signal_cooldown_s":max(900,state["signal_cooldown_s"]), "mode":"quiet++",
                  "base_tf":"5m"})

def apply_preset_ultra_quiet():
    # текущий дефолт
    state.update({"eps_pct":0.0021,"atr_k":0.35,"slope_min":0.0003,"bounce_k":0.16,
                  "signal_cooldown_s":1200,"mode":"ultra-quiet","base_tf":"15m"})

def apply_preset_night():
    # очень тихо
    state.update({"eps_pct":0.0028,"atr_k":0.45,"slope_min":0.0005,"bounce_k":0.12,
                  "signal_cooldown_s":3600,"mode":"night","base_tf":"15m"})

def handle_cmd(text):
    raw=text.strip()
    parts=raw.split()
    cmd=parts[0].lower().split('@')[0] if parts else ""

    if cmd=="/start":
        send_tg("🤖 KuCoin EMA бот готов. Напиши /help.")
    elif cmd=="/help":
        send_tg(
            "Команды:\n"
            "/status\n"
            "/quietpp | /quiet | /night | /soft | /hard | /mode insane|turbo|ultra|normal\n"
            "/setfilters eps atr_k slope_min\n"
            "/setbounce K\n"
            "/setcooldown N\n"
            "/setnosig N   (минут между «нет сигнала»)\n"
            "/setcheck N\n"
            "/settf TF\n"
            "/setsymbols A B C\n"
            "/candles SYMBOL [TF]\n"
            "/report [TF]\n"
            "/autoreport on|off [минут]\n"
            "/setbatch N | /setthrottle S"
        )
    elif cmd=="/status":
        send_tg(
            f"🩺 mode={state['mode']} tf={state['base_tf']} (fb {state['fallback_tf']}) check={state['check_s']}s\n"
            f"eps={state['eps_pct']} atr_k={state['atr_k']} slope_min={state['slope_min']} bounce_k={state['bounce_k']}\n"
            f"cooldown={state['signal_cooldown_s']}s  no_sig={state['no_sig_cooldown_s']}s  "
            f"batch={state['batch_size']} throttle={state['per_req_sleep']}s\n"
            f"symbols={state['symbols']}\n"
            f"report={'on' if state['report_enabled'] else 'off'} every={state['report_every_min']}m\n{fmt_dt()}"
        )
    elif cmd=="/quietpp":
        apply_preset_quietpp(); send_tg("🎛 QUIET++ (ещё мягче, TF 5m)")
    elif cmd=="/quiet":
        state.update({"eps_pct":0.0017,"atr_k":0.28,"slope_min":0.0001,"bounce_k":0.20,
                      "signal_cooldown_s":600,"mode":"quiet","base_tf":"5m"})
        send_tg("🤫 QUIET (мягко)")
    elif cmd=="/night":
        apply_preset_night(); send_tg("🌙 NIGHT (очень тихо, TF 15m, 1 сигнал/ч и реже)")
    elif cmd=="/soft":
        state.update({"eps_pct":0.003,"atr_k":0.08,"slope_min":-0.002,"bounce_k":0.40,"mode":"soft","base_tf":"5m"})
        send_tg("🎛 SOFT preset")
    elif cmd=="/hard":
        state.update({"eps_pct":0.0015,"atr_k":0.20,"slope_min":-0.0002,"bounce_k":0.25,"mode":"hard","base_tf":"5m"})
        send_tg("🎛 HARD preset")
    elif cmd=="/mode":
        if len(parts)>1:
            m=parts[1].lower()
            mp={"normal":(0.0015,0.20,-0.0002),"ultra":(0.0020,0.10,-0.0010),
                "turbo":(0.0030,0.06,-0.0020),"insane":(0.0050,0.04,-0.0040)}
            if m in mp:
                e,a,s=mp[m]; state.update({"eps_pct":e,"atr_k":a,"slope_min":s,"mode":m,"base_tf":"5m"})
                send_tg(f"mode={m} eps={e} atr_k={a} slope_min={s}")
            else:
                send_tg("unknown mode. use: insane|turbo|ultra|normal")
        else:
            send_tg("format: /mode ultra")
    elif cmd=="/setfilters":
        if len(parts)<4:
            send_tg(f"текущие: eps={state['eps_pct']} atr_k={state['atr_k']} slope_min={state['slope_min']}")
        else:
            try:
                eps=float(parts[1]); ak=float(parts[2]); sm=float(parts[3])
                state.update({"eps_pct":eps,"atr_k":ak,"slope_min":sm})
                send_tg(f"ok: eps={eps} atr_k={ak} slope_min={sm}")
            except:
                send_tg("формат: /setfilters 0.0021 0.35 0.0003")
    elif cmd=="/setbounce":
        try:
            v=float(parts[1]); v=max(0.1,min(1.0,v)); state["bounce_k"]=v; send_tg(f"bounce_k={v}")
        except: send_tg("формат: /setbounce 0.16")
    elif cmd=="/setcooldown":
        try:
            v=int(parts[1]); v=max(60,min(7200,v)); state["signal_cooldown_s"]=v; send_tg(f"cooldown={v}")
        except: send_tg("формат /setcooldown 60..7200")
    elif cmd=="/setnosig":
        try:
            mins=int(parts[1]); v=max(10,min(1440,mins))*60
            state["no_sig_cooldown_s"]=v; send_tg(f"no-signal every ≥ {mins}m")
        except: send_tg("формат /setnosig 120  (минут)")
    elif cmd=="/setcheck":
        try:
            v=int(parts[1]); state["check_s"]=max(5,min(120,v)); send_tg(f"check interval = {state['check_s']}s")
        except: send_tg("формат: /setcheck 20")
    elif cmd=="/settf":
        try: v=parts[1]; state["base_tf"]=v; send_tg(f"TF={v}")
        except: send_tg("формат /settf 1m|5m|15m|1h|4h|1d")
    elif cmd=="/setsymbols":
        try:
            syms=[s.upper() for s in parts[1:]]; state["symbols"]=syms; state["rr_index"]=0
            send_tg(f"symbols={state['symbols']}")
        except: send_tg("формат /setsymbols BTC-USDT ETH-USDT ...")
    elif cmd=="/candles":
        sym=parts[1].upper() if len(parts)>1 else "BTC-USDT"
        tf =parts[2] if len(parts)>2 else state["base_tf"]
        send_tg(build_candles_report(sym,tf))
    elif cmd=="/report":
        tf=parts[1] if len(parts)>1 else state["base_tf"]
        for m in build_all_report(tf): send_tg("🧾 Отчёт EMA/ATR\n"+m)
    elif cmd=="/autoreport":
        if len(parts)<2:
            send_tg(f"autoreport={'on' if state['report_enabled'] else 'off'} every={state['report_every_min']}m"); return
        mode=parts[1].lower()
        if mode=="on":
            mins=int(parts[2]) if len(parts)>2 else state["report_every_min"]
            state["report_enabled"]=True; state["report_every_min"]=max(10,min(1440,mins))
            send_tg(f"✅ autoreport ON, every {state['report_every_min']}m")
        elif mode=="off":
            state["report_enabled"]=False; send_tg("⛔ autoreport OFF")
        else: send_tg("формат: /autoreport on|off [минут]")
    else:
        send_tg("🤷 Не знаю такую команду. Напиши /help")

# ===== Потоки =====
def tg_loop():
    # гарантируем polling (без webhook)
    try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook", timeout=6)
    except: pass

    url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"; offset=None
    while True:
        try:
            j=requests.get(url,params={"timeout":20,"offset":offset},timeout=25).json()
            if j.get("ok"):
                for u in j.get("result", []):
                    offset=u["update_id"]+1
                    msg=u.get("message",{}) or {}
                    chat=str(msg.get("chat",{}).get("id",""))
                    if chat!=str(TELEGRAM_CHAT_ID): continue
                    t=msg.get("text","")
                    if t: handle_cmd(t)
        except Exception as e:
            print("tg loop error:", e)
        time.sleep(1)

def signals_worker():
    send_tg("🤖 KuCoin EMA бот (Ultra-Quiet, TF 15m) запущен. /help")
    while True:
        try:
            for s in next_batch():
                try: check_symbol(s)
                except Exception as e: print("check_symbol error", s, e)
            time.sleep(max(1,int(state["check_s"])))
        except Exception as e:
            print("signals loop error:", e); time.sleep(2)

def report_worker():
    last=0
    while True:
        try:
            if state["report_enabled"] and now_ts()-last >= state["report_every_min"]*60:
                last=now_ts()
                send_tg(f"🧾 Автоотчёт активен. Интервал {state['report_every_min']} мин — {fmt_dt()}")
        except Exception as e:
            print("report worker error:", e)
        time.sleep(10)

@app.route("/")
def root(): return "ok"

if __name__=="__main__":
    apply_preset_ultra_quiet()   # стартуем очень тихо
    threading.Thread(target=signals_worker,daemon=True).start()
    threading.Thread(target=tg_loop,daemon=True).start()
    threading.Thread(target=report_worker,daemon=True).start()
    port=int(os.environ.get("PORT","10000"))
    app.run(host="0.0.0.0", port=port)
