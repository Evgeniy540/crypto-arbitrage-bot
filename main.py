# -*- coding: utf-8 -*-
import os, time, math, threading, requests, json
from datetime import datetime, timezone
from flask import Flask

# ==== ТВОИ ДАННЫЕ ====
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"   # можно строкой; в коде сравнивается как int
# =====================

FUT_SUFFIX = "_UMCBL"
SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","TRXUSDT",
    "LINKUSDT","NEARUSDT","ATOMUSDT","INJUSDT","SUIUSDT",
    "DOTUSDT","OPUSDT","ARBUSDT","APTUSDT","LTCUSDT","PEPEUSDT"
]

BASE_TF              = "5m"   # 1m/3m/5m/15m/30m/1h/4h/1d
CHECK_INTERVAL_S     = 300    # проверяем раз в 5 минут
SEND_STARTUP         = True

# ===== ПРЕСЕТЫ РЕЖИМОВ (/mode) =====
PRESETS = {
    "aggressive": {
        "TREND_FAST": 20,  "TREND_SLOW": 100, "TREND_CONFIRM_BARS": 1,
        "TREND_TFS": ["5m","15m","1h"], "TREND_ALERT_COOLDOWN_MIN": 5,
        "STRENGTH_MIN": 0.0010, "ATR_MIN_PCT": 0.0005, "ATR_MAX_PCT": 0.0300,
        "RSI_MIN_LONG": 48, "RSI_MAX_SHORT": 52
    },
    "balanced": {
        "TREND_FAST": 50,  "TREND_SLOW": 200, "TREND_CONFIRM_BARS": 2,
        "TREND_TFS": ["15m","1h"], "TREND_ALERT_COOLDOWN_MIN": 15,
        "STRENGTH_MIN": 0.0020, "ATR_MIN_PCT": 0.0010, "ATR_MAX_PCT": 0.0150,
        "RSI_MIN_LONG": 50, "RSI_MAX_SHORT": 50
    },
    "safe": {
        "TREND_FAST": 100, "TREND_SLOW": 200, "TREND_CONFIRM_BARS": 3,
        "TREND_TFS": ["1h","4h"], "TREND_ALERT_COOLDOWN_MIN": 30,
        "STRENGTH_MIN": 0.0030, "ATR_MIN_PCT": 0.0020, "ATR_MAX_PCT": 0.0200,
        "RSI_MIN_LONG": 55, "RSI_MAX_SHORT": 45
    }
}
MODE_FILE = "mode.txt"
_current_mode = None

def save_mode(name: str):
    try:
        with open(MODE_FILE, "w", encoding="utf-8") as f:
            f.write(name.strip())
    except Exception:
        pass

def load_mode() -> str:
    try:
        with open(MODE_FILE, "r", encoding="utf-8") as f:
            name = f.read().strip()
            if name in PRESETS: return name
    except Exception:
        pass
    return "balanced"

def apply_mode(name: str):
    """Применяет пресет в глобальные параметры фильтров/трендов."""
    global TREND_FAST, TREND_SLOW, TREND_CONFIRM_BARS, TREND_TFS, TREND_ALERT_COOLDOWN_MIN
    global STRENGTH_MIN, ATR_MIN_PCT, ATR_MAX_PCT, RSI_MIN_LONG, RSI_MAX_SHORT
    cfg = PRESETS[name]
    TREND_FAST = cfg["TREND_FAST"]
    TREND_SLOW = cfg["TREND_SLOW"]
    TREND_CONFIRM_BARS = cfg["TREND_CONFIRM_BARS"]
    TREND_TFS = cfg["TREND_TFS"]
    TREND_ALERT_COOLDOWN_MIN = cfg["TREND_ALERT_COOLDOWN_MIN"]
    STRENGTH_MIN = cfg["STRENGTH_MIN"]
    ATR_MIN_PCT = cfg["ATR_MIN_PCT"]
    ATR_MAX_PCT = cfg["ATR_MAX_PCT"]
    RSI_MIN_LONG = cfg["RSI_MIN_LONG"]
    RSI_MAX_SHORT = cfg["RSI_MAX_SHORT"]

def format_mode_settings(name: str) -> str:
    c = PRESETS[name]
    lines = [
        f"Режим: {name}",
        f"TREND: EMA{c['TREND_FAST']}/{c['TREND_SLOW']}, підтверждение={c['TREND_CONFIRM_BARS']} бар(ов), TFs={','.join(c['TREND_TFS'])}, cooldown={c['TREND_ALERT_COOLDOWN_MIN']} м.",
        f"STRENGTH_MIN={c['STRENGTH_MIN']*100:.2f}% • ATR={c['ATR_MIN_PCT']*100:.2f}%..{c['ATR_MAX_PCT']*100:.2f}% • RSI long≥{c['RSI_MIN_LONG']} / short≤{c['RSI_MAX_SHORT']}"
    ]
    return "\n".join(lines)

