# -*- coding: utf-8 -*-
"""
EMA(9/21)+ATR сигнальный бот • KuCoin SPOT (ENTRY preset by default — мягкие фильтры)
— анти-лимиты KuCoin: батчи, троттлинг, ретраи при 429
— пресеты: /entry, /quietpp, /quiet, /soft, /hard, /night, /mode insane|turbo|ultra|normal
— тонкая настройка: /setfilters, /setbounce, /setcooldown, /setcheck, /settf, /setsymbols, /setnosig, /setbatch, /setthrottle
— отчёты/диагностика: /candles, /report, /autoreport, /status, /help
— сводка «нет сигналов»: /nosigall on|off [каждые_мин] [мин_без_сигнала], /nosigallstatus
"""

import os, time, threading, requests, random
from datetime import datetime
from collections import defaultdict
from flask import Flask

# === ТВОИ ДАННЫЕ (как просил) ===
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
# =================================

# Символы KuCoin (формат с дефисом!)
DEFAULT_SYMBOLS = [
    "BTC-USDT","ETH-USDT","BNB-USDT","SOL-USDT","XRP-USDT","ADA-USDT","DOGE-USDT","TRX-USDT",
    "TON-USDT","LINK-USDT","LTC-USDT","DOT-USDT","ARB-USDT","OP-USDT","PEPE-USDT","SHIB-USDT"
]

# Базовые параметры
EMA_FAST, EMA_SLOW     = 9, 21
BASE_TF, FALLBACK_TF   = "5m", "15m"  # ENTRY: мягче на 5m, резерв 15m
MIN_CANDLES            = 120          # достаточно истории

# KuCoin API
KUCOIN_BASE    = "https://api.kucoin.com"
KUCOIN_CANDLES = KUCOIN_BASE + "/api/v1/market/candles"
HEADERS        = {"User-Agent": "ema-kucoin-bot/3.3-entry-nosigall"}

# Flask + состояние
app = Flask(__name__)
state = {
    # рынок / расчёты
    "symbols": DEFAULT_SYMBOLS[:],
    "base_tf": BASE_TF,
    "fallback_tf": FALLBACK_TF,
    "min_candles": MIN_CANDLES,
    "ema_fast": EMA_FAST,
    "ema_slow": EMA_SLOW,

    # тайминги (мягче = чаще проверки и короче кулдауны)
    "check_s": 15,               # пауза между батчами
    "signal_cooldown_s": 420,    # 7 мин между сигналами по одной паре
    "no_sig_cooldown_s": 3600,   # «нет сигнала» не чаще, чем раз в 60 минут
    "error_cooldown_s": 600,     # 10 мин

    # чувствительность (мягкие фильтры для большего числа входов)
    "eps_pct": 0.0012,           # допуск для «почти-кросс»
    "atr_k":   0.18,             # дифф EMA должен быть >= 0.18*ATR (низкий порог)
    "slope_min": -0.0001,        # разрешаем почти плоские/слабые наклоны EMA9
    "slope21_min": 0.00003,      # лёгкий тренд-фильтр EMA21
    "dead_pct": 0.0004,          # маленькая «мёртвая зона» — больше сигналов
    "bounce_k": 0.28,            # отскок от EMA21 — мягкий

    "mode": "entry",

    # отчёты
    "report_enabled": True,
    "report_every_min": 120,     # автоотчёт

    # сводка "нет сигналов" одним сообщением
    "nosig_all_enabled": True,       # включено по умолчанию
    "nosig_all_every_min": 120,      # как часто слать сводку (мин)
    "nosig_all_min_age_min": 60,     # упоминать пары, где нет сигналов >= N минут

    # анти-лимиты
    "batch_size": 8,             # больше монет за цикл
    "per_req_sleep": 0.25,
    "rr_index": 0,
    "max_retries": 3,
    "backoff_base": 0.7,
}

cool_signal = defaultdict(float)
cool_no     = defaultdict(float)
cool_err    = defaultdict(float)
last_sig    = defaultdict(float)   # когда по символу был последний сигнал (ts)

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
            "1h":"1hour","4h":"4hour","1d":"1day"}.get(tf,"5min")

def tf_seconds(tf):
    return {"1m":60,"5m":300,"15m":900,"30m":1800,"1h":3600,"4h":14400,"1d":86400}.get(tf,300)

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

