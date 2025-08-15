# -*- coding: utf-8 -*-
import os
import time
import threading
from datetime import datetime, timezone
from collections import defaultdict, deque

import requests
from flask import Flask, jsonify

# ========= ТВОИ ДАННЫЕ =========
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
# ===============================

# -------- Настройки --------
FUT_SUFFIX = "_UMCBL"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT"]

GRANULARITY = "1min"           # желаемый ТФ; код подберёт правильный формат сам
EMA_FAST, EMA_SLOW = 9, 21
CANDLES_LIMIT = 220

# Near-cross (уменьшаем «процент»)
EPS_PCT = 0.001          # 0.1%
NEAR_CROSS_ALERTS = True
NEAR_COOLDOWN_SEC = 300

COOLDOWN_SEC = 60
HEARTBEAT_SEC = 3600
SEND_INITIAL_BIAS = True

REQUEST_TIMEOUT = 12
SLEEP_BETWEEN_SYMBOLS = 0.25
LOOP_SLEEP = 1.5

BASE_URL = "https://api.bitget.com"
_REQ_HEADERS = {"User-Agent": "futures-signal-bot/1.3", "Accept": "application/json"}

# -------- Служебные --------
last_cross = {}
last_band_state = {}
last_alert_time = defaultdict(lambda: 0.0)
last_near_time = defaultdict(lambda: 0.0)
last_heartbeat_time = defaultdict(lambda: 0.0)
cl_buf = defaultdict(lambda: deque(maxlen=CANDLES_LIMIT))

app = Flask(__name__)

# ========= Утилиты =========
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

def ema_pair(series, fast, slow):
    if len(series) < slow:
        return None, None
    def ema_full(prices, p):
        k = 2/(p+1.0)
        e = float(prices[0])
        for x in prices[1:]:
            e = x*k + e*(1-k)
        return e
    return ema_full(series, fast), ema_full(series, slow)

# ========= Bitget =========
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

def _try_mix_v2(symbol_full: str, gran_candidates, limit: int):
    """Перебираем форматы granularity для v2 до успеха."""
    url = f"{BASE_URL}/api/v2/mix/market/candles"
    for gran in gran_candidates:
        try:
            r = requests.get(
                url,
                params={"symbol": symbol_full, "granularity": gran, "limit": limit},
                headers=_REQ_HEADERS, timeout=REQUEST_TIMEOUT,
            )
            data = r.json()
            code = str(data.get("code"))
            if code == "00000":
                if gran != gran_candidates[0]:
                    print(f"[{symbol_full}] v2 accepted gran='{gran}'")
                return _parse_ohlcv_payload(data)
            else:
                print(f"[{symbol_full}] v2 fail {code}: {data.get('msg')} (gran={gran})")
        except Exception as e:
            print(f"[{symbol_full}] v2 exception (gran={gran}): {e}")
    return None

def _try_mix_v1(symbol_full: str, gran_candidates, limit: int):
    """Перебираем форматы granularity для v1 до успеха."""
    url = f"{BASE_URL}/api/mix/v1/market/candles"
    for gran in gran_candidates:
        try:
            r = requests.get(
                url,
                params={"symbol": symbol_full, "granularity": gran, "limit": limit},
                headers=_REQ_HEADERS, timeout=REQUEST_TIMEOUT,
            )
            data = r.json()
            code = str(data.get("code"))
            if code == "00000":
                if gran != gran_candidates[0]:
                    print(f"[{symbol_full}] v1 accepted gran='{gran}'")
                return _parse_ohlcv_payload(data)
            else:
                print(f"[{symbol_full}] v1 fail {code}: {data.get('msg')} (gran={gran})")
        except Exception as e:
            print(f"[{symbol_full}] v1 exception (gran={gran}): {e}")
    return None