# ===== Значения по умолчанию (будут перезаписаны apply_mode(load_mode())) =====
RSI_MIN_LONG  = 50
RSI_MAX_SHORT = 50
STRENGTH_MIN  = 0.0020   # 0.20%
ATR_MIN_PCT   = 0.0010   # 0.10%
ATR_MAX_PCT   = 0.0150   # 1.50%

# История/окна (умный режим)
NEED_IDEAL     = 210       # цель для 5m
NEED_MIN       = 120       # минимум для 5m
NEED_MIN_HTF   = 60        # минимум для 15m/1h
FETCH_BUFFER   = 60
STEP_BARS      = 100
MAX_WINDOWS    = 30
MAX_TOTAL_BARS = 1000
REQUEST_PAUSE  = 0.25

# Анти-спам
PING_COOLDOWN_MIN   = 60    # «без изменений»/слабые статусы не чаще 1/час
STATE_COOLDOWN_MIN  = 5     # одинаковый статус по тикеру — не чаще, чем раз в 5 мин

# ===== Настройки алертов СМЕНЫ ТРЕНДА (будут перезаписаны пресетом) =====
TREND_FAST = 50
TREND_SLOW = 200
TREND_CONFIRM_BARS = 2
TREND_TFS = ["15m","1h"]
TREND_ALERT_COOLDOWN_MIN = 15
_last_trend = {}  # (symbol, tf) -> (last_trend, ts_last_alert)

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
            json={"chat_id": int(TELEGRAM_CHAT_ID), "text": text}, timeout=10
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
        "1m":"60","3m":"180","5m":"300",
        "15m":"900","30m":"1800","1h":"3600",
        "4h":"14400","1d":"86400"
    }
    return mapping.get(tf, "300")

def _granularity_sec(tf: str) -> int:
    return int(_granularity(tf))

def _parse_rows(rows):
    rows = list(rows)
    rows.reverse()  # API отдаёт от новых к старым -> в хронологию
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
    if isinstance(js, list):  # некоторые регионы/прокси отдают сразу массив
        return _parse_rows(js)
    if isinstance(js, dict) and js.get("code") == "00000" and "data" in js:
        return _parse_rows(js["data"])
    return []

def bitget_candles(symbol, tf="5m", futures=True, need=NEED_IDEAL+FETCH_BUFFER):
    """
    Сбор длинной истории окнами через /history-candles (узкие окна, много шагов).
    Возвращает [(ts,o,h,l,c,v)] от старых к новым.
    """
    full_symbol = symbol + (FUT_SUFFIX if futures else "")
    gran_s = _granularity_sec(tf)

    end_ms = int(time.time() * 1000)
    all_rows = {}
    step_ms = STEP_BARS * gran_s * 1000

    for _ in range(MAX_WINDOWS):
        start_ms = max(0, end_ms - step_ms)
        try:
            part = _fetch_hist_window(full_symbol, gran_s, start_ms, end_ms, futures=futures)
            for ts,o,h,l,c,v in part:
                all_rows[ts] = (ts,o,h,l,c,v)  # де-дупликация
        except Exception:
            break
        end_ms = start_ms - 1
        time.sleep(REQUEST_PAUSE)

    rows = sorted(all_rows.values(), key=lambda x: x[0])
    return rows  # «умный» сбор: дальше режем в get_close_series

def get_close_series(symbol, tf, need=NEED_IDEAL, min_need=NEED_MIN):
    c = bitget_candles(symbol, tf=tf, futures=True, need=need+FETCH_BUFFER)
    if not c or len(c) < min_need:
        return [], []
    # режем лишнее по потолку
    if len(c) > min(MAX_TOTAL_BARS, need + FETCH_BUFFER):
        c = c[-(need+FETCH_BUFFER):]
    closes=[x[4] for x in c]
    return c, closes

# ---------- тренд и сигналы ----------
def trend_dir(closes):
    e50=ema(closes,50); e200=ema(closes,200)
    if math.isnan(e50[-1]) or math.isnan(e200[-1]): return None, e50, e200
    if e50[-1] > e200[-1]:  return "LONG",  e50, e200
    if e50[-1] < e200[-1]:  return "SHORT", e50, e200
    return None, e50, e200

def trend_dir_with_params(closes, fast, slow):
    ef=ema(closes, fast); es=ema(closes, slow)
    if math.isnan(ef[-1]) or math.isnan(es[-1]): return None, ef, es
    if ef[-1] > es[-1]:  return "LONG",  ef, es
    if ef[-1] < es[-1]:  return "SHORT", ef, es
    return None, ef, es

def strength_pct(e_fast, e_slow, close):
    return abs(e_fast - e_slow)/close