def fetch_candles(symbol, tf, want=320, drop_last_unclosed=True):
    """
    KuCoin отдаёт [t, o, c, h, l, v]; ниже переставляем в (t,o,h,l,c).
    drop_last_unclosed=True — отбрасываем последнюю «незакрытую» свечу (чтобы не перерисовывало).
    """
    try:
        r=kucoin_get(KUCOIN_CANDLES,{"symbol":symbol,"type":tf_to_kucoin(tf)},timeout=10)
        j=r.json()
    except Exception as e: return None,f"bad resp {e}"
    if j.get("code")!="200000": return None,f"KuCoin error {j.get('msg')}"
    rows=[]
    for v in j.get("data",[]):
        try: rows.append((int(v[0]),float(v[1]),float(v[3]),float(v[4]),float(v[2])))  # t,o,h,l,c
        except: pass
    if not rows: return None,"empty"
    rows.sort()
    if drop_last_unclosed and len(rows)>=1:
        t_last=rows[-1][0]//1000
        if now_ts() - t_last < tf_seconds(tf):  # свеча незакрыта — отбросим
            rows = rows[:-1]
    if not rows: return None,"only-unclosed"
    rows = rows[-want:]
    t=[x[0] for x in rows]; o=[x[1] for x in rows]; h=[x[2] for x in rows]; l=[x[3] for x in rows]; c=[x[4] for x in rows]
    time.sleep(state["per_req_sleep"])
    return {"t":t,"o":o,"h":h,"l":l,"c":c},None

# ===== Логика сигналов =====
def cross_or_near(e9,e21,price,eps_abs,dead_abs):
    if len(e9)<2 or len(e21)<2 or e9[-1] is None or e21[-1] is None: return None
    prev=(e9[-2]-e21[-2]) if (e9[-2] is not None and e21[-2] is not None) else None
    curr=e9[-1]-e21[-1]

    # «мёртвая зона»: маленькая разница — игнор
    if abs(curr) < dead_abs:
        return None

    # кроссы
    if prev is not None and prev<=0<curr: return "LONG","кросс ↑"
    if prev is not None and prev>=0>curr: return "SHORT","кросс ↓"

    # почти-кросс (ближе к цене)
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

def decide_signal(e9,e21,atr_arr,price,eps_pct,atr_k,slope_min,slope21_min,dead_pct):
    if not e9 or not e21 or e9[-1] is None or e21[-1] is None: return None,"нет EMA"
    eps_abs  = price*eps_pct
    dead_abs = price*dead_pct

    # тренд EMA21 (даже в мягком режиме — лёгкая проверка)
    s21 = e21[-1] - (e21[-2] if e21[-2] is not None else e21[-1])

    v=cross_or_near(e9,e21,price,eps_abs,dead_abs)
    if v:
        side,note=v
        slope=e9[-1]-(e9[-2] if e9[-2] is not None else e9[-1])
        if slope < slope_min:
            return None,"slope9"
        # лёгкий тренд-фильтр (совсем слабый, чтобы не душить входы)
        if (side=="LONG" and s21 < slope21_min) or (side=="SHORT" and s21 > -slope21_min):
            return None,"slope21"
        if atr_arr and atr_arr[-1] is not None:
            a=atr_arr[-1]; diff=abs(e9[-1]-e21[-1])
            if diff < a*atr_k and abs(diff) > eps_abs:
                return None,"atr"
        return side,note

    a=atr_arr[-1] if atr_arr and atr_arr[-1] is not None else None
    v=bounce_signal(e9,e21,price,a)
    if v:
        side,_=v
        if (side=="LONG" and s21 < slope21_min) or (side=="SHORT" and s21 > -slope21_min):
            return None,"bounce21"
        return v
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
        candles,err=fetch_candles(sym,tf,320,drop_last_unclosed=True)
        if not candles:
            if now_ts()-cool_err[sym] >= state["error_cooldown_s"]:
                cool_err[sym]=now_ts(); send_tg(f"❌ {sym}: {err}")
            return
        c=candles["c"]; h=candles["h"]; l=candles["l"]
        if len(c)<state["min_candles"]:
            maybe_no_signal(sym); return

        e9=ema(c,state["ema_fast"]); e21=ema(c,state["ema_slow"]); atr_a=atr(h,l,c)
        side,note=decide_signal(
            e9,e21,atr_a,c[-1],
            state["eps_pct"],state["atr_k"],state["slope_min"],state["slope21_min"],state["dead_pct"]
        )
        if side:
            cool_signal[sym]=now_ts()+state["signal_cooldown_s"]
            last_sig[sym] = now_ts()  # отметка времени последнего сигнала
            send_tg(make_text(sym,side,c[-1],tf,note)); return
        else:
            maybe_no_signal(sym); return

# ===== Отчёты =====
def fmt_pct(x):
    try: return f"{x*100:.3f}%"
    except: return "—"