# Наборы кандидатов. Видел инсталляции Bitget, где:
# - v2 принимает "1min" ИЛИ "60";
# - v1 принимает "1min" ИЛИ "60" ИЛИ "1".
V2_GRAN_CANDIDATES_MAP = {
    "1min": ["1min", "60"],
    "3min": ["3min", "180"],
    "5min": ["5min", "300"],
    "15min": ["15min", "900"],
    "30min": ["30min", "1800"],
    "1h": ["1h", "3600"],
    "4h": ["4h", "14400"],
    "6h": ["6h", "21600"],
    "12h": ["12h", "43200"],
    "1day": ["1day", "86400"],
    "1week": ["1week", "604800"],
    "1M": ["1M", "2592000"],
}
V1_GRAN_CANDIDATES_MAP = {
    "1min": ["1min", "60", "1"],
    "3min": ["3min", "180", "3"],
    "5min": ["5min", "300", "5"],
    "15min": ["15min", "900", "15"],
    "30min": ["30min", "1800", "30"],
    "1h": ["1h", "3600", "60"],
    "4h": ["4h", "14400", "240"],
    "6h": ["6h", "21600", "360"],
    "12h": ["12h", "43200", "720"],
    "1day": ["1day", "86400", "1D"],
    "1week": ["1week", "604800", "1W"],
    "1M": ["1M", "2592000", "1M"],
}

def bitget_get_futures_candles(symbol_base: str, granularity: str, limit: int):
    symbol = symbol_base + FUT_SUFFIX
    v2_cands = V2_GRAN_CANDIDATES_MAP.get(granularity, ["1min", "60"])
    v1_cands = V1_GRAN_CANDIDATES_MAP.get(granularity, ["1min", "60", "1"])

    # 1) v2 с подбором формата
    data = _try_mix_v2(symbol, v2_cands, limit)
    if data is not None:
        return data

    # 2) v1 с подбором формата
    data = _try_mix_v1(symbol, v1_cands, limit)
    if data is not None:
        return data

    # 3) не удалось
    raise RuntimeError(f"[{symbol}] candles fail for all formats: v2{v2_cands} / v1{v1_cands}")

# ========= Логика сигналов =========
def analyze_and_alert(sym_base: str, candles):
    closes = [c[4] for c in candles]
    for px in closes:
        if not cl_buf[sym_base] or px != cl_buf[sym_base][-1]:
            cl_buf[sym_base].append(px)

    if len(cl_buf[sym_base]) < EMA_SLOW:
        return

    fast, slow = ema_pair(list(cl_buf[sym_base]), EMA_FAST, EMA_SLOW)
    if fast is None or slow is None or slow == 0:
        return

    diff_pct = (fast - slow) / slow
    if diff_pct > EPS_PCT:
        band = "BUY"
    elif diff_pct < -EPS_PCT:
        band = "SELL"
    else:
        band = "NEUTRAL"

    prev_band = last_band_state.get(sym_base)

    # стартовый статус
    if prev_band is None and SEND_INITIAL_BIAS and band in ("BUY", "SELL"):
        price = candles[-1][4]
        side = "LONG (лонг)" if band == "BUY" else "SHORT (шорт)"
        msg = (f"✅ Стартовый статус {sym_base}{FUT_SUFFIX}\n"
               f"Идея: {side}\n"
               f"Цена: {price:.6f}\n"
               f"EMA {EMA_FAST}/{EMA_SLOW} • TF {GRANULARITY}\n"
               f"Δ={diff_pct*100:.3f}% (порог {EPS_PCT*100:.2f}%)")
        print(msg); send_telegram(msg)

    # мягкий сигнал
    if NEAR_CROSS_ALERTS and band == "NEUTRAL" and prev_band in ("BUY", "SELL"):
        tnow = time.time()
        if tnow - last_near_time[sym_base] >= NEAR_COOLDOWN_SEC:
            price = candles[-1][4]
            toward = "SELL/SHORT" if prev_band == "BUY" else "BUY/LONG"
            msg = (f"🟡 {sym_base}{FUT_SUFFIX}: близко к пересечению → возможен {toward}\n"
                   f"Цена: {price:.6f}\n"
                   f"Δ={diff_pct*100:.3f}% (порог {EPS_PCT*100:.2f}%) • TF {GRANULARITY} • EMA {EMA_FAST}/{EMA_SLOW}")
            print(msg); send_telegram(msg)
            last_near_time[sym_base] = tnow

    # жёсткий сигнал
    if prev_band is not None and prev_band != band and band in ("BUY", "SELL"):
        tnow = time.time()
        if tnow - last_alert_time[sym_base] >= COOLDOWN_SEC:
            price = candles[-1][4]
            side = "LONG (покупать/открывать лонг)" if band == "BUY" else "SHORT (продавать/открывать шорт)"
            msg = (f"🔔 {band} {sym_base}{FUT_SUFFIX}\n"
                   f"Идея: {side}\n"
                   f"Цена: {price:.6f}\n"
                   f"EMA {EMA_FAST}/{EMA_SLOW} • TF {GRANULARITY}\n"
                   f"Δ={diff_pct*100:.3f}% (порог {EPS_PCT*100:.2f}%)")
            print(msg); send_telegram(msg)
            last_alert_time[sym_base] = tnow
            last_cross[sym_base] = band

    # heartbeat
    hb_now = time.time()
    if band and hb_now - last_heartbeat_time[sym_base] >= HEARTBEAT_SEC:
        price = candles[-1][4]
        side = {"BUY":"LONG","SELL":"SHORT","NEUTRAL":"NEUTRAL"}[band]
        hb = (f"ℹ️ {sym_base}{FUT_SUFFIX}: новых пересечений нет. Сейчас {side}, "
              f"цена {price:.6f}. Δ={diff_pct*100:.3f}% (порог {EPS_PCT*100:.2f}%), TF {GRANULARITY}, EMA {EMA_FAST}/{EMA_SLOW}.")
        print(hb); send_telegram(hb)
        last_heartbeat_time[sym_base] = hb_now

    last_band_state[sym_base] = band

