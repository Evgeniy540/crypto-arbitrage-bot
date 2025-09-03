# -*- coding: utf-8 -*-
"""
EMA(9/21)+ATR сигнальный бот • KuCoin SPOT (супер-мягкие фильтры)
Команды: /status, /mode, /setcooldown, /settf, /setsymbols, /setfilters, /setmincandles, /candles, /report, /autoreport, /help
Фичи: авто-отчёт EMA/ATR раз в N минут (по умолчанию 60)
"""

import os, time, threading, requests
from datetime import datetime
from collections import defaultdict
from flask import Flask

# ==== ТВОИ ДАННЫЕ ====
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
# =====================

# Базовые символы KuCoin SPOT (с дефисом!)
SYMBOLS = ["BTC-USDT","ETH-USDT","SOL-USDT","XRP-USDT","TRX-USDT"]

# Тайминги и чувствительность по умолчанию (супер-мягко)
CHECK_INTERVAL_S     = 15     # проверяем каждые 15с
SIGNAL_COOLDOWN_S    = 60     # антиспам — 60с
NO_SIGNAL_COOLDOWN   = 600    # "нет сигнала" — раз в 10 мин
ERROR_COOLDOWN       = 300
MIN_CANDLES          = 60     # хватит 60 свечей для расчётов
EMA_FAST, EMA_SLOW   = 9, 21
BASE_TF, FALLBACK_TF = "1m", "5m"  # агрессивнее по умолчанию

# Режимы
FILTERS = {
    "normal": {"eps": 0.0012, "atr_k": 0.15, "slope_min": -0.0005},
    "ultra":  {"eps": 0.0020, "atr_k": 0.10, "slope_min": -0.0010},
    "turbo":  {"eps": 0.0030, "atr_k": 0.06, "slope_min": -0.0020},
    "insane": {"eps": 0.0050, "atr_k": 0.04, "slope_min": -0.0040},  # по умолчанию
}

# KuCoin API
KUCOIN_BASE    = "https://api.kucoin.com"
KUCOIN_CANDLES = KUCOIN_BASE + "/api/v1/market/candles"
HEADERS        = {"User-Agent": "ema-kucoin-bot/2.0"}

# Состояние
app = Flask(__name__)
state = {
    "symbols": SYMBOLS[:],
    "base_tf": BASE_TF,
    "fallback_tf": FALLBACK_TF,
    "check_s": CHECK_INTERVAL_S,
    "min_candles": MIN_CANDLES,
    "ema_fast": EMA_FAST,
    "ema_slow": EMA_SLOW,
    "eps_pct": FILTERS["insane"]["eps"],
    "atr_k": FILTERS["insane"]["atr_k"],
    "slope_min": FILTERS["insane"]["slope_min"],
    "signal_cooldown_s": SIGNAL_COOLDOWN_S,
    "mode": "insane",
    # авто-отчёт
    "report_enabled": True,
    "report_every_min": 60,
}

cool_signal = defaultdict(float)
cool_no     = defaultdict(float)
cool_err    = defaultdict(float)

# ==== Утилиты ====
def now_ts(): return time.time()
def fmt_dt(ts=None): return datetime.fromtimestamp(ts or now_ts()).strftime("%Y-%m-%d %H:%M:%S")

def send_tg(txt):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID,"text":txt,"parse_mode":"HTML"}, timeout=12)
    except Exception as e:
        print("Telegram send error:", e)

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
    }.get(tf, "1min")

def fetch_candles(symbol, tf, want=300):
    try:
        r = requests.get(KUCOIN_CANDLES, params={"symbol":symbol,"type":tf_to_kucoin(tf)}, headers=HEADERS, timeout=10)
        j = r.json()
    except Exception as e:
        return None, f"bad resp {e}"
    if j.get("code")!="200000":
        return None, f"KuCoin error {j.get('msg')}"
    rows=[]
    for v in j.get("data",[]):
        try:
            # формат: [time, open, close, high, low, volume]
            rows.append((int(v[0]), float(v[1]), float(v[2]), float(v[3]), float(v[4])))
        except:
            pass
    if not rows: return None, "empty"
    rows.sort()
    t=[x[0] for x in rows][-want:]
    o=[x[1] for x in rows][-want:]
    c=[x[2] for x in rows][-want:]
    h=[x[3] for x in rows][-want:]
    l=[x[4] for x in rows][-want:]
    return {"t":t,"o":o,"h":h,"l":l,"c":c}, None

