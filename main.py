# -*- coding: utf-8 -*-
import os
import time
import math
import json
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify

# ========= ТВОИ ДАННЫЕ (можешь оставить как есть) =========
# Лучше брать из переменных окружения на Render, но по твоим просьбам вписал напрямую.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "5723086631")  # строкой — так надёжнее
# ==========================================================

app = Flask(__name__)

# -------- Общие настройки --------
FUT_SUFFIX = "_UMCBL"  # Bitget USDT-M perpetual
DEFAULT_SYMBOLS = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","TRXUSDT","PEPEUSDT"]

CONFIG = {
    "symbols": DEFAULT_SYMBOLS[:],
    "base_tf": "5m",
    "fallback_tf": "1m",      # используем для "усиления" сигналов, если на 5m тихо
    "ema_fast": 9,
    "ema_slow": 21,
    "min_candles": 10,
    "strength": 0.05,         # доля (0.05=5%)
    "atr_min": 0.05,          # доля; по итогу парсинга приведём к 0.05..0.10
    "atr_max": 0.10,
    "check_interval_s": 120,  # период проверки
    "cooldown_min": 10,       # антиспам по символу
    "near_cross_eps": 0.001,  # 0.10% близость EMA9/EMA21
    "ema_slope_min": 0.0,     # фильтр наклона EMA9 (0=отключен)
}

# Разрешённые пределы для ATR (доли)
ATR_MIN_ALLOWED = 0.001   # 0.10%
ATR_MAX_ALLOWED = 0.20    # 20.0%

# Антиспам и живость
_last_signal_ts_any = 0.0
_last_alive_notice_ts = 0.0
_symbol_cooldown = {}    # { "BTCUSDT": unixtime_last_signal }
_updates_offset = 0

# --------- Утилиты времени/логов ----------
def now_ts() -> float:
    return time.time()