# ========= Цикл =========
def worker_loop():
    hdr = (f"🤖 Фьючерсный сигнальный бот запущен\n"
           f"Пары: {', '.join(s + FUT_SUFFIX for s in SYMBOLS)}\n"
           f"TF: {GRANULARITY} • EMA {EMA_FAST}/{EMA_SLOW}\n"
           f"«Near-cross» порог: ±{EPS_PCT*100:.2f}% (cooldown {NEAR_COOLDOWN_SEC}s)\n"
           f"Жёсткие сигналы только при смене стороны (cooldown {COOLDOWN_SEC}s).")
    print(f"[{now_iso()}] worker started."); send_telegram(hdr)

    while True:
        for base in SYMBOLS:
            try:
                candles = bitget_get_futures_candles(base, GRANULARITY, CANDLES_LIMIT)
                analyze_and_alert(base, candles)
            except Exception as e:
                print(f"[{base}{FUT_SUFFIX}] fetch/analyze error: {e}")
            time.sleep(SLEEP_BETWEEN_SYMBOLS)
        time.sleep(LOOP_SLEEP)

# ========= HTTP =========
@app.route("/")
def root():
    return "ok"

@app.route("/status")
def status():
    return jsonify({
        "ok": True,
        "mode": "futures-umcbl",
        "symbols": [s + FUT_SUFFIX for s in SYMBOLS],
        "tf": GRANULARITY,
        "ema": {"fast": EMA_FAST, "slow": EMA_SLOW},
        "eps_pct": EPS_PCT,
        "near_cross_alerts": NEAR_CROSS_ALERTS,
        "cooldown_sec": COOLDOWN_SEC,
        "near_cooldown_sec": NEAR_COOLDOWN_SEC,
        "heartbeat_sec": HEARTBEAT_SEC,
        "send_initial_bias": SEND_INITIAL_BIAS,
        "time": now_iso(),
        "last_cross": last_cross,
        "last_band_state": last_band_state,
    })

@app.route("/ping")
def ping():
    ok = send_telegram(f"🧪 Ping от сервера: {now_iso()}")
    return jsonify({"sent": ok, "time": now_iso()})

def run():
    th = threading.Thread(target=worker_loop, daemon=True)
    th.start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    run()