def analyze_symbol(sym):
    # базовый ТФ
    c5, cls5 = get_close_series(sym, BASE_TF, need=NEED_IDEAL, min_need=NEED_MIN)
    if not cls5: return ("NO_DATA", f"{sym}_UMCBL: недостаточно данных")

    e50_5 = ema(cls5, 50); e200_5 = ema(cls5, 200)
    rsi5  = rsi(cls5, 14)
    if math.isnan(e200_5[-1]) or math.isnan(rsi5[-1]):
        return ("NO_DATA", f"{sym}_UMCBL: недостаточно индикаторов")

    close5   = cls5[-1]
    dir5     = "LONG" if e50_5[-1] > e200_5[-1] else "SHORT"
    strength = strength_pct(e50_5[-1], e200_5[-1], close5)
    atrp     = atr_pct(c5,14)

    # старшие ТФ с меньшим минимумом
    _, cls15 = get_close_series(sym, "15m", need=NEED_MIN, min_need=NEED_MIN_HTF)
    _, cls1h = get_close_series(sym, "1h",  need=NEED_MIN, min_need=NEED_MIN_HTF)
    dir15, _, _ = trend_dir(cls15) if cls15 else (None, [], [])
    dir1h, _, _ = trend_dir(cls1h) if cls1h else (None, [], [])

    t15_ok_long  = (dir15 == "LONG")
    t1h_ok_long  = (dir1h == "LONG")
    t15_ok_short = (dir15 == "SHORT")
    t1h_ok_short = (dir1h == "SHORT")

    filters_long = (
        dir5 == "LONG" and t15_ok_long and t1h_ok_long and
        strength >= STRENGTH_MIN and rsi5[-1] >= RSI_MIN_LONG and
        (not math.isnan(atrp) and ATR_MIN_PCT <= atrp <= ATR_MAX_PCT)
    )
    filters_short = (
        dir5 == "SHORT" and t15_ok_short and t1h_ok_short and
        strength >= STRENGTH_MIN and rsi5[-1] <= RSI_MAX_SHORT and
        (not math.isnan(atrp) and ATR_MIN_PCT <= atrp <= ATR_MAX_PCT)
    )

    info = (f"Цена: {round(close5,6)} • {BASE_TF}: {dir5}\n"
            f"RSI={round(rsi5[-1],1)} • ATR={round(atrp*100,2)}% • "
            f"Сила={round(strength*100,2)}% • EMA50/200 OK")

    # таймштамп UTC в заголовок сильного сигнала
    now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")

    if filters_long:
        return ("STRONG_LONG", f"🟩 СИЛЬНЫЙ LONG {sym}_UMCBL ({now_str})\n{info}")
    if filters_short:
        return ("STRONG_SHORT", f"🟪 СИЛЬНЫЙ SHORT {sym}_UMCBL ({now_str})\n{info}")
    return ("WEAK", f"⚪ {sym}_UMCBL: фильтры НЕ собраны\n{info}")

# --- детектор смены тренда на HTF ---
def detect_trend_change(sym, tf, need, min_need):
    _, cls = get_close_series(sym, tf, need=need, min_need=min_need)
    if not cls: return None
    ef = ema(cls, TREND_FAST)
    es = ema(cls, TREND_SLOW)

    # подтверждение: последние N баров подряд один и тот же знак
    states = []
    for i in range(TREND_CONFIRM_BARS):
        a = ef[-1 - i]; b = es[-1 - i]
        if math.isnan(a) or math.isnan(b): return None
        states.append("LONG" if a > b else "SHORT" if a < b else None)
    if None in states or not all(s == states[0] for s in states):
        return None
    curr = states[0]

    key = (sym, tf)
    prev, ts_prev = _last_trend.get(key, (None, 0))
    now = time.time()
    if prev != curr and (now - ts_prev) >= TREND_ALERT_COOLDOWN_MIN*60:
        _last_trend[key] = (curr, now)
        when = datetime.now(timezone.utc).strftime("%H:%M UTC")
        return f"🔄 Смена тренда {sym}_UMCBL на {tf}: {curr} ({when})"
    # При первом запуске — фиксируем состояние, но не шлём алерт
    if prev is None:
        _last_trend[key] = (curr, now)
    return None

# ---------- анти-спам и отправка ----------
_last_state = {}       # symbol -> (state, ts_sent)
_last_ping_ts = 0

def send_changes(msgs):
    if not msgs: return False
    dt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msgs.append(f"⏳ Следующая проверка через {CHECK_INTERVAL_S//60} минут")
    tg("📊 Обновления (" + BASE_TF + ") — " + dt + "\n" + "\n\n".join(msgs))
    return True