def build_candles_report(sym, tf):
    cndl, err = fetch_candles(sym, tf, 180, drop_last_unclosed=True)
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
    n=max(1,min(len(syms),int(state.get("batch_size",8))))
    i=int(state.get("rr_index",0))%len(syms)
    batch=(syms+syms)[i:i+n]
    state["rr_index"]=(i+n)%len(syms)
    return batch

def apply_preset_entry():
    # максимально «мягкий» пресет для более частых входов (по умолчанию)
    state.update({
        "base_tf": "5m",
        "fallback_tf": "15m",
        "min_candles": 120,

        "check_s": 15,
        "signal_cooldown_s": 420,    # 7 минут
        "no_sig_cooldown_s": 3600,   # 60 минут
        "error_cooldown_s": 600,

        "eps_pct": 0.0012,
        "atr_k": 0.18,
        "slope_min": -0.0001,
        "slope21_min": 0.00003,
        "dead_pct": 0.0004,
        "bounce_k": 0.28,

        "batch_size": 8,
        "per_req_sleep": 0.25,
        "mode": "entry"
    })

def apply_preset_quietpp():
    # мягко, но тише, чем entry (для снижения шума)
    state.update({
        "eps_pct":0.0026,"atr_k":0.42,"slope_min":0.00045,"slope21_min":0.00012,"dead_pct":0.0010,
        "bounce_k":0.12, "signal_cooldown_s":2100, "mode":"quiet++", "base_tf":"15m",
        "fallback_tf":"30m","check_s":30,"batch_size":4
    })

def apply_preset_ultra_quiet():
    # очень тихо (для ночи/работы)
    state.update({
        "eps_pct":0.0030,"atr_k":0.50,"slope_min":0.0006,"slope21_min":0.00015,"dead_pct":0.0012,
        "bounce_k":0.10,"signal_cooldown_s":2700,"mode":"ultra-quiet+","base_tf":"15m",
        "fallback_tf":"30m","check_s":30,"batch_size":4
    })

