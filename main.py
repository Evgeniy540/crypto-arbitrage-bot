# -*- coding: utf-8 -*-
import os
import time
import threading
from datetime import datetime, timezone
from collections import defaultdict

import requests
from flask import Flask, jsonify, request

# ========= ТВОИ ДАННЫЕ =========
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
# ===============================

# -------- Настройки --------
FUT_SUFFIX = "_UMCBL"  # USDT-M perpetual на Bitget

# РАСШИРЕННЫЙ СПИСОК МОНЕТ (25)
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT",
    "BNBUSDT", "ADAUSDT", "DOGEUSDT", "AVAXUSDT",
    "DOTUSDT", "LTCUSDT", "APTUSDT", "ARBUSDT", "OPUSDT",
    "LINKUSDT", "ATOMUSDT", "NEARUSDT", "FILUSDT", "SUIUSDT",
    "PEPEUSDT", "SHIBUSDT", "ETCUSDT", "ICPUSDT", "INJUSDT"
]

WORK_TF = "10min"       # рабочий ТФ для входов
HTF_TF  = "15min"      # 1-й фильтр тренда
HTF2_TF = "1h"         # 2-й фильтр тренда

EMA_FAST, EMA_SLOW = 9, 21
EMA_DIR_PERIOD = 50                 # фильтр направления 1 (средний тренд)
EMA_LONG_PERIOD = 200               # фильтр направления 2 (глобальный тренд)
EMA50_NEEDS_SLOPE = False           # требовать наклон EMA50 по направлению
EMA200_NEEDS_SLOPE = False          # требовать наклон EMA200 по направлению
CANDLES_LIMIT = 600                 # глубокая история

STRENGTH_PCT = 0.002   # 0.20% мин. «сила» кросса
RSI_PERIOD = 14
RSI_MID = 50           # порог RSI

# --- ATR-фильтр волатильности ---
ATR_MIN_PCT = 0.0015   # 0.15% — тонко => блок
ATR_MAX_PCT = 0.03     # 3.00% — шторм => блок

ALERT_COOLDOWN_SEC = 15 * 60
HEARTBEAT_SEC = 60 * 60
REQUEST_TIMEOUT = 12

# Чуть увеличены интервалы, чтобы не упереться в лимиты при 25 парах
SLEEP_BETWEEN_SYMBOLS = 0.35
LOOP_SLEEP = 1.8

RECHECK_FAIL_SEC = 15 * 60

# --- ПРЕДСИГНАЛЫ ---
SETUP_COOLDOWN_SEC = 20 * 60

BASE_URL = "https://api.bitget.com"
_REQ_HEADERS = {"User-Agent": "futures-signal-bot/2.0", "Accept": "application/json"}

# -------- Служебные --------
last_alert_time = defaultdict(lambda: 0.0)
last_heartbeat_time = defaultdict(lambda: 0.0)
last_band_state = {}                                # LONG/SHORT/NEUTRAL (5m)
accepted_params = {}                                # (sym_base, tf) -> dict(...)
disabled_symbols = {}                               # (sym_base, tf) -> dict(...)
last_candles_count = defaultdict(lambda: {"5m": 0, "15m": 0, "1h": 0})
last_filter_gate = defaultdict(lambda: "unknown")   # 'allow' | 'block' | 'unknown'
last_atr_info = defaultdict(lambda: {"atr": None, "atr_pct": None})
last_block_reasons = defaultdict(list)
last_setup_time = defaultdict(lambda: 0.0)

app = Flask(__name__)

# ========= Утилиты =========
_GRAN_TO_SEC = {
    "1":60, "60":60, "1min":60,
    "3":180, "180":180, "3min":180,
    "5":300, "300":300, "5min":300,
    "15":900, "900":900, "15min":900,
    "30":1800, "1800":1800, "30min":1800,
    "60min":3600, "1h":3600, "3600":3600,
    "240":14400, "4h":14400, "14400":14400,
    "21600":21600, "6h":21600,
    "43200":43200, "12h":43200,
    "86400":86400, "1day":86400,
    "604800":604800, "1week":604800,
    "2592000":2592000, "1M":2592000,
}