def ts_to_str(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# --------- Telegram ----------
def tg_url(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"

def send_msg(chat_id: str, text: str, disable_web_page_preview=True):
    try:
        requests.post(
            tg_url("sendMessage"),
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": disable_web_page_preview,
                "parse_mode": "HTML"
            },
            timeout=15
        )
    except Exception as e:
        log(f"TG send error: {e}")

def _parse_pct(x: str) -> float:
    """
    Принятая нотация:
    - '0.05' или '.05' -> 0.05 (5%)
    - '5' или '5%'     -> 0.05 (5%)
    """
    s = x.strip().lower().replace(',', '.')
    if s.endswith('%'):
        s = s[:-1].strip()
        val = float(s) / 100.0
        return val
    if s.startswith('.'):
        s = '0' + s
    val = float(s)
    if val > 1.0:
        val = val / 100.0
    return val

def apply_mode(mode: str):
    m = mode.lower()
    if m == "ultra":
        CONFIG.update({
            "strength": 0.03,           # 3%
            "atr_min": 0.05,            # 5%
            "atr_max": 0.10,            # 10% (у тебя код исторически держал 5..10)
            "min_candles": 8,
            "cooldown_min": 5,
            "check_interval_s": 60,
            "near_cross_eps": 0.0015,   # 0.15%
            "ema_slope_min": 0.0,
        })
        return True
    elif m == "normal":
        CONFIG.update({
            "strength": 0.05,           # 5%
            "atr_min": 0.05,            # 5%
            "atr_max": 0.10,            # 10%
            "min_candles": 15,
            "cooldown_min": 15,
            "check_interval_s": 300,
            "near_cross_eps": 0.001,    # 0.10%
            "ema_slope_min": 0.0,
        })
        return True
    return False

# --------- Bitget свечи ----------
BITGET_HOST = "https://api.bitget.com"
HISTORY_CANDLES = "/api/mix/v1/market/history-candles"

# TF -> granularity
GRANULARITY_MAP = {
    "1m": "1min",
    "3m": "3min",
    "5m": "5min",
    "15m": "15min",
}

def fetch_candles(symbol: str, tf: str, limit: int = 300):
    """
    Возвращает candles в виде списков (ts, open, high, low, close) — от старых к новым.
    """
    inst_id = f"{symbol}{FUT_SUFFIX}"
    gran = GRANULARITY_MAP.get(tf, "5min")
    params = {"symbol": inst_id, "granularity": gran}
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        r = requests.get(BITGET_HOST + HISTORY_CANDLES, params=params, headers=headers, timeout=15)
        data = r.json()
        if data.get("code") not in (None, "00000", 0):
            log(f"Bitget error {data.get('code')}: {data.get('msg')}")
            return []
        arr = data.get("data") or []
        # Формат: [timestamp(ms), open, high, low, close, volume, ...] — строки
        # Разворачиваем к старым -> новым
        arr = list(reversed(arr))
        out = []
        for row in arr[-limit:]:
            ts_ms = int(float(row[0]))
            o = float(row[1]); h = float(row[2]); l = float(row[3]); c = float(row[4])
            out.append((ts_ms/1000.0, o, h, l, c))
        return out
    except Exception as e:
        log(f"fetch_candles error {symbol} {tf}: {e}")
        return []

# --------- ТА: EMA и ATR ----------
def ema_series(values, period):
    """
    Экспоненциальная средняя. Возвращает список такой же длины.
    """
    if not values or period <= 0:
        return []
    k = 2.0 / (period + 1.0)
    ema = []
    s = sum(values[:period]) / period if len(values) >= period else sum(values)/len(values)
    ema.append(s)
    start = 1
    if len(values) >= period:
        start = period
    for i in range(start, len(values)):
        s = values[i]*k + ema[-1]*(1-k)
        ema.append(s)
    # выровнять длину
    if len(ema) < len(values):
        ema = [ema[0]]*(len(values)-len(ema)) + ema
    return ema

def atr_percent(highs, lows, closes, period=14):
    """
    Возвращает ATR в долях (например 0.05=5%) — берём ATR/close.
    """
    n = len(closes)
    if n < period+1:
        return None
    trs = []
    prev_close = closes[0]
    for i in range(1, n):
        tr = max(highs[i]-lows[i], abs(highs[i]-prev_close), abs(lows[i]-prev_close))
        trs.append(tr)
        prev_close = closes[i]
    # simple moving average TR за 'period' последних
    if len(trs) < period:
        return None
    atr = sum(trs[-period:]) / period
    last_close = closes[-1]
    if last_close <= 0:
        return None
    return atr / last_close

# --------- Логика силы/сигнала ----------
def compute_strength(ema_f, ema_s):
    # %-расхождение EMA: |EMA9-EMA21|/EMA21
    return abs(ema_f - ema_s) / max(1e-9, ema_s)

def near_cross(ema_f, ema_s, eps):
    return abs(ema_f - ema_s) / max(1e-9, ema_s) <= eps

def slope(series):
    if len(series) < 2:
        return 0.0
    return (series[-1] - series[-2]) / max(1e-9, series[-2])

def should_alert(ema9, ema21, ema9_series, atr_val):
    # 1) ATR в коридоре
    if atr_val is None or not (CONFIG["atr_min"] <= atr_val <= CONFIG["atr_max"]):
        return False, f"ATR {atr_val*100:.2f}% вне [{CONFIG['atr_min']*100:.2f}..{CONFIG['atr_max']*100:.2f}]"
    # 2) сила сигнала
    st = compute_strength(ema9, ema21)
    if st >= CONFIG["strength"]:
        return True, f"strength {st*100:.2f}% >= {CONFIG['strength']*100:.2f}%"
    # 3) near-cross + наклон EMA9
    if near_cross(ema9, ema21, CONFIG["near_cross_eps"]):
        sl = slope(ema9_series)
        if sl >= CONFIG["ema_slope_min"]:
            return True, f"near-cross {CONFIG['near_cross_eps']*100:.2f}%, slope {sl:.5f}"
    return False, f"weak strength {st*100:.2f}%"

def tf_label(tf: str) -> str:
    return tf

# --------- Основной цикл проверок ----------
def analyze_symbol(symbol: str):
    """
    Возвращает (signal_text|None, reason, used_tf)
    """
    # Сначала базовый TF
    for tf in (CONFIG["base_tf"], CONFIG["fallback_tf"]):
        candles = fetch_candles(symbol, tf)
        if len(candles) < CONFIG["min_candles"]:
            reason = f"недостаточно свечей ({len(candles)}<{CONFIG['min_candles']}) на {tf}"
            continue

        closes = [c[4] for c in candles]
        highs  = [c[2] for c in candles]
        lows   = [c[3] for c in candles]

        ema9  = ema_series(closes, CONFIG["ema_fast"])
        ema21 = ema_series(closes, CONFIG["ema_slow"])

        if len(ema9) < CONFIG["min_candles"] or len(ema21) < CONFIG["min_candles"]:
            reason = f"EMA не готовы на {tf}"
            continue

        e9  = ema9[-1]
        e21 = ema21[-1]
        atr = atr_percent(highs, lows, closes, period=14)

        ok, why = should_alert(e9, e21, ema9, atr if atr is not None else 0.0)
        if not ok:
            # тихо — пытаемся другим tf (fallback) на следующей итерации for
            reason = why
            if tf == CONFIG["fallback_tf"]:
                return None, reason, tf
            continue

        side = "LONG 📈" if e9 > e21 else "SHORT 📉"
        msg = (
            f"🔔 <b>{symbol}</b> {side}\n"
            f"TF: {tf_label(tf)} | ATR: {atr*100:.2f}% | ΔEMA: {abs(e9-e21)/e21*100:.2f}%\n"
            f"Причина: {why}\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        return msg, why, tf

    # Ни на одном TF не прошло
    return None, reason if 'reason' in locals() else "тихо", CONFIG["base_tf"]

def can_alert_symbol(symbol: str) -> bool:
    last = _symbol_cooldown.get(symbol, 0.0)
    return (now_ts() - last) >= CONFIG["cooldown_min"]*60.0

def mark_alert_symbol(symbol: str):
    _symbol_cooldown[symbol] = now_ts()

def scan_loop():
    global _last_signal_ts_any, _last_alive_notice_ts
    log("Scan loop started")
    send_msg(TELEGRAM_CHAT_ID, "🤖 Бот запущен (сигнальный). Используй /mode ultra или /mode normal для пресета.")
    while True:
        started = now_ts()
        any_signal = False
        reasons = []  # собираем краткие причины по монетам, полезно для живого отчёта

        for sym in CONFIG["symbols"]:
            try:
                text, reason, used_tf = analyze_symbol(sym)
                if text and can_alert_symbol(sym):
                    send_msg(TELEGRAM_CHAT_ID, text)
                    mark_alert_symbol(sym)
                    any_signal = True
                    _last_signal_ts_any = now_ts()
                else:
                    reasons.append(f"{sym}:{used_tf} {reason}")
            except Exception as e:
                reasons.append(f"{sym}: ошибка {e}")

        # Если за последний час не было сигналов — отправим «жив»
        nowt = now_ts()
        if (nowt - _last_signal_ts_any) >= 3600 and (nowt - _last_alive_notice_ts) >= 3600:
            msg = "🟡 Жив. За последний час сигналов не было.\n" \
                  + ("Причины:\n" + "\n".join(reasons[:10]) if reasons else "")
            send_msg(TELEGRAM_CHAT_ID, msg)
            _last_alive_notice_ts = nowt

        # Держим период
        elapsed = now_ts() - started
        sleep_s = max(1.0, CONFIG["check_interval_s"] - elapsed)
        time.sleep(sleep_s)

# --------- Telegram команды (Long Poll) ----------
def handle_command(chat_id: str, text: str):
    # только из твоего чата
    if str(chat_id) != str(TELEGRAM_CHAT_ID):
        return

    parts = text.strip().split()
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd == "/start":
        send_msg(chat_id, "👋 Готов к работе. Команды: /status, /mode ultra|normal, /setstrength, /setatr, /setmincandles, /setcooldown, /setcheck, /setsymbols")
    elif cmd == "/status":
        cfg = CONFIG
        msg = (
            "📟 <b>Статус</b>\n"
            f"Монеты: {', '.join(cfg['symbols'])}\n"
            f"TF: base={cfg['base_tf']} fallback={cfg['fallback_tf']}\n"
            f"EMA: {cfg['ema_fast']}/{cfg['ema_slow']}\n"
            f"MIN_CANDLES: {cfg['min_candles']}\n"
            f"strength: {cfg['strength']*100:.2f}%\n"
            f"ATR: {cfg['atr_min']*100:.2f}% — {cfg['atr_max']*100:.2f}%\n"
            f"check: {cfg['check_interval_s']}s | cooldown: {cfg['cooldown_min']}m\n"
        )
        send_msg(chat_id, msg)
    elif cmd == "/mode":
        if len(args) != 1:
            send_msg(chat_id, "❌ Пример: /mode ultra  | /mode normal")
            return
        if apply_mode(args[0]):
            send_msg(chat_id, f"✅ Режим {args[0].upper()} применён.")
            handle_command(chat_id, "/status")
        else:
            send_msg(chat_id, "❌ Неизвестный режим. Используй ultra | normal")
    elif cmd == "/setstrength":
        if len(args) != 1:
            send_msg(chat_id, "❌ Пример: /setstrength 0.03  (или 3%)")
            return
        try:
            val = _parse_pct(args[0])
            if not (0.001 <= val <= 0.20):
                send_msg(chat_id, "❌ strength допустимо 0.10%..20%")
                return
            CONFIG["strength"] = val
            send_msg(chat_id, f"✅ strength = {val*100:.2f}%")
        except Exception as e:
            send_msg(chat_id, f"❌ Ошибка: {e}")
    elif cmd == "/setatr":
        if len(args) != 2:
            send_msg(chat_id, "❌ Пример: /setatr 0.05 0.10  (или 5% 10%)")
            return
        try:
            lo = _parse_pct(args[0]); hi = _parse_pct(args[1])
            # Исторически у тебя бот держал 5..10%, оставим такие пределы по умолчанию:
            lo = max(lo, 0.05); hi = min(hi, 0.10)
            if not (lo < hi):
                send_msg(chat_id, "❌ ATR: min должен быть < max. Диапазон 5%..10%.")
                return
            CONFIG["atr_min"], CONFIG["atr_max"] = lo, hi
            send_msg(chat_id, f"✅ ATR: {lo*100:.2f}% — {hi*100:.2f}%")
        except Exception as e:
            send_msg(chat_id, f"❌ Ошибка: {e}")
    elif cmd == "/setmincandles":
        if len(args) != 1:
            send_msg(chat_id, "❌ Пример: /setmincandles 10")
            return
        try:
            v = int(float(args[0]))
            v = max(5, min(200, v))
            CONFIG["min_candles"] = v
            send_msg(chat_id, f"✅ MIN_CANDLES = {v}")
        except Exception as e:
            send_msg(chat_id, f"❌ Ошибка: {e}")
    elif cmd == "/setcooldown":
        if len(args) != 1:
            send_msg(chat_id, "❌ Пример: /setcooldown 10")
            return
        try:
            m = int(float(args[0]))
            m = max(1, min(120, m))
            CONFIG["cooldown_min"] = m
            send_msg(chat_id, f"✅ cooldown = {m} мин")
        except Exception as e:
            send_msg(chat_id, f"❌ Ошибка: {e}")
    elif cmd == "/setcheck":
        if len(args) != 1:
            send_msg(chat_id, "❌ Пример: /setcheck 120  (секунды)")
            return
        try:
            s = int(float(args[0]))
            s = max(15, min(600, s))
            CONFIG["check_interval_s"] = s
            send_msg(chat_id, f"✅ check = {s} сек")
        except Exception as e:
            send_msg(chat_id, f"❌ Ошибка: {e}")
    elif cmd == "/setsymbols":
        if not args:
            send_msg(chat_id, "❌ Пример: /setsymbols BTCUSDT,ETHUSDT,SOLUSDT")
            return
        raw = " ".join(args)
        parts = [p.strip().upper() for p in raw.replace(";", ",").split(",")]
        parts = [p for p in parts if p.endswith("USDT")]
        if not parts:
            send_msg(chat_id, "❌ Укажи пары через запятую, например: BTCUSDT,ETHUSDT")
            return
        CONFIG["symbols"] = parts
        send_msg(chat_id, f"✅ Монеты: {', '.join(parts)}")
    else:
        # игнорим неизвестные команды, чтобы не спамить
        pass

def tg_polling_loop():
    global _updates_offset
    log("TG polling started")
    while True:
        try:
            resp = requests.get(
                tg_url("getUpdates"),
                params={"timeout": 50, "offset": _updates_offset},
                timeout=70
            ).json()
            if not resp.get("ok"):
                time.sleep(2)
                continue
            for upd in resp.get("result", []):
                _updates_offset = max(_updates_offset, upd["update_id"] + 1)
                msg = upd.get("message") or upd.get("edited_message")
                if not msg: 
                    continue
                chat_id = msg["chat"]["id"]
                text = msg.get("text", "") or ""
                if not text:
                    continue
                handle_command(chat_id, text)
        except Exception as e:
            log(f"TG poll error: {e}")
            time.sleep(2)

# --------- Flask keep-alive ----------
@app.route("/")
def root():
    return jsonify({"status":"ok","time": datetime.now().isoformat()})

@app.route("/ping")
def ping():
    return "pong", 200

# --------- Запуск ----------
def main():
    # привет и статус
    apply_mode("ultra")  # можно сразу ультра — по твоему запросу
    log("Starting threads...")
    t1 = threading.Thread(target=scan_loop, daemon=True)
    t2 = threading.Thread(target=tg_polling_loop, daemon=True)
    t1.start(); t2.start()

    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