# ==== Логика сигналов (расслабленная) ====
def cross_or_near(e9, e21, price, eps_abs):
    """Определяем кросс или «почти кросс»."""
    if len(e9)<2 or len(e21)<2 or e9[-1] is None or e21[-1] is None: return None
    prev = (e9[-2] - e21[-2]) if (e9[-2] is not None and e21[-2] is not None) else None
    curr = e9[-1] - e21[-1]
    # кросс
    if prev is not None and prev <= 0 < curr: return "LONG","кросс ↑"
    if prev is not None and prev >= 0 > curr: return "SHORT","кросс ↓"
    # почти кросс
    if abs(curr) <= eps_abs:
        slope = e9[-1] - (e9[-2] if e9[-2] is not None else e9[-1])
        return ("LONG" if slope >= 0 else "SHORT"), "почти кросс"
    return None

def bounce_signal(e9, e21, price, atr_val):
    """
    «Отскок»: если e9>e21 и цена близко к e21 (<=0.5*ATR) — LONG,
    если e9<e21 и цена близко к e21 — SHORT.
    """
    if e9[-1] is None or e21[-1] is None or atr_val is None: return None
    diff = abs(price - e21[-1])
    if diff <= 0.5*atr_val:
        if e9[-1] >= e21[-1]: return "LONG","отскок от EMA21 ↑"
        else: return "SHORT","отскок от EMA21 ↓"
    return None

def decide_signal(e9, e21, atr_arr, price, eps_pct, atr_k, slope_min):
    if not e9 or not e21 or e9[-1] is None or e21[-1] is None: return None,"нет EMA"
    eps_abs = price * eps_pct
    # 1) кросс/почти кросс
    v = cross_or_near(e9, e21, price, eps_abs)
    if v: 
        side, note = v
        # супер-мягко: slope_min лишь как очень мягкий фильтр
        slope = e9[-1] - (e9[-2] if len(e9)>=2 and e9[-2] is not None else e9[-1])
        if slope < slope_min:
            return None, "slope"
        # ATR-фильтр ослаблен: не блокируем, если diff уже очень мал (почти кросс)
        if atr_arr and atr_arr[-1] is not None:
            a = atr_arr[-1]; diff = abs(e9[-1]-e21[-1])
            if diff > eps_abs and diff < a*atr_k:
                # если diff чуть больше eps, но меньше atr_k — всё равно даём
                pass
        return side, note
    # 2) отскок
    a = atr_arr[-1] if atr_arr and atr_arr[-1] is not None else None
    v = bounce_signal(e9, e21, price, a)
    if v: return v
    return None,"нет"

def maybe_no_signal(sym):
    if now_ts()-cool_no[sym]>=NO_SIGNAL_COOLDOWN:
        cool_no[sym]=now_ts(); send_tg(f"ℹ️ По {sym} пока нет сигнала ({fmt_dt()})")

def make_text(sym, side, price, tf, note):
    return (f"📣 <b>{sym}</b> | TF <b>{tf}</b>\n"
            f"{'🟢 LONG' if side=='LONG' else '🔴 SHORT'} @ <b>{price:.4f}</b>\n"
            f"{note}\n{fmt_dt()}")

def check_symbol(sym):
    if now_ts() < cool_signal[sym]: return
    for tf in (state["base_tf"], state["fallback_tf"]):
        candles, err = fetch_candles(sym, tf, 240)
        if not candles:
            if now_ts()-cool_err[sym] >= ERROR_COOLDOWN:
                cool_err[sym]=now_ts(); send_tg(f"❌ {sym}: {err}")
            return
        c = candles["c"]; h = candles["h"]; l = candles["l"]
        if len(c) < state["min_candles"]:
            maybe_no_signal(sym); return
        e9  = ema(c, state["ema_fast"])
        e21 = ema(c, state["ema_slow"])
        atr_a = atr(h, l, c, period=14)
        side, note = decide_signal(e9, e21, atr_a, c[-1],
                                   state["eps_pct"], state["atr_k"], state["slope_min"])
        if side:
            cool_signal[sym] = now_ts() + state["signal_cooldown_s"]
            send_tg(make_text(sym, side, c[-1], tf, note))
            return
        else:
            maybe_no_signal(sym)
            return

# ===== Диагностика и отчёты =====
def fmt_pct(x): 
    try: return f"{x*100:.3f}%"
    except: return "—"

def build_candles_report(sym, tf):
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
    msgs=[]; block=[]
    for sym in state["symbols"]:
        block.append(build_candles_report(sym, tf))
        if len("\n\n".join(block)) > 3500:
            msgs.append("\n\n".join(block)); block=[]
    if block: msgs.append("\n\n".join(block))
    return msgs