def apply_preset_night():
    # максимально тихо
    state.update({
        "eps_pct":0.0038,"atr_k":0.60,"slope_min":0.0008,"slope21_min":0.00022,"dead_pct":0.0016,
        "bounce_k":0.08,"signal_cooldown_s":3600,"mode":"night","base_tf":"30m",
        "fallback_tf":"1h","check_s":45,"batch_size":3
    })

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
            "/entry (мягкий пресет — больше входов)\n"
            "/quietpp | /quiet | /night | /soft | /hard | /mode insane|turbo|ultra|normal\n"
            "/setfilters eps atr_k slope_min [slope21_min] [dead_pct]\n"
            "/setbounce K\n"
            "/setcooldown N\n"
            "/setnosig N   (минут между «нет сигнала»)\n"
            "/setcheck N\n"
            "/settf TF\n"
            "/setsymbols A B C\n"
            "/candles SYMBOL [TF]\n"
            "/report [TF]\n"
            "/autoreport on|off [минут]\n"
            "/setbatch N | /setthrottle S\n"
            "/nosigall on|off [каждые_мин] [мин_без_сигнала]\n"
            "/nosigallstatus"
        )
    elif cmd=="/status":
        send_tg(
            f"🩺 mode={state['mode']} tf={state['base_tf']} (fb {state['fallback_tf']}) check={state['check_s']}s\n"
            f"eps={state['eps_pct']} atr_k={state['atr_k']} slope9={state['slope_min']} slope21={state['slope21_min']} dead={state['dead_pct']}\n"
            f"bounce_k={state['bounce_k']} cooldown={state['signal_cooldown_s']}s  no_sig={state['no_sig_cooldown_s']}s\n"
            f"batch={state['batch_size']} throttle={state['per_req_sleep']}s\n"
            f"nosig_all={'on' if state['nosig_all_enabled'] else 'off'} every={state['nosig_all_every_min']}m min_age={state['nosig_all_min_age_min']}m\n"
            f"symbols={state['symbols']}\n"
            f"report={'on' if state['report_enabled'] else 'off'} every={state['report_every_min']}m\n{fmt_dt()}"
        )

    # === Пресеты
    elif cmd=="/entry":
        apply_preset_entry(); send_tg("🚀 ENTRY preset: мягкие фильтры и чаще входы (TF 5m)")
    elif cmd=="/quietpp":
        apply_preset_quietpp(); send_tg("🎛 QUIET++ (тише, TF 15m)")
    elif cmd=="/quiet":
        state.update({
            "eps_pct":0.0022,"atr_k":0.36,"slope_min":0.00035,"slope21_min":0.00010,"dead_pct":0.0009,
            "bounce_k":0.14,"signal_cooldown_s":1500,"mode":"quiet","base_tf":"15m",
            "fallback_tf":"30m","check_s":25,"batch_size":5
        })
        send_tg("🤫 QUIET (мягко, но тише)")
    elif cmd=="/night":
        apply_preset_night(); send_tg("🌙 NIGHT (очень тихо, TF 30m+)")
    elif cmd=="/soft":
        state.update({
            "eps_pct":0.0020,"atr_k":0.28,"slope_min":0.00015,"slope21_min":0.00005,"dead_pct":0.0008,
            "bounce_k":0.18,"mode":"soft","base_tf":"15m","fallback_tf":"30m","check_s":20
        })
        send_tg("🎛 SOFT preset")
    elif cmd=="/hard":
        state.update({
            "eps_pct":0.0018,"atr_k":0.24,"slope_min":0.00010,"slope21_min":0.00004,"dead_pct":0.0007,
            "bounce_k":0.20,"mode":"hard","base_tf":"5m","fallback_tf":"15m","check_s":12
        })
        send_tg("🎛 HARD preset")
    elif cmd=="/mode":
        if len(parts)>1:
            m=parts[1].lower()
            mp={"normal":(0.0020,0.30,0.00018,0.00006,0.0009,"15m","30m",18,900),
                "ultra": (0.0028,0.46,0.00055,0.00014,0.0012,"15m","30m",24,2100),
                "turbo": (0.0032,0.52,0.00070,0.00018,0.0014,"10m","30m",16,1800),
                "insane":(0.0045,0.65,0.00090,0.00025,0.0018,"30m","1h", 30,3600)}
            if m in mp:
                e,a,s,s21,dead,tf,fb,chk,cool = mp[m]
                state.update({"eps_pct":e,"atr_k":a,"slope_min":s,"slope21_min":s21,"dead_pct":dead,
                              "mode":m,"base_tf":tf,"fallback_tf":fb,"check_s":chk,"signal_cooldown_s":cool})
                send_tg(f"mode={m} eps={e} atr_k={a} slope9={s} slope21={s21} dead={dead} tf={tf} fb={fb}")
            else:
                send_tg("unknown mode. use: insane|turbo|ultra|normal")
        else:
            send_tg("format: /mode ultra")

    # === Тонкая настройка
    elif cmd=="/setfilters":
        # /setfilters eps atr_k slope_min [slope21_min] [dead_pct]
        try:
            if len(parts)<4:
                send_tg(f"текущие: eps={state['eps_pct']} atr_k={state['atr_k']} slope9={state['slope_min']} slope21={state['slope21_min']} dead={state['dead_pct']}")
            else:
                eps=float(parts[1]); ak=float(parts[2]); sm=float(parts[3])
                s21=float(parts[4]) if len(parts)>4 else state["slope21_min"]
                dead=float(parts[5]) if len(parts)>5 else state["dead_pct"]
                state.update({"eps_pct":eps,"atr_k":ak,"slope_min":sm,"slope21_min":s21,"dead_pct":dead})
                send_tg(f"ok: eps={eps} atr_k={ak} slope9={sm} slope21={s21} dead={dead}")
        except:
            send_tg("формат: /setfilters 0.0012 0.18 -0.0001 0.00003 0.0004")
    elif cmd=="/setbounce":
        try:
            v=float(parts[1]); v=max(0.05,min(1.0,v)); state["bounce_k"]=v; send_tg(f"bounce_k={v}")
        except: send_tg("формат: /setbounce 0.28")
    elif cmd=="/setcooldown":
        try:
            v=int(parts[1]); v=max(60,min(14400,v)); state["signal_cooldown_s"]=v; send_tg(f"cooldown={v}")
        except: send_tg("формат /setcooldown 60..14400")
    elif cmd=="/setnosig":
        try:
            mins=int(parts[1]); v=max(10,min(2880,mins))*60
            state["no_sig_cooldown_s"]=v; send_tg(f"no-signal every ≥ {mins}m")
        except: send_tg("формат /setnosig 60  (минут)")
    elif cmd=="/setcheck":
        try:
            v=int(parts[1]); state["check_s"]=max(5,min(180,v)); send_tg(f"check interval = {state['check_s']}s")
        except: send_tg("формат: /setcheck 15")
    elif cmd=="/settf":
        try:
            v=parts[1]; state["base_tf"]=v; send_tg(f"TF={v}")
        except: send_tg("формат /settf 1m|5m|15m|30m|1h|4h|1d")
    elif cmd=="/setsymbols":
        try:
            syms=[s.upper() for s in parts[1:]]; state["symbols"]=syms; state["rr_index"]=0
            send_tg(f"symbols={state['symbols']}")
        except: send_tg("формат /setsymbols BTC-USDT ETH-USDT ...")
    elif cmd=="/setbatch":
        try:
            v=int(parts[1]); v=max(1,min(50,v)); state["batch_size"]=v; send_tg(f"batch_size={v}")
        except: send_tg("формат /setbatch 8")
    elif cmd=="/setthrottle":
        try:
            v=float(parts[1]); v=max(0.05,min(2.0,v)); state["per_req_sleep"]=v; send_tg(f"throttle={v}s")
        except: send_tg("формат /setthrottle 0.25")

    # === Сводка «нет сигналов»
    elif cmd == "/nosigall":
        # /nosigall on|off [every_min] [min_age_min]
        try:
            if len(parts) < 2:
                send_tg(
                    f"nosig_all_enabled={'on' if state['nosig_all_enabled'] else 'off'} | "
                    f"every={state['nosig_all_every_min']}m | min_age={state['nosig_all_min_age_min']}m"
                )
            else:
                mode = parts[1].lower()
                if mode == "on":
                    every = int(parts[2]) if len(parts) > 2 else state["nosig_all_every_min"]
                    age   = int(parts[3]) if len(parts) > 3 else state["nosig_all_min_age_min"]
                    state["nosig_all_enabled"] = True
                    state["nosig_all_every_min"] = max(10, min(1440, every))
                    state["nosig_all_min_age_min"] = max(5, min(1440, age))
                    send_tg(f"✅ nosig_all ON | every={state['nosig_all_every_min']}m | min_age={state['nosig_all_min_age_min']}m")
                elif mode == "off":
                    state["nosig_all_enabled"] = False
                    send_tg("⛔ nosig_all OFF")
                else:
                    send_tg("формат: /nosigall on|off [каждые_мин] [мин_без_сигнала]")
        except:
            send_tg("формат: /nosigall on|off [каждые_мин] [мин_без_сигнала]")

    elif cmd == "/nosigallstatus":
        send_tg(
            f"nosig_all_enabled={'on' if state['nosig_all_enabled'] else 'off'} | "
            f"every={state['nosig_all_every_min']}m | min_age={state['nosig_all_min_age_min']}m"
        )

    # === Отчёты
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
    send_tg("🤖 KuCoin EMA бот (ENTRY, TF 5m) запущен. /help")
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