def check_once():
    global _last_ping_ts
    now = time.time()
    changed_msgs = []

    for s in SYMBOLS:
        # 1) основной анализ
        try:
            state, text = analyze_symbol(s)
        except Exception as e:
            state, text = ("ERR", f"{s}_UMCBL: ошибка данных — {e}")

        last_state, last_ts = _last_state.get(s, (None, 0))

        if state in ("STRONG_LONG", "STRONG_SHORT"):
            if state != last_state or (now - last_ts >= STATE_COOLDOWN_MIN*60):
                changed_msgs.append(text)
                _last_state[s] = (state, now)
        elif state == "WEAK":
            # слабые/нейтральные — максимум раз в час
            if (state != last_state and (now - _last_ping_ts >= PING_COOLDOWN_MIN*60)):
                changed_msgs.append(text)
                _last_state[s] = (state, now)
                _last_ping_ts = now
        else:
            # NO_DATA / ERR — тоже не чаще 1/час
            if (state != last_state and (now - _last_ping_ts >= PING_COOLDOWN_MIN*60)):
                changed_msgs.append(text)
                _last_state[s] = (state, now)
                _last_ping_ts = now

        # 2) алерты смены тренда по выбранным ТФ
        for tf in TREND_TFS:
            msg = detect_trend_change(
                s, tf,
                need=NEED_MIN if tf in ("15m","1h") else NEED_IDEAL,
                min_need=NEED_MIN_HTF if tf in ("15m","1h") else NEED_MIN
            )
            if msg:
                changed_msgs.append(msg)

    sent = send_changes(changed_msgs)
    if (not sent) and (now - _last_ping_ts >= PING_COOLDOWN_MIN*60):
        dt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        tg(f"ℹ️ Без изменений по фильтрам ({BASE_TF}) — {dt}\n⏳ Следующая проверка через {CHECK_INTERVAL_S//60} минут")
        _last_ping_ts = now

# ---------- Telegram команды (/mode, /help) ----------
def tg_send(chat_id: int, text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text}, timeout=10
        )
    except Exception:
        pass

def handle_command(chat_id: int, text: str):
    global _current_mode
    if chat_id != int(TELEGRAM_CHAT_ID):
        tg_send(chat_id, "⛔ Нет доступа.")
        return
    t = (text or "").strip().lower()
    if t == "/help" or t == "/start":
        tg_send(chat_id,
            "Команды:\n"
            "/mode — показать режим и доступные пресеты\n"
            "/mode aggressive — включить агрессивный режим\n"
            "/mode balanced — включить сбалансированный режим\n"
            "/mode safe — включить консервативный режим"
        )
        return
    if t == "/mode":
        msg = "Текущие настройки:\n" + format_mode_settings(_current_mode) + \
              "\n\nДоступно: aggressive / balanced / safe\nПример: /mode aggressive"
        tg_send(chat_id, msg)
        return
    if t.startswith("/mode "):
        name = t.split(" ", 1)[1].strip()
        if name not in PRESETS:
            tg_send(chat_id, "Неизвестный режим. Доступно: aggressive / balanced / safe")
            return
        apply_mode(name)
        save_mode(name)
        _current_mode = name
        tg_send(chat_id, "✅ Режим применён.\n" + format_mode_settings(name))
        return
    tg_send(chat_id, "Неизвестная команда. Напиши /help")

def tg_poll_loop():
    """Лонг-поллинг Telegram для команд /mode (без вебхука)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    last_update_id = None
    while True:
        try:
            params = {"timeout": 50}
            if last_update_id is not None:
                params["offset"] = last_update_id + 1
            r = requests.get(url, params=params, timeout=60)
            r.raise_for_status()
            js = r.json()
            if not js.get("ok"):
                time.sleep(2); continue
            for upd in js.get("result", []):
                last_update_id = upd.get("update_id", last_update_id)
                msg = upd.get("message") or upd.get("edited_message")
                if not msg: continue
                chat = msg.get("chat", {})
                chat_id = chat.get("id")
                text = msg.get("text", "")
                if text and chat_id:
                    handle_command(int(chat_id), text)
        except Exception:
            time.sleep(2)

def loop():
    if SEND_STARTUP:
        tg("🤖 Бот запущен: сильные сигналы сразу, нейтралка ≤ 1/ч, UTC-таймштамп, алерты тренда (15m/1h).\n"
           f"Текущий режим: {_current_mode}")
    while True:
        try:
            check_once()
        except Exception as e:
            tg(f"⚠️ Главный цикл: ошибка — {e}")
        time.sleep(CHECK_INTERVAL_S)

# ====== STARTUP ======
if __name__ == "__main__":
    # загрузить и применить режим
    _current_mode = load_mode()
    apply_mode(_current_mode)

    # стартуем Flask (healthcheck) + polling команд
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=tg_poll_loop, daemon=True).start()
    # основной цикл
    loop()
