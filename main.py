# -*- coding: utf-8 -*-
import os, time, math, threading, requests
from datetime import datetime, timezone
from flask import Flask

# ==== ТВОИ ДАННЫЕ ====
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
# =====================

FUT_SUFFIX = "_UMCBL"
SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","TRXUSDT",
    "LINKUSDT","NEARUSDT","ATOMUSDT","INJUSDT","SUIUSDT",
    "DOTUSDT","OPUSDT","ARBUSDT","APTUSDT","LTCUSDT","PEPEUSDT"
]

BASE_TF          = "5m"   # 1m / 3m / 5m / 15m / 30m / 1h / 4h / 1d
CHECK_INTERVAL_S = 60
SEND_STARTUP     = True

# Пороги фильтров
RSI_MIN_LONG  = 50
RSI_MAX_SHORT = 50
STRENGTH_MIN  = 0.0020
ATR_MIN_PCT   = 0.0030
ATR_MAX_PCT   = 0.0150

# Требования по истории
NEED_IDEAL = 210     # «идеально» для EMA200/RSI14
NEED_MIN   = 120     # «умный минимум»: работаем, если >= 120
FETCH_BUFFER = 60    # запас к need, чтобы EMA были гладкими

# Параметры догрузки истории
STEP_BARS       = 100     # сколько баров затягиваем за один шаг окна
MAX_WINDOWS     = 30      # максимум шагов назад (≈ до 3000 баров запрошено)
MAX_TOTAL_BARS  = 1000    # жёсткий потолок на массив истории
REQUEST_PAUSE   = 0.25    # сек пауза между запросами /history-candles

# ---------- infra ----------
app = Flask(__name__)
@app.route("/")
def root(): return "OK"

def run_flask():
    port = int(os.environ.get("PORT","8000"))
    app.run(host="0.0.0.0", port=port)

def tg(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10
        )
    except Exception:
        pass

# ---------- индикаторы ----------
def ema(vals, n):
    if len(vals) < n: return [math.nan]*len(vals)
    k = 2/(n+1)
    out = [math.nan]*(n-1)
    s  = sum(vals[:n])/n
    out.append(s)
    p = s
    for x in vals[n:]:
        p = x*k + p*(1-k)
        out.append(p)
    return out

