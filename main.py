# -*- coding: utf-8 -*-
import os
import time
import threading
from datetime import datetime, timezone
from collections import defaultdict, deque

import requests
from flask import Flask, jsonify

# =============== ТВОИ ДАННЫЕ (вписано) ===============
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
# =====================================================

# -------- Настройки стратегии --------
# USDT-M perpetual на Bitget => суффикс _UMCBL
FUT_SUFFIX = "_UMCBL"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT"]  # базовые без суффикса
GRANULARITY = "1min"        # допустимые для futures: 1min,3min,5min,15min,30min,1h,4h,6h,12h,1day,1week,1M,
                            # также 6Hutc,12Hutc,1Dutc,3Dutc,1Wutc,1Mutc
EMA_FAST, EMA_SLOW = 7, 14
CANDLES_LIMIT = 220
COOLDOWN_SEC = 60           # антиспам на один символ
REQUEST_TIMEOUT = 12
SLEEP_BETWEEN_SYMBOLS = 0.5
LOOP_SLEEP = 3

# -------- Служебные хранилища --------
last_cross = {}                                   # "BUY"/"SELL"/None
last_alert_time = defaultdict(lambda: 0.0)        # антиспам
cl_buf = defaultdict(lambda: deque(maxlen=CANDLES_LIMIT))  # закрытия для EMA

BASE_URL = "https://api.bitget.com"
app = Flask(__name__)

# ================= Утилиты =================
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def send_telegram(text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        print(f"[TG] send error: {e}")

# ================= Доступ к Bitget (Futures/MIX) =================
def _parse_v2(data):
    rows = data.get("data", [])
    out = []
    for row in rows:
        ts = int(float(row[0]))
        o = float(row[1]); h = float(row[2]); l = float(row[3]); c = float(row[4])
        v = float(row[5]) if len(row) > 5 else 0.0
        out.append([ts, o, h, l, c, v])
    out.sort(key=lambda x: x[0])
    return out

def _parse_v1(data):
    rows = data.get("data", [])
    out = []
    for row in rows:
        ts = int(float(row[0]))
        o = float(row[1]); h = float(row[2]); l = float(row[3]); c = float(row[4])
        v = float(row[5]) if len(row) > 5 else 0.0
        out.append([ts, o, h, l, c, v])
    out.sort(key=lambda x: x[0])
    return out

def bitget_get_futures_candles(symbol_base: str, granularity: str, limit: int):
    """
    Пытаемся получить свечи фьючерсов (USDT-M):
      1) v2: /api/v2/mix/market/candles?symbol=BTCUSDT_UMCBL
      2) v1: /api/mix/v1/market/candles?symbol=BTCUSDT_UMCBL
    Возвращаем [[ts_ms,o,h,l,c,v], ...] по возрастанию ts.
    """
    symbol = symbol_base + FUT_SUFFIX

    # --- v2 ---
    try:
        url = f"{BASE_URL}/api/v2/mix/market/candles"
        params = {"symbol": symbol, "granularity": granularity, "limit": str(limit)}
        r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        data = r.json()
        if str(data.get("code")) == "00000":
            return _parse_v2(data)
        else:
            code = str(data.get("code")); msg = data.get("msg")
            # если ошибка о неверной гранулярности
            if code in {"400171", "400170"}:
                raise RuntimeError(f"Invalid granularity for futures: {msg}")
            # в остальных случаях попробуем v1
            # print(f"[{symbol}] v2 fail {code}: {msg}")
    except Exception:
        pass

    # --- v1 (бэкап) ---
    url = f"{BASE_URL}/api/mix/v1/market/candles"
    params = {"symbol": symbol, "granularity": granularity, "limit": str(limit)}
    r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    data = r.json()
    if str(data.get("code")) == "00000":
        return _parse_v1(data)
    code = str(data.get("code")); msg = data.get("msg")
    raise RuntimeError(f"futures candles fail: {code} {msg}")

# ================= EMA =================
def ema_pair(series, fast, slow):
    if len(series) < slow:
        return None, None
    def ema_full(prices, p):
        k = 2/(p+1)
        e = prices[0]
        for x in prices[1:]:
            e = x*k + e*(1-k)
        return e
    return ema_full(series, fast), ema_full(series, slow)

# ================= Логика сигналов =================
def analyze_and_alert(sym_base: str, candles):
    closes = [c[4] for c in candles]
    for px in closes:
        if not cl_buf[sym_base] or px != cl_buf[sym_base][-1]:
            cl_buf[sym_base].append(px)

    if len(cl_buf[sym_base]) < EMA_SLOW:
        return

    fast, slow = ema_pair(list(cl_buf[sym_base]), EMA_FAST, EMA_SLOW)
    if fast is None or slow is None:
        return

    prev_state = last_cross.get(sym_base)
    state = "BUY" if fast > slow else "SELL" if fast < slow else prev_state

    if state and state != prev_state:
        last_ts = candles[-1][0]
        ts_iso = datetime.fromtimestamp(last_ts/1000, tz=timezone.utc).isoformat()
        price = candles[-1][4]

        tnow = time.time()
        if tnow - last_alert_time[sym_base] >= COOLDOWN_SEC:
            side = "LONG (покупать/открывать лонг)" if state == "BUY" else "SHORT (продавать/открывать шорт)"
            text = (
                f"🔔 {state} {sym_base}{FUT_SUFFIX}\n"
                f"Режим: Futures USDT-M\n"
                f"Идея: {side}\n"
                f"Цена: {price:.6f}\n"
                f"EMA {EMA_FAST}/{EMA_SLOW} (TF {GRANULARITY})\n"
                f"{ts_iso}"
            )
            print(text)
            send_telegram(text)
            last_alert_time[sym_base] = tnow

    last_cross[sym_base] = state

# ================= Рабочий цикл =================
def worker_loop():
    hdr = (f"🤖 Фьючерсный сигнальный бот запущен! "
           f"EMA {EMA_FAST}/{EMA_SLOW}, TF {GRANULARITY}. "
           f"Сообщения — только при новых пересечениях.")
    print(f"[{now_iso()}] worker started. Futures symbols={SYMBOLS}, TF={GRANULARITY}")
    send_telegram(hdr)

    while True:
        for base in SYMBOLS:
            try:
                candles = bitget_get_futures_candles(base, GRANULARITY, CANDLES_LIMIT)
                analyze_and_alert(base, candles)
            except Exception as e:
                print(f"[{base}{FUT_SUFFIX}] fetch/analyze error: {e}")
            time.sleep(SLEEP_BETWEEN_SYMBOLS)
        time.sleep(LOOP_SLEEP)

# ================= HTTP keep-alive =================
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
        "ema": f"{EMA_FAST}/{EMA_SLOW}",
        "cooldown_sec": COOLDOWN_SEC,
        "time": now_iso()
    })

def run():
    th = threading.Thread(target=worker_loop, daemon=True)
    th.start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    run()