V2_GRAN_CANDS = {
    "1min": ["1min", "60"], "3min": ["3min", "180"], "5min": ["5min", "300"],
    "15min": ["15min", "900"], "30min": ["30min", "1800"], "1h": ["1h", "3600"],
    "4h": ["4h", "14400"], "6h": ["6h", "21600"], "12h": ["12h", "43200"],
    "1day": ["1day", "86400"], "1week": ["1week", "604800"], "1M": ["1M", "2592000"],
}
V1_GRAN_CANDS = {
    "1min": ["1min", "60", "1"], "3min": ["3min", "180", "3"], "5min": ["5min", "300", "5"],
    "15min": ["15min", "900", "15"], "30min": ["30min", "1800", "30"],
    "1h": ["1h", "3600", "60"], "4h": ["4h", "14400", "240"], "6h": ["6h", "21600", "360"],
    "12h": ["12h", "43200", "720"], "1day": ["1day", "86400", "1D"], "1week": ["1week", "604800", "1W"], "1M": ["1M", "2592000", "1M"],
}

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def send_telegram(text: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[TG] send error: {e}")
        return False

def _safe_json(resp) -> dict:
    try:
        return resp.json()
    except Exception as e:
        print(f"[http] .json() parse error: {e}")
        return {}

# ========= Индикаторы =========
def ema_series(values, period):
    out, k, ema = [], 2.0/(period+1.0), None
    for v in values:
        ema = v if ema is None else (v*k + ema*(1-k))
        out.append(ema)
    return out

def rsi_series(close, period=14):
    if len(close) < period + 2:
        return [50.0]*len(close)
    gains = [max(0.0, close[i]-close[i-1]) for i in range(1, len(close))]
    losses = [max(0.0, close[i-1]-close[i]) for i in range(1, len(close))]
    avg_gain = sum(gains[:period])/period
    avg_loss = sum(losses[:period])/period
    rsis = [50.0]*(period+1)
    rs = (avg_gain/avg_loss) if avg_loss != 0 else 9999
    rsis.append(100 - 100/(1+rs))
    for i in range(period+2, len(close)+1):
        g = gains[i-2]; l = losses[i-2]
        avg_gain = (avg_gain*(period-1)+g)/period
        avg_loss = (avg_loss*(period-1)+l)/period
        rs = (avg_gain/avg_loss) if avg_loss != 0 else 9999
        rsis.append(100 - 100/(1+rs))
    return rsis[:len(close)]

def atr_series(high, low, close, period=14):
    trs = []
    for i in range(len(close)):
        if i == 0:
            trs.append(high[i]-low[i])
        else:
            trs.append(max(
                high[i]-low[i],
                abs(high[i]-close[i-1]),
                abs(low[i]-close[i-1])
            ))
    if len(trs) < period:
        return [None]*len(close)
    out = [None]*(period-1) + [sum(trs[:period])/period]
    for i in range(period, len(trs)):
        out.append((out[-1]*(period-1)+trs[i])/period)
    return out

# ========= Bitget candles =========
def _parse_ohlcv_payload(data):
    rows = data.get("data", []) or []
    out = []
    for row in rows:
        try:
            ts = int(float(row[0]))
            o = float(row[1]); h = float(row[2]); l = float(row[3]); c = float(row[4])
            v = float(row[5]) if len(row) > 5 else 0.0
            out.append([ts, o, h, l, c, v])
        except Exception:
            continue
    out.sort(key=lambda x: x[0])
    return out

def _try_v2(symbol_str: str, gran: str, product_type, limit: int):
    url = f"{BASE_URL}/api/v2/mix/market/candles"
    params = {"symbol": symbol_str, "granularity": gran, "limit": limit}
    if product_type: params["productType"] = product_type
    try:
        r = requests.get(url, params=params, headers=_REQ_HEADERS, timeout=REQUEST_TIMEOUT)
        data = _safe_json(r)
        if str(data.get("code")) == "00000":
            return _parse_ohlcv_payload(data)
    except Exception as e:
        print(f"[v2] exception {symbol_str} {gran} {product_type}: {e}")
    return None

def _try_v1(symbol_str: str, gran: str, limit: int):
    url = f"{BASE_URL}/api/mix/v1/market/candles"
    params = {"symbol": symbol_str, "granularity": gran, "limit": limit}
    try:
        r = requests.get(url, params=params, headers=_REQ_HEADERS, timeout=REQUEST_TIMEOUT)
        data = _safe_json(r)
        if str(data.get("code")) == "00000":
            return _parse_ohlcv_payload(data)
    except Exception as e:
        print(f"[v1] exception {symbol_str} {gran}: {e}")
    return None

def fetch_candles_exact(sym_base: str, tf: str, limit: int):
    key = (sym_base, tf)
    if key in disabled_symbols:
        if time.time() < disabled_symbols[key]["until_ts"]:
            raise RuntimeError(f"{sym_base}{FUT_SUFFIX}/{tf} disabled: {disabled_symbols[key]['reason']}")
        else:
            disabled_symbols.pop(key, None)
            send_telegram(f"✅ Повторная попытка включения {sym_base}{FUT_SUFFIX} [{tf}]")

    if key in accepted_params:
        cfg = accepted_params[key]
        data = _try_v2(cfg["symbol"], cfg["gran"], cfg.get("productType"), limit) if cfg["endpoint"]=="v2" \
               else _try_v1(cfg["symbol"], cfg["gran"], limit)
        if data: return data
        accepted_params.pop(key, None)

    v2_grans = V2_GRAN_CANDS.get(tf, ["5min","300"])
    v1_grans = V1_GRAN_CANDS.get(tf, ["5min","300","5"])
    for sym in (sym_base + FUT_SUFFIX, sym_base):
        for prod in (None, "umcbl", "UMCBL"):
            for gran in v2_grans:
                data = _try_v2(sym, gran, prod, limit)
                if data:
                    accepted_params[key] = {"endpoint":"v2","symbol":sym,"gran":gran,"productType":prod}
                    print(f"[{sym_base} {tf}] ACCEPT v2: symbol={sym}, gran={gran}, productType={prod}")
                    return data
    for sym in (sym_base + FUT_SUFFIX, sym_base):
        for gran in v1_grans:
            data = _try_v1(sym, gran, limit)
            if data:
                accepted_params[key] = {"endpoint":"v1","symbol":sym,"gran":gran}
                print(f"[{sym_base} {tf}] ACCEPT v1: symbol={sym}, gran={gran}")
                return data

    reason = f"свечи не отдаются для TF={tf}"
    until_ts = time.time() + RECHECK_FAIL_SEC
    disabled_symbols[key] = {"reason": reason, "until_ts": until_ts}
    send_telegram(f"⛔ Отключаю {sym_base}{FUT_SUFFIX} [{tf}] на {RECHECK_FAIL_SEC//60} мин: {reason}")
    raise RuntimeError(f"[{sym_base} {tf}] disabled: {reason}")

def get_closed_ohlcv(sym_base: str, tf: str, limit: int):
    data = fetch_candles_exact(sym_base, tf, limit)
    if not data: return [], [], []
    gran_sec = _GRAN_TO_SEC.get(tf, 300)
    now_ms = int(time.time()*1000)
    closed = [r for r in data if (now_ms - int(r[0])) >= gran_sec*1000]
    if not closed: return [], [], []
    highs = [r[2] for r in closed]
    lows  = [r[3] for r in closed]
    closes= [r[4] for r in closed]
    return highs, lows, closes

# ========= Логика сигналов =========
def analyze_and_alert(sym_base: str):
    # 5m, 15m, 1h
    h5, l5, c5   = get_closed_ohlcv(sym_base, WORK_TF, CANDLES_LIMIT)
    h15, l15, c15= get_closed_ohlcv(sym_base, HTF_TF, CANDLES_LIMIT//2)
    h1h, l1h, c1h= get_closed_ohlcv(sym_base, HTF2_TF, max(200, CANDLES_LIMIT//3))

    last_candles_count[sym_base] = {"5m": len(c5), "15m": len(c15), "1h": len(c1h)}
    if len(c5) < max(EMA_SLOW+5, 60) or len(c15) < max(EMA_SLOW+5, 40) or len(c1h) < 60:
        last_filter_gate[sym_base] = "unknown"
        last_block_reasons[sym_base] = ["недостаточно данных"]
        return

    # EMA/RSI/ATR
    ema9_5, ema21_5   = ema_series(c5, EMA_FAST),  ema_series(c5, EMA_SLOW)
    ema50_5           = ema_series(c5, EMA_DIR_PERIOD)
    ema200_5          = ema_series(c5, EMA_LONG_PERIOD)
    ema9_15, ema21_15 = ema_series(c15, EMA_FAST), ema_series(c15, EMA_SLOW)
    ema9_1h, ema21_1h = ema_series(c1h, EMA_FAST), ema_series(c1h, EMA_SLOW)
    rsi5 = rsi_series(c5, RSI_PERIOD)
    atr5 = atr_series(h5, l5, c5, 14)

    i, j, k = len(c5)-1, len(c15)-1, len(c1h)-1
    if i < 2 or j < 1 or k < 1:
        last_filter_gate[sym_base] = "unknown"
        last_block_reasons[sym_base] = ["недостаточно данных"]
        return

    # Подтверждённый кросс и удержание 2 свечи
    cross_up_prev   = ema9_5[i-2] <= ema21_5[i-2] and ema9_5[i-1] >  ema21_5[i-1]
    cross_down_prev = ema9_5[i-2] >= ema21_5[i-2] and ema9_5[i-1] <  ema21_5[i-1]
    hold_up   = (ema9_5[i] > ema21_5[i]) and (ema9_5[i-1] > ema21_5[i-1])
    hold_down = (ema9_5[i] < ema21_5[i]) and (ema9_5[i-1] < ema21_5[i-1])

    strength_now = abs(ema9_5[i] - ema21_5[i]) / c5[i] >= STRENGTH_PCT
    trend_up   = (ema9_15[j] > ema21_15[j]) and (ema9_1h[k] > ema21_1h[k])
    trend_down = (ema9_15[j] < ema21_15[j]) and (ema9_1h[k] < ema21_1h[k])

    price_above = c5[i] > max(ema9_5[i], ema21_5[i])
    price_below = c5[i] < min(ema9_5[i], ema21_5[i])

    # --- Фильтр направления по EMA50 и EMA200 ---
    ema50_slope_ok  = (not EMA50_NEEDS_SLOPE)  or (ema50_5[i]  >= ema50_5[i-1])
    ema200_slope_ok = (not EMA200_NEEDS_SLOPE) or (ema200_5[i] >= ema200_5[i-1])
    dir_long_ok  = (c5[i] > ema50_5[i] and c5[i] > ema200_5[i] and ema50_slope_ok and ema200_slope_ok)
    dir_short_ok = (c5[i] < ema50_5[i] and c5[i] < ema200_5[i] and ((not EMA50_NEEDS_SLOPE)  or (ema50_5[i]  <= ema50_5[i-1])) and ((not EMA200_NEEDS_SLOPE) or (ema200_5[i] <= ema200_5[i-1])))

    rsi_ok_long  = (rsi5[i] >= RSI_MID) and (rsi5[i] > rsi5[i-1])
    rsi_ok_short = (rsi5[i] <= RSI_MID) and (rsi5[i] < rsi5[i-1])

    # --- ATR-фильтр ---
    entry = c5[i]
    this_atr = atr5[i] if atr5[i] else entry * 0.01
    atr_pct = this_atr / entry if entry > 0 else None
    atr_ok = (atr_pct is not None) and (ATR_MIN_PCT <= atr_pct <= ATR_MAX_PCT)
    last_atr_info[sym_base] = {"atr": this_atr, "atr_pct": atr_pct}

    side_5m = "LONG" if hold_up else ("SHORT" if hold_down else "NEUTRAL")
    last_band_state[sym_base] = side_5m

    prev_gate = last_filter_gate[sym_base]

    allow_long  = (hold_up   and strength_now and trend_up   and price_above and rsi_ok_long  and atr_ok and dir_long_ok)
    allow_short = (hold_down and strength_now and trend_down and price_below and rsi_ok_short and atr_ok and dir_short_ok)
    allow_any = (allow_long or allow_short)
    last_filter_gate[sym_base] = "allow" if allow_any else "block"

    # Причины блокировки
    reasons = []
    if not atr_ok:
        if atr_pct is None:
            reasons.append("ATR недоступен")
        elif atr_pct < ATR_MIN_PCT:
            reasons.append(f"ATR ниже минимума ({ATR_MIN_PCT*100:.2f}%)")
        else:
            reasons.append(f"ATR выше максимума ({ATR_MAX_PCT*100:.2f}%)")
    if not (trend_up or trend_down):
        reasons.append("тренд 15m/1h = FLAT")
    if side_5m == "LONG" and not trend_up:
        reasons.append("конфликт трендов (5m=LONG vs 15m/1h≠UP)")
    if side_5m == "SHORT" and not trend_down:
        reasons.append("конфликт трендов (5m=SHORT vs 15m/1h≠DOWN)")
    if not strength_now:
        reasons.append(f"сила кросса < {STRENGTH_PCT*100:.2f}%")
    if side_5m == "LONG" and not price_above:
        reasons.append("цена не выше EMA")
    if side_5m == "SHORT" and not price_below:
        reasons.append("цена не ниже EMA")
    if side_5m == "LONG" and not rsi_ok_long:
        reasons.append(f"RSI < {RSI_MID} или падает")
    if side_5m == "SHORT" and not rsi_ok_short:
        reasons.append(f"RSI > {RSI_MID} или растёт")
    if side_5m == "LONG" and not (cross_up_prev and hold_up):
        reasons.append("нет подтверждённого кросса EMA ↑")
    if side_5m == "SHORT" and not (cross_down_prev and hold_down):
        reasons.append("нет подтверждённого кросса EMA ↓")
    # EMA50/EMA200 направление
    if side_5m == "LONG" and not dir_long_ok:
        reasons.append("цена ниже EMA50/EMA200 (фильтр направления)")
    if side_5m == "SHORT" and not dir_short_ok:
        reasons.append("цена выше EMA50/EMA200 (фильтр направления)")

    last_block_reasons[sym_base] = sorted(set(reasons)) if not allow_any else []

    # --- ПРЕДСИГНАЛЫ и смена статуса ---
    now = time.time()

    # Ворота стали allow -> «фильтры зелёные»
    if allow_any and prev_gate != "allow" and now - last_setup_time[sym_base] >= SETUP_COOLDOWN_SEC:
        send_telegram(
            f"🟢 {sym_base}{FUT_SUFFIX}: фильтры ЗЕЛЁНЫЕ\n"
            f"5m: {side_5m} • тренды 15m/1h OK • сила ≥ {STRENGTH_PCT*100:.2f}% • "
            f"RSI {(('≥' if side_5m=='LONG' else '≤') + str(RSI_MID))} • "
            f"ATR {(atr_pct*100 if atr_pct is not None else 0):.2f}% в коридоре • EMA50/EMA200 OK"
        )
        last_setup_time[sym_base] = now

    # Сетап: всё ОК, но ждём подтверждённого кросса
    setup_long  = (strength_now and trend_up   and price_above and rsi_ok_long  and atr_ok and dir_long_ok)  and not (cross_up_prev and hold_up)
    setup_short = (strength_now and trend_down and price_below and rsi_ok_short and atr_ok and dir_short_ok) and not (cross_down_prev and hold_down)

    if (setup_long or setup_short) and now - last_setup_time[sym_base] >= SETUP_COOLDOWN_SEC:
        setup_dir = "LONG" if setup_long else "SHORT"
        wait_txt = "ждём подтверждения кросса EMA ↑" if setup_long else "ждём подтверждения кросса EMA ↓"
        send_telegram(
            f"⚡ Возможен вход {setup_dir} по {sym_base}{FUT_SUFFIX}\n"
            f"Цена: {entry:.6f} • 5m: {side_5m}\n"
            f"Тренды 15m/1h: OK • Сила={(abs(ema9_5[i]-ema21_5[i])/entry*100):.2f}% (≥ {STRENGTH_PCT*100:.2f}%)\n"
            f"RSI(14)={rsi5[i]:.1f} • ATR={(atr_pct*100 if atr_pct is not None else 0):.2f}% в коридоре • EMA50/EMA200 OK\n"
            f"⏳ {wait_txt}"
        )
        last_setup_time[sym_base] = now

    # --- Реальные сигналы ---
    tp_dist = 1.5 * this_atr
    sl_dist = 1.0 * this_atr

    # LONG
    if cross_up_prev and allow_long:
        if now - last_alert_time[sym_base] >= ALERT_COOLDOWN_SEC:
            msg = (f"🔔 BUY/LONG {sym_base}{FUT_SUFFIX} (5m подтверждённый)\n"
                   f"Цена: {entry:.6f}\n"
                   f"Тренды: 15m/1h OK • Сила ≥ {STRENGTH_PCT*100:.2f}% • EMA50/EMA200 OK\n"
                   f"Цена выше EMA • RSI≥{RSI_MID}\n"
                   f"ATR={this_atr:.6f} ({atr_pct*100:.2f}%) • Диапазон OK [{ATR_MIN_PCT*100:.2f}–{ATR_MAX_PCT*100:.2f}%]\n"
                   f"TP ≈ {entry+tp_dist:.6f} • SL ≈ {entry-sl_dist:.6f}")
            print(msg); send_telegram(msg)
            last_alert_time[sym_base] = now
        return

    # SHORT
    if cross_down_prev and allow_short:
        if now - last_alert_time[sym_base] >= ALERT_COOLDOWN_SEC:
            msg = (f"🔔 SELL/SHORT {sym_base}{FUT_SUFFIX} (5m подтверждённый)\n"
                   f"Цена: {entry:.6f}\n"
                   f"Тренды: 15m/1h OK • Сила ≥ {STRENGTH_PCT*100:.2f}% • EMA50/EMA200 OK\n"
                   f"Цена ниже EMA • RSI≤{RSI_MID}\n"
                   f"ATR={this_atr:.6f} ({atr_pct*100:.2f}%) • Диапазон OK [{ATR_MIN_PCT*100:.2f}–{ATR_MAX_PCT*100:.2f}%]\n"
                   f"TP ≈ {entry-tp_dist:.6f} • SL ≈ {entry+sl_dist:.6f}")
            print(msg); send_telegram(msg)
            last_alert_time[sym_base] = now
        return

    # Heartbeat
    if now - last_heartbeat_time[sym_base] >= HEARTBEAT_SEC:
        trend_txt = "UP" if trend_up else ("DOWN" if trend_down else "FLAT")
        gate_txt = "✅ Сигналы разрешены (условия совпадают)" if last_filter_gate[sym_base]=="allow" \
                   else ("⛔ Сигналы заблокированы фильтрами" if last_filter_gate[sym_base]=="block"
                         else "ℹ️ Недостаточно данных для фильтров")
        reasons_txt = ""
        if last_block_reasons[sym_base]:
            reasons_txt = "🚫 Причины: " + "; ".join(last_block_reasons[sym_base])
        atr_txt = f"ATR={this_atr:.6f} ({(atr_pct*100 if atr_pct is not None else 0):.2f}%), коридор [{ATR_MIN_PCT*100:.2f}–{ATR_MAX_PCT*100:.2f}%]"
        hb = (f"ℹ️ {sym_base}{FUT_SUFFIX}: новых входов нет.\n"
              f"Сейчас: {side_5m} (5m), цена {entry:.6f}\n"
              f"Тренд 15m/1h: {trend_txt}\n"
              f"{atr_txt}\n{gate_txt}\n{reasons_txt}".rstrip())
        print(hb); send_telegram(hb)
        last_heartbeat_time[sym_base] = now

# ========= Цикл =========
def worker_loop():
    hdr = (f"🤖 Фьючерсный сигнальный бот запущен\n"
           f"Пары: {', '.join(s + FUT_SUFFIX for s in SYMBOLS)}\n"
           f"Входы: TF {WORK_TF} • EMA {EMA_FAST}/{EMA_SLOW} "
           f"(фильтры направления: EMA{EMA_DIR_PERIOD} + EMA{EMA_LONG_PERIOD})\n"
           f"Фильтры тренда: {HTF_TF} и {HTF2_TF}\n"
           f"Мин. сила кросса: {STRENGTH_PCT*100:.2f}%\n"
           f"ATR-коридор: {ATR_MIN_PCT*100:.2f}%–{ATR_MAX_PCT*100:.2f}%\n"
           f"Кулдаун на сигналы: {ALERT_COOLDOWN_SEC//60} мин.\n"
           f"Предсигналы: кулдаун {SETUP_COOLDOWN_SEC//60} мин.")
    print(f"[{now_iso()}] worker started."); send_telegram(hdr)

    while True:
        for base in SYMBOLS:
            try:
                analyze_and_alert(base)
            except Exception as e:
                print(f"[{base}{FUT_SUFFIX}] analyze error: {e}")
            time.sleep(SLEEP_BETWEEN_SYMBOLS)
        time.sleep(LOOP_SLEEP)

# ========= HTTP =========
@app.route("/")
def root():
    return "ok"

@app.route("/status")
def status():
    disabled_view = {
        f"{k[0]}[{k[1]}]": {
            "reason": v["reason"],
            "until_ts": v["until_ts"],
            "until_iso": datetime.fromtimestamp(v["until_ts"], tz=timezone.utc).isoformat()
        } for k, v in disabled_symbols.items()
    }
    status_lines = []
    for b in SYMBOLS:
        band = last_band_state.get(b, 'unknown')
        cnt = last_candles_count[b]
        gate = last_filter_gate[b]
        gate_icon = "✅ allow" if gate=="allow" else ("⛔ block" if gate=="block" else "ℹ️ unknown")
        atr_info = last_atr_info[b]
        atr_pct_view = f"{(atr_info['atr_pct']*100):.2f}%" if atr_info['atr_pct'] is not None else "n/a"
        reasons = last_block_reasons[b]
        reasons_txt = (" | причины: " + "; ".join(reasons)) if reasons else ""
        status_lines.append(
            f"{b}{FUT_SUFFIX}: {band} • candles 5m={cnt['5m']}, 15m={cnt['15m']}, 1h={cnt['1h']} • "
            f"ATR={atr_info['atr'] if atr_info['atr'] is not None else 'n/a'} ({atr_pct_view}) • {gate_icon}{reasons_txt}"
        )

    return jsonify({
        "ok": True,
        "mode": "futures-umcbl",
        "symbols": [s + FUT_SUFFIX for s in SYMBOLS],
        "work_tf": WORK_TF,
        "htf": HTF_TF,
        "htf2": HTF2_TF,
        "ema": {"fast": EMA_FAST, "slow": EMA_SLOW, "dir": EMA_DIR_PERIOD, "long": EMA_LONG_PERIOD},
        "strength_pct": STRENGTH_PCT,
        "atr_min_pct": ATR_MIN_PCT,
        "atr_max_pct": ATR_MAX_PCT,
        "cooldown_sec": ALERT_COOLDOWN_SEC,
        "heartbeat_sec": HEARTBEAT_SEC,
        "setup_cooldown_sec": SETUP_COOLDOWN_SEC,
        "accepted_params": accepted_params,
        "disabled_symbols": disabled_view,
        "time": now_iso(),
        "last_band_state": last_band_state,
        "candles_count": last_candles_count,
        "filter_gate": last_filter_gate,
        "atr_info": last_atr_info,
        "block_reasons": last_block_reasons,
        "status_lines": status_lines,
    })

@app.route("/ping")
def ping():
    ok = send_telegram(f"🧪 Ping от сервера: {now_iso()}")
    return jsonify({"sent": ok, "time": now_iso()})

# --- Вебхук Telegram ---
@app.route("/telegram", methods=["POST", "GET"])
def telegram_webhook():
    if request.method == "GET":
        return "telegram webhook ok", 200
    try:
        upd = request.get_json(force=True, silent=True) or {}
        msg = (upd.get("message") or upd.get("edited_message")) or {}
        text = (msg.get("text") or "").strip()
        if text in ("/start", "/help"):
            send_telegram("✅ Бот запущен. Команды: /status — показать текущее состояние.")
        elif text == "/status":
            lines = []
            for b in SYMBOLS:
                band = last_band_state.get(b, 'unknown')
                cnt  = last_candles_count[b]
                gate = last_filter_gate[b]
                gate_icon = "✅ allow" if gate=="allow" else ("⛔ block" if gate=="block" else "ℹ️ unknown")
                atr_info = last_atr_info[b]
                atr_pct_view = f"{(atr_info['atr_pct']*100):.2f}%" if atr_info['atr_pct'] is not None else "n/a"
                reasons = last_block_reasons[b]
                reasons_txt = (" | причины: " + "; ".join(reasons)) if reasons else ""
                lines.append(
                    f"{b}{FUT_SUFFIX}: {band} • candles 5m={cnt['5m']}, 15m={cnt['15m']}, 1h={cnt['1h']} • "
                    f"ATR={atr_info['atr'] if atr_info['atr'] is not None else 'n/a'} ({atr_pct_view}) • "
                    f"{gate_icon}{reasons_txt}"
                )
            send_telegram("📊 Статус:\n" + "\n".join(lines))
    except Exception as e:
        print(f"[telegram_webhook] error: {e}")
    return "OK", 200

def run():
    th = threading.Thread(target=worker_loop, daemon=True)
    th.start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=False)

if __name__ == "__main__":
    run()