def rsi(vals, n=14):
    if len(vals) < n+1: return [math.nan]*len(vals)
    gains=[0.0]; losses=[0.0]
    for i in range(1,len(vals)):
        d = vals[i]-vals[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag = sum(gains[1:n+1])/n; al = sum(losses[1:n+1])/n
    rsis=[math.nan]*n
    def rsi_from(g,l): return 100.0 if l==0 else 100 - 100/(1+g/l)
    rsis.append(rsi_from(ag,al))
    for i in range(n+1,len(vals)):
        ag=(ag*(n-1)+gains[i])/n; al=(al*(n-1)+losses[i])/n
        rsis.append(rsi_from(ag,al))
    return rsis

def true_range(h,l,c_prev): return max(h-l, abs(h-c_prev), abs(l-c_prev))

def atr_pct(candles, n=14):
    if len(candles) < n+1: return math.nan
    trs=[]
    for i in range(1,len(candles)):
        _,o,h,l,c,_ = candles[i]
        _,o0,h0,l0,c0,_ = candles[i-1]
        trs.append(true_range(h,l,c0))
    atr = sum(trs[-n:])/n
    close = candles[-1][4]
    return atr/close

# ---------- данные ----------
def _granularity(tf: str) -> str:
    tf = tf.lower().strip()
    mapping = {
        "1m":"60","1min":"60",
        "3m":"180","3min":"180",
        "5m":"300","5min":"300",
        "15m":"900","15min":"900",
        "30m":"1800","30min":"1800",
        "1h":"3600","4h":"14400","1d":"86400","1day":"86400"
    }
    return mapping.get(tf, "300")

def _granularity_sec(tf: str) -> int:
    return int(_granularity(tf))

def _parse_rows(rows):
    """rows -> [(ts,o,h,l,c,v)] от старых к новым"""
    rows = list(rows)
    rows.reverse()  # API отдаёт от новых к старым
    out=[]
    for R in rows:
        try:
            ts=int(R[0])//1000
            o,h,l,c,v = map(float, R[1:6])
            out.append((ts,o,h,l,c,v))
        except Exception:
            continue
    return out

def _fetch_hist_window(full_symbol, gran_s, start_ms, end_ms, futures=True):
    """Один запрос к /history-candles. Возвращает [(ts,o,h,l,c,v)] или []."""
    base = "https://api.bitget.com/api/mix/v1/market/history-candles" if futures \
           else "https://api.bitget.com/api/spot/v1/market/history-candles"
    headers = {"User-Agent":"Mozilla/5.0","Accept":"application/json"}
    params = {
        "symbol": full_symbol,
        "granularity": str(gran_s),
        "startTime": str(start_ms),
        "endTime":   str(end_ms),
    }
    r = requests.get(base, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    js = r.json()
    if isinstance(js, list):
        return _parse_rows(js)
    if isinstance(js, dict) and js.get("code") == "00000" and "data" in js:
        return _parse_rows(js["data"])
    return []

def bitget_candles(symbol, tf="5m", futures=True, need=NEED_IDEAL+FETCH_BUFFER):
    """
    Сбор длинной истории окнами через /history-candles (узкие окна, много шагов).
    Если не удалось — фолбэк на /candles.
    Возвращает [(ts,o,h,l,c,v)] от старых к новым.
    """
    full_symbol = symbol + (FUT_SUFFIX if futures else "")
    gran_s   = _granularity_sec(tf)

    end_ms = int(time.time() * 1000)
    all_rows = {}
    total_target = min(MAX_TOTAL_BARS, max(need, NEED_IDEAL+FETCH_BUFFER))
    step_ms = STEP_BARS * gran_s * 1000

    # 1) Идём назад окнами
    for _ in range(MAX_WINDOWS):
        start_ms = max(0, end_ms - step_ms)
        try:
            part = _fetch_hist_window(full_symbol, gran_s, start_ms, end_ms, futures=futures)
            for ts,o,h,l,c,v in part:
                all_rows[ts] = (ts,o,h,l,c,v)  # де-дупликация
        except Exception:
            # при ошибке просто выходим к фолбэку
            break

        if len(all_rows) >= total_target:
            break

        end_ms = start_ms - 1
        time.sleep(REQUEST_PAUSE)

    rows = sorted(all_rows.values(), key=lambda x: x[0])
    if len(rows) >= NEED_MIN:
        # режем по потолку, чтобы не раздувать память
        return rows[-total_target:] if len(rows) > total_target else rows

    # 2) Фолбэк: /candles (limit)
    base_cand = "https://api.bitget.com/api/mix/v1/market/candles" if futures \
                else "https://api.bitget.com/api/spot/v1/market/candles"
    headers = {"User-Agent":"Mozilla/5.0","Accept":"application/json"}
    params = {
        "symbol": full_symbol,
        "granularity": str(gran_s),
        "limit": str(min(total_target, 600))  # обычно до 600 баров
    }
    r2 = requests.get(base_cand, params=params, headers=headers, timeout=15)
    r2.raise_for_status()
    js2 = r2.json()
    if isinstance(js2, list):
        rows2 = _parse_rows(js2)
    elif isinstance(js2, dict) and "data" in js2:
        rows2 = _parse_rows(js2["data"])
    else:
        rows2 = []

    # если у нас совместно (история + фолбэк) >= NEED_MIN — используем
    merged = sorted({x[0]:x for x in rows + rows2}.values(), key=lambda x: x[0])
    return merged[-total_target:] if len(merged) >= NEED_MIN else merged

def get_close_series(symbol, tf, need=NEED_IDEAL):
    # просим «идеал», но работать будем от NEED_MIN
    c = bitget_candles(symbol, tf=tf, futures=True, need=need+FETCH_BUFFER)
    if not c or len(c) < NEED_MIN:   # меньше 120 — реально недостаточно
        return [], []
    # если баров меньше идеала — тоже ок, просто считаем на том, что есть
    if len(c) > min(MAX_TOTAL_BARS, need + FETCH_BUFFER):
        c = c[-(need+FETCH_BUFFER):]
    closes=[x[4] for x in c]
    return c, closes

# ---------- логика ----------
def trend_dir(closes):
    e50=ema(closes,50); e200=ema(closes,200)
    if math.isnan(e50[-1]) or math.isnan(e200[-1]): return None, e50, e200
    if e50[-1] > e200[-1]:  return "LONG",  e50, e200
    if e50[-1] < e200[-1]:  return "SHORT", e50, e200
    return None, e50, e200

def strength_pct(e_fast, e_slow, close):
    return abs(e_fast - e_slow)/close

def analyze_symbol(sym):
    c5, cls5 = get_close_series(sym, BASE_TF, need=NEED_IDEAL)
    if not cls5: return f"{sym}_UMCBL: недостаточно данных на {BASE_TF}"

    e50_5 = ema(cls5, 50); e200_5 = ema(cls5, 200)
    rsi5  = rsi(cls5, 14)
    if math.isnan(e200_5[-1]) or math.isnan(rsi5[-1]):
        return f"{sym}_UMCBL: недостаточно данных для индикаторов ({BASE_TF})"

    close5   = cls5[-1]
    dir5     = "LONG" if e50_5[-1] > e200_5[-1] else "SHORT"
    strength = strength_pct(e50_5[-1], e200_5[-1], close5)
    atrp     = atr_pct(c5,14)

    # контроль тренда на старших ТФ — тоже в «умном» режиме
    _, cls15 = get_close_series(sym, "15m", need=NEED_MIN)
    _, cls1h = get_close_series(sym, "1h",  need=NEED_MIN)
    dir15, _, _ = trend_dir(cls15) if cls15 else (None, [], [])
    dir1h, _, _ = trend_dir(cls1h) if cls1h else (None, [], [])

    t15_ok_long  = (dir15 == "LONG")
    t1h_ok_long  = (dir1h == "LONG")
    t15_ok_short = (dir15 == "SHORT")
    t1h_ok_short = (dir1h == "SHORT")

    filters_green_long = (
        dir5 == "LONG" and t15_ok_long and t1h_ok_long and
        strength >= STRENGTH_MIN and rsi5[-1] >= RSI_MIN_LONG and
        (not math.isnan(atrp) and ATR_MIN_PCT <= atrp <= ATR_MAX_PCT)
    )
    filters_green_short = (
        dir5 == "SHORT" and t15_ok_short and t1h_ok_short and
        strength >= STRENGTH_MIN and rsi5[-1] <= RSI_MAX_SHORT and
        (not math.isnan(atrp) and ATR_MIN_PCT <= atrp <= ATR_MAX_PCT)
    )

    possible_long  = (dir5 == "LONG"  and strength < STRENGTH_MIN and rsi5[-1] >= RSI_MIN_LONG)
    possible_short = (dir5 == "SHORT" and strength < STRENGTH_MIN and rsi5[-1] <= RSI_MAX_SHORT)

    trend_str = f"Тренды 15m/1h: " \
                f"{'OK' if (dir15=='LONG') else '–'}/{ 'OK' if (dir1h=='LONG') else '–' } (для LONG); " \
                f"{'OK' if (dir15=='SHORT') else '–'}/{ 'OK' if (dir1h=='SHORT') else '–' } (для SHORT)"

    info = (f"Цена: {round(close5,6)} • {BASE_TF}: {dir5}\n"
            f"{trend_str}\n"
            f"Сила={round(strength*100,2)}% (≥ {STRENGTH_MIN*100:.2f}%) • "
            f"RSI(14)={round(rsi5[-1],1)} • ATR={round(atrp*100,2)}% в коридоре • "
            f"EMA50/EMA200 OK")

    msgs=[]
    if filters_green_long:
        msgs.append(f"🟢 {sym}_UMCBL: фильтры ЗЕЛЁНЫЕ (LONG)\n{info}")
    if filters_green_short:
        msgs.append(f"🟣 {sym}_UMCBL: фильтры ЗЕЛЁНЫЕ (SHORT)\n{info}")
    if not msgs and possible_long:
        msgs.append(f"⚡ Возможно вход LONG по {sym}_UMCBL\n{info}\n⌛ ждём подтверждения кросса EMA ↑")
    if not msgs and possible_short:
        msgs.append(f"⚡ Возможно вход SHORT по {sym}_UMCBL\n{info}\n⌛ ждём подтверждения кросса EMA ↓")
    if not msgs:
        msgs.append(f"⚪ {sym}_UMCBL: фильтры НЕ собраны\n{info}")
    return "\n".join(msgs)

def check_once():
    lines=[]
    for s in SYMBOLS:
        try:
            lines.append(analyze_symbol(s))
        except requests.HTTPError as he:
            lines.append(f"{s}_UMCBL: HTTP ошибка — {he}")
        except Exception as e:
            lines.append(f"{s}_UMCBL: ошибка данных — {e}")
    dt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tg("📊 Фильтры (" + BASE_TF + ") — " + dt + "\n" + "\n\n".join(lines))

def loop():
    if SEND_STARTUP:
        tg("🤖 Бот запущен (умный сбор истории: окна 100 баров, минимум 120 баров для расчёта).")
    while True:
        try:
            check_once()
        except Exception as e:
            tg(f"⚠️ Главный цикл: ошибка — {e}")
        time.sleep(CHECK_INTERVAL_S)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    loop()
