# -*- coding: utf-8 -*-
"""
main.py — сигнальный EMA-бот для Bitget (UMCBL фьючерсы, только сигналы)
- Свечи: /api/mix/v1/market/history-candles (PUBLIC)
- EMA(9/21), пресеты /mode ultra|normal, ручные /set*
- Антиспам "нет сигнала" не чаще 1/час на символ
- Flask для Render + фоновая проверка + прием команд через getUpdates
"""

import os
import time
import threading
from datetime import datetime
from collections import defaultdict

import requests
from flask import Flask

# ==== ТВОИ ДАННЫЕ (вписано) ====
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
# ===============================

# Полные фьючерсные символы Bitget (USDT-M perpetual)
SYMBOLS = [
    "BTCUSDT_UMCBL",
    "ETHUSDT_UMCBL",
    "SOLUSDT_UMCBL",
    "XRPUSDT_UMCBL",
    "TRXUSDT_UMCBL"
]

# Базовые настройки
BASE_TF = "5m"
FALLBACK_TF = "15m"
CHECK_INTERVAL_S = 60
MIN_CANDLES = 120
EMA_FAST, EMA_SLOW = 9, 21

# Фильтры
EPS_PCT = 0.0008     # близость EMA (0.08% от цены)
ATR_FACTOR = 0.25    # фильтр по волатильности
SLOPE_MIN = 0.0      # минимальный наклон быстрой EMA

# Антиспам
NO_SIGNAL_COOLDOWN_S = 3600
SIGNAL_COOLDOWN_S = 300

# Bitget публичный эндпоинт
HEADERS = {"User-Agent": "Mozilla/5.0 (EMA-signal-bot/1.0)"}
BITGET_MIX_CANDLES = "https://api.bitget.com/api/mix/v1/market/history-candles"

# Глобальное состояние
app = Flask(__name__)
last_notif_no_signal = defaultdict(lambda: 0.0)
cooldown_per_symbol = defaultdict(lambda: 0.0)
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