# ===== Команды Telegram =====
def apply_mode(m):
    m = (m or "insane").lower()
    if m in FILTERS:
        f = FILTERS[m]
        state.update({"eps_pct":f["eps"], "atr_k":f["atr_k"], "slope_min":f["slope_min"], "mode":m})
    else:
        f = FILTERS["insane"]
        state.update({"eps_pct":f["eps"], "atr_k":f["atr_k"], "slope_min":f["slope_min"], "mode":"insane"})

def handle_cmd(text):
    if text.startswith("/mode"):
        parts=text.split(); apply_mode(parts[1] if len(parts)>1 else "insane")
        send_tg(f"mode={state['mode']} eps={state['eps_pct']} atr_k={state['atr_k']} slope_min={state['slope_min']}")
    elif text.startswith("/status"):
        send_tg(f"🩺 symbols={state['symbols']}\n"
                f"tf={state['base_tf']} (fb {state['fallback_tf']}) check={state['check_s']}s\n"
                f"mode={state['mode']} eps={state['eps_pct']} atr_k={state['atr_k']} slope_min={state['slope_min']}\n"
                f"report={'on' if state['report_enabled'] else 'off'} every={state['report_every_min']}m\n{fmt_dt()}")
    elif text.startswith("/setcooldown"):
        try: v=int(text.split()[1]); v=max(30,min(1800,v)); state["signal_cooldown_s"]=v; send_tg(f"cooldown={v}")
        except: send_tg("формат /setcooldown 60..1800")
    elif text.startswith("/settf"):
        try: v=text.split()[1]; state["base_tf"]=v; send_tg(f"TF={v}")
        except: send_tg("формат /settf 1m|5m|15m|1h|4h|1d")
    elif text.startswith("/setsymbols"):
        try: syms=text.split()[1:]; state["symbols"]=[s.upper() for s in syms]; send_tg(f"symbols={state['symbols']}")
        except: send_tg("формат /setsymbols BTC-USDT ETH-USDT ...")
    elif text.startswith("/setfilters"):
        # /setfilters eps atr_k slope_min
        parts=text.split()
        if len(parts)<4: 
            send_tg(f"текущие: eps={state['eps_pct']} atr_k={state['atr_k']} slope_min={state['slope_min']}")
        else:
            try:
                eps=float(parts[1]); ak=float(parts[2]); sm=float(parts[3])
                state.update({"eps_pct":eps,"atr_k":ak,"slope_min":sm})
                send_tg(f"ok: eps={eps} atr_k={ak} slope_min={sm}")
            except:
                send_tg("формат: /setfilters 0.005 0.04 -0.004")
    elif text.startswith("/setmincandles"):
        try: v=int(text.split()[1]); v=max(30,min(300,v)); state["min_candles"]=v; send_tg(f"min_candles={v}")
        except: send_tg("формат /setmincandles 60")
    elif text.startswith("/candles"):
        parts=text.split()
        sym=parts[1].upper() if len(parts)>1 else "BTC-USDT"
        tf =parts[2] if len(parts)>2 else state["base_tf"]
        send_tg(build_candles_report(sym,tf))
    elif text.startswith("/report"):
        parts=text.split()
        tf=parts[1] if len(parts)>1 else state["base_tf"]
        for m in build_all_report(tf): send_tg("🧾 Отчёт EMA/ATR\n"+m)
    elif text.startswith("/autoreport"):
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
        send_tg("Команды:\n"
                "/status\n"
                "/mode insane|turbo|ultra|normal\n"
                "/setcooldown N\n"
                "/settf TF\n"
                "/setsymbols A B C\n"
                "/setfilters eps atr_k slope_min\n"
                "/setmincandles N\n"
                "/candles SYMBOL [TF]\n"
                "/report [TF]\n"
                "/autoreport on|off [минут]")

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
        except Exception as e:
            print("tg loop error:", e)
        time.sleep(1)

# ===== Рабочие потоки =====
def signals_worker():
    send_tg("🤖 KuCoin EMA бот (insane) запущен. /help")
    while True:
        for s in state["symbols"]:
            try: check_symbol(s)
            except Exception as e: print("check_symbol error", s, e)
        time.sleep(state["check_s"])

def report_worker():
    last = 0
    while True:
        try:
            if state["report_enabled"] and now_ts()-last >= state["report_every_min"]*60:
                last = now_ts()
                tf = state["base_tf"]
                for m in build_all_report(tf): send_tg("🧾 Авто-отчёт EMA/ATR\n"+m)
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