def nosig_all_worker():
    last = 0
    warmup_done = False  # дадим боту чуть поработать перед первой сводкой
    while True:
        try:
            # ждём до старта хотя бы 1 полный цикл проверок
            if not warmup_done:
                time.sleep(max(60, state["check_s"] * 2))
                warmup_done = True

            if not state.get("nosig_all_enabled", True):
                time.sleep(10)
                continue

            every_s = max(10, state.get("nosig_all_every_min", 120)) * 60
            min_age = max(5,  state.get("nosig_all_min_age_min", 60)) * 60

            if now_ts() - last >= every_s:
                last = now_ts()
                stale = []
                for sym in state["symbols"]:
                    age = now_ts() - last_sig[sym]
                    # если никогда не было сигналов, считаем возраст бесконечным → упоминать
                    if last_sig[sym] == 0 or age >= min_age:
                        stale.append(sym)

                if stale:
                    chunks = []
                    line = []
                    # компактно порежем список по ~20 символов в строке
                    for i, s in enumerate(stale, 1):
                        line.append(s)
                        if i % 20 == 0:
                            chunks.append(", ".join(line)); line = []
                    if line: chunks.append(", ".join(line))

                    msg_head = f"ℹ️ Нет новых сигналов ≥ {int(min_age/60)} мин\n" \
                               f"Сводка на {fmt_dt()} (TF {state['base_tf']}):"
                    send_tg(msg_head)
                    for part in chunks:
                        send_tg(part)
                else:
                    # всё ок — тихо пропустим, чтобы не спамить
                    pass
        except Exception as e:
            print("nosig_all worker error:", e)
        time.sleep(5)

@app.route("/")
def root(): return "ok"

if __name__=="__main__":
    apply_preset_entry()   # стартуем мягко
    threading.Thread(target=signals_worker,daemon=True).start()
    threading.Thread(target=tg_loop,daemon=True).start()
    threading.Thread(target=report_worker,daemon=True).start()
    threading.Thread(target=nosig_all_worker,daemon=True).start()
    # Поднимем Flask-сервер (важно для Render/ Railway)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT","10000")))