# ===== Утилиты
def now_ts(): return time.time()
def fmt_dt(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def send_tg(text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print("Telegram error:", e)

def ema(series, period):
    if len(series) < period: return []
    k = 2.0 / (period+1)
    out = []
    ema_prev = sum(series[:period]) / period
    out.extend([None]*(period-1))
    out.append(ema_prev)
    for x in series[period:]:
        ema_prev = x*k + ema_prev*(1-k)
        out.append(ema_prev)
    return out

def true_range(h,l,c):
    tr=[None]
    for i in range(1, len(c)):
        tr.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    return tr

def atr(h,l,c,period=14):
    tr = true_range(h,l,c)
    vals = [x for x in tr if x is not None]
    if len(vals) < period: return [None]*len(c)
    k = 2.0/(period+1)
    out=[None]; atr_prev = sum(vals[:period])/period
    out.extend([None]*(len(c)-len(vals)-1))
    for i in range(period, len(vals)):
        atr_prev = vals[i]*k + atr_prev*(1-k)
        out.append(atr_prev)
    while len(out) < len(c): out.append(atr_prev)
    return out

def gran_ok(tf: str) -> str:
    # Bitget ждёт секунды
    mapping = {
        "1m":"60","5m":"300","15m":"900","30m":"1800",
        "1h":"3600","4h":"14400","1d":"86400"
    }
    return mapping.get(tf, "300")

def parse_candles(data):
    rows=[]
    for row in data:
        try:
            ts=int(row[0])/1000.0; o=float(row[1]); h=float(row[2]); l=float(row[3]); c=float(row[4])
            rows.append((ts,o,h,l,c))
        except: pass
    rows.sort(key=lambda x: x[0])
    t=[r[0] for r in rows]; o=[r[1] for r in rows]; h=[r[2] for r in rows]; l=[r[3] for r in rows]; c=[r[4] for r in rows]
    return t,o,h,l,c

def fetch_candles(full_symbol: str, tf: str, limit: int = 300):
    params = {"symbol": full_symbol, "granularity": gran_ok(tf), "limit": str(limit)}
    try:
        r = requests.get(BITGET_MIX_CANDLES, params=params, headers=HEADERS, timeout=20)
        j = r.json()
    except Exception as e:
        return None, f"Network error: {e}"
    if not isinstance(j, dict): return None, "Bad response"
    if j.get("code") != "00000":
        return None, f"Bitget error {j.get('code')}: {j.get('msg')}"
    data = j.get("data", [])
    if not data: return None, "No candles"
    t,o,h,l,c = parse_candles(data)
    return {"t":t,"o":o,"h":h,"l":l,"c":c}, None

def last_cross_signal(efast, eslow, eps_pct, slope_min, atr_arr, atr_k):
    if not efast or not eslow or efast[-1] is None or eslow[-1] is None:
        return None, "нет EMA"
    if len(efast) < 3 or len(eslow) < 3:
        return None, "мало EMA"
    df_prev = efast[-2]-eslow[-2] if efast[-2] is not None and eslow[-2] is not None else None
    df_curr = efast[-1]-eslow[-1]
    price = efast[-1]
    eps_abs = price*eps_pct
    slope = (efast[-1]-efast[-2]) if efast[-2] is not None else 0.0
    if slope < slope_min: return None, "slope низкий"
    a = atr_arr[-1] if atr_arr and atr_arr[-1] is not None else None
    if a is not None and abs(df_curr) < a*atr_k: return None, "diff < ATR*k"
    if df_prev is not None and (df_prev <= 0.0 < df_curr): return "LONG", "кросс вверх"
    if df_prev is not None and (df_prev >= 0.0 > df_curr): return "SHORT","кросс вниз"
    if abs(df_curr) <= eps_abs:
        return ("LONG" if slope>0 else "SHORT"), "близко к кроссу"
    return None, "нет условия"

def maybe_send_no_signal(sym):
    ts = now_ts()
    if ts - last_notif_no_signal[sym] >= NO_SIGNAL_COOLDOWN_S:
        last_notif_no_signal[sym] = ts
        send_tg(f"ℹ️ По {sym} сейчас нет сигнала ({fmt_dt()}).")

def make_signal_text(sym, side, price, tf, note):
    arrow = "🟢 LONG" if side=="LONG" else "🔴 SHORT"
    return (f"📣 <b>{sym}</b> | TF <b>{tf}</b>\n"
            f"{arrow} | Цена ~ <b>{price:.4f}</b>\n"
            f"Причина: {note}\n"
            f"{fmt_dt()}")

# ===== Логика проверки
def check_symbol(full_symbol: str):
    if now_ts() < cooldown_per_symbol[full_symbol]:
        return
    candles, err = fetch_candles(full_symbol, state["base_tf"], limit=max(300, state["min_candles"]+50))
    tf_used = state["base_tf"]
    if err:
        # fallback на 15m
        candles_fb, err_fb = fetch_candles(full_symbol, state["fallback_tf"], limit=max(300, state["min_candles"]+50))
        if candles_fb:
            candles = candles_fb; tf_used = state["fallback_tf"]
        else:
            send_tg(f"❌ Ошибка {full_symbol}: {err or err_fb}")
            return

    t,o,h,l,c = candles["t"], candles["o"], candles["h"], candles["l"], candles["c"]
    if len(c) < state["min_candles"]:
        maybe_send_no_signal(full_symbol); return

    efast = ema(c, state["ema_fast"])
    eslow = ema(c, state["ema_slow"])
    atr_arr = atr(h, l, c, period=14)

    side, note = last_cross_signal(efast, eslow, state["eps_pct"], state["slope_min"], atr_arr, state["atr_k"])
    if side:
        price = c[-1]
        send_tg(make_signal_text(full_symbol, side, price, tf_used, note))
        cooldown_per_symbol[full_symbol] = now_ts() + state["signal_cooldown_s"]
    else:
        maybe_send_no_signal(full_symbol)

# ===== Пресеты и команды
def apply_mode(mode: str):
    mode = mode.lower()
    if mode == "ultra":
        state["eps_pct"] = 0.0005
        state["atr_k"]   = 0.35
        state["slope_min"] = 0.0
        state["mode"] = "ultra"
    else:
        state["eps_pct"] = 0.0008
        state["atr_k"]   = 0.25
        state["slope_min"] = 0.0
        state["mode"] = "normal"

def handle_command(text: str):
    txt = text.strip()
    if txt.startswith("/mode"):
        parts = txt.split()
        apply_mode(parts[1] if len(parts)>=2 else "normal")
        send_tg(f"✅ mode={state['mode']} | eps={state['eps_pct']}, atr_k={state['atr_k']}")
        return
    if txt.startswith("/setcheck"):
        try:
            v=int(txt.split()[1]); state["check_s"]=max(20,min(600,v)); send_tg(f"⏱ check={state['check_s']}s")
        except: send_tg("Формат: /setcheck 60"); return
        return
    if txt.startswith("/setmins"):
        try:
            v=int(txt.split()[1]); state["min_candles"]=max(40,min(500,v)); send_tg(f"📊 min_candles={state['min_candles']}")
        except: send_tg("Формат: /setmins 120"); return
        return
    if txt.startswith("/seteps"):
        try:
            v=float(txt.split()[1]); state["eps_pct"]=max(0.0001,min(0.005,v)); send_tg(f"⚙️ eps={state['eps_pct']}")
        except: send_tg("Формат: /seteps 0.0008"); return
        return
    if txt.startswith("/setatr"):
        try:
            v=float(txt.split()[1]); state["atr_k"]=max(0.0,min(2.0,v)); send_tg(f"⚙️ atr_k={state['atr_k']}")
        except: send_tg("Формат: /setatr 0.25"); return
        return
    if txt.startswith("/setcooldown"):
        try:
            v=int(txt.split()[1]); state["signal_cooldown_s"]=max(60,min(3600,v)); send_tg(f"🧊 cooldown={state['signal_cooldown_s']}s")
        except: send_tg("Формат: /setcooldown 300"); return
        return
    if txt.startswith("/setsymbols"):
        try:
            payload = txt.split(None,1)[1]
            items = [x.strip().upper() for x in payload.replace(",", " ").split() if x.strip()]
            # ожидаем уже полные названия *_UMCBL
            state["symbols"] = items
            send_tg(f"✅ SYMBOLS:\n{', '.join(state['symbols'])}")
        except: send_tg("Формат: /setsymbols BTCUSDT_UMCBL ETHUSDT_UMCBL ...")
        return
    if txt.startswith("/status"):
        send_tg(
            "🩺 Статус:\n"
            f"symbols: {', '.join(state['symbols'])}\n"
            f"tf: {state['base_tf']} (fb {state['fallback_tf']})\n"
            f"check: {state['check_s']}s, min: {state['min_candles']}\n"
            f"eps: {state['eps_pct']}, atr_k: {state['atr_k']}\n"
            f"cooldown: {state['signal_cooldown_s']}s\n"
            f"time: {fmt_dt()}"
        )
        return
    if txt.startswith("/help"):
        send_tg("Команды: /mode ultra|normal, /setcheck N, /setmins N, /seteps X, /setatr X, /setcooldown N, /setsymbols ..., /status")
        return

def tg_updates_loop():
    offset=None
    url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    while True:
        try:
            params={"timeout":20}
            if offset is not None: params["offset"]=offset
            j=requests.get(url, params=params, timeout=25).json()
            if j.get("ok"):
                for upd in j.get("result", []):
                    offset = upd["update_id"]+1
                    msg = upd.get("message") or upd.get("edited_message")
                    if not msg: continue
                    if str(msg["chat"]["id"]) != TELEGRAM_CHAT_ID: continue
                    text = msg.get("text","")
                    if text: handle_command(text)
        except Exception as e:
            print("tg loop err:", e)
        time.sleep(1)

# ===== Основной воркер
def worker():
    send_tg("🤖 Бот запущен (сигнальный). Используй /mode ultra или /mode normal.")
    # Быстрая диагностика: пробуем BTCUSDT_UMCBL 5m
    test, err = fetch_candles("BTCUSDT_UMCBL", state["base_tf"], 100)
    if err: send_tg(f"⚠️ Диагностика Bitget: {err}")
    else:   send_tg("✅ Bitget подключен (history-candles OK).")

    while True:
        start = now_ts()
        for sym in state["symbols"][:]:
            try:
                check_symbol(sym)
            except Exception as e:
                print("check_symbol error", sym, e)
        spent = now_ts()-start
        time.sleep(max(2.0, state["check_s"]-spent))

# ===== Flask (Render keep-alive)
@app.route("/")
def root(): return "ok"

@app.route("/ping")
def ping(): return {"ok": True, "time": fmt_dt()}

def run_threads():
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=tg_updates_loop, daemon=True).start()

if __name__ == "__main__":
    run_threads()
    port=int(os.environ.get("PORT","10000"))
    app.run(host="0.0.0.0", port=port)
