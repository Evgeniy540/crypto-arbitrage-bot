# -*- coding: utf-8 -*-
import os
import time
import threading
from datetime import datetime, timezone
from collections import defaultdict, deque

import requests
from flask import Flask, jsonify

# ========= ТВОИ ДАННЫЕ (вписано) =========
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
# ========================================

# -------- Настройки стратегии / опроса --------
FUT_SUFFIX = "_UMCBL"                          # USDT-M Futures у Bitget
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT"]

# TF: можно "1min","3min","5min","15min","30min","1h","4h","6h","12h","1day","1week","1M"
GRANULARITY = "1min"
EMA_FAST, EMA_SLOW = 9, 21
CANDLES_LIMIT = 220

# ==== «уменьшаем процент» для генерации сигналов ====
# Порог близости EMA: если |EMA_fast - EMA_slow| / EMA_slow <= EPS_PCT,
# считаем, что линии "почти пересеклись" → отправляем «near-cross» (мягкий) сигнал.
EPS_PCT = 0.001          # 0.1%  (0.0005 = 0.05%, 0.002 = 0.2%)
NEAR_CROSS_ALERTS = True # включить мягкие сигналы
NEAR_COOLDOWN_SEC = 300  # не чаще одного мягкого сигнала раз в 5 минут по символу
# ====================================================

COOLDOWN_SEC = 60                              # минимальный интервал между ЖЁСТКИМИ сигналами по символу
HEARTBEAT_SEC = 3600                           # «нет нового пересечения» не чаще 1/час
SEND_INITIAL_BIAS = True                       # прислать стартовую сторону после запуска

REQUEST_TIMEOUT = 12
SLEEP_BETWEEN_SYMBOLS = 0.25                   # пауза между монетами
LOOP_SLEEP = 1.5                               # пауза между кругами

BASE_URL = "https://api.bitget.com"
_REQ_HEADERS = {
    "User-Agent": "futures-signal-bot/1.2",
    "Accept": "application/json",
}

# -------- Служебные хранилища --------
last_cross = {}                                   # последняя ЖЁСТКАЯ сторона "BUY"/"SELL"
last_band_state = {}                              # последняя "зона": BUY / NEUTRAL / SELL
last_alert_time = defaultdict(lambda: 0.0)        # антиспам для жёстких сигналов
last_near_time = defaultdict(lambda: 0.0)         # антиспам для мягких сигналов
last_heartbeat_time = defaultdict(lambda: 0.0)    # антиспам для «нет нового пересечения»
cl_buf = defaultdict(lambda: deque(maxlen=CANDLES_LIMIT))  # буфер закрытий по символу

# -------- Flask --------
app = Flask(__name__)

# ================= Утилиты =================
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def send_telegram(text: str) -> bool:
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        r = requests.post(
            url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
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
        k = 2 / (p + 1.0)
        e = float(prices[0])
        for x in prices[1:]:
            e = x * k + e * (1 - k)
        return e

    return ema_full(series, fast), ema_full(series, slow)

# ---- маппинги гранулярностей для Bitget ----
# v2 ожидает секунды, v1 принимает строковый формат
_V2_GRAN_MAP = {
    "1min": "60",
    "3min": "180",
    "5min": "300",
    "15min": "900",
    "30min": "1800",
    "1h": "3600",
    "4h": "14400",
    "6h": "21600",
    "12h": "43200",
    "1day": "86400",
    "1week": "604800",
    "1M": "2592000",
}
def _to_v2_granularity(g: str) -> str:
    return _V2_GRAN_MAP.get(g, "60")  # по умолчанию 1min

# ================= Bitget: чтение свечей (Futures/MIX) =================
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

def bitget_get_futures_candles(symbol_base: str, granularity: str, limit: int):
    """
    Сначала пробуем v2: /api/v2/mix/market/candles  (granularity = секунды)
    Если код != 00000 — откатываемся на v1: /api/mix/v1/market/candles (granularity = "1min"/...)
    """
    symbol = symbol_base + FUT_SUFFIX
    gran_v2 = _to_v2_granularity(granularity)

    # v2
    try:
        r = requests.get(
            f"{BASE_URL}/api/v2/mix/market/candles",
            params={"symbol": symbol, "granularity": gran_v2, "limit": str(limit)},
            headers=_REQ_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        data = r.json()
        code = str(data.get("code"))
        if code == "00000":
            return _parse_ohlcv_payload(data)
        else:
            # Логируем и откатываемся на v1
            print(f"[{symbol}] v2 fail {code}: {data.get('msg')} (gran={gran_v2})")
    except Exception as e:
        print(f"[{symbol}] v2 exception: {e}")

    # v1 (backup)
    try:
        r = requests.get(
            f"{BASE_URL}/api/mix/v1/market/candles",
            params={"symbol": symbol, "granularity": granularity, "limit": str(limit)},
            headers=_REQ_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        data = r.json()
        code = str(data.get("code"))
        if code == "00000":
            return _parse_ohlcv_payload(data)
        raise RuntimeError(f"[{symbol}] v1 fail {code}: {data.get('msg')} (gran={granularity})")
    except Exception as e:
        # пусть поднимем исключение в верх — оно залогируется и цикл продолжится
        raise

# ================= Логика сигналов =================
def analyze_and_alert(sym_base: str, candles):
    # Поддерживаем буфер закрытий
    closes = [c[4] for c in candles]
    for px in closes:
        if not cl_buf[sym_base] or px != cl_buf[sym_base][-1]:
            cl_buf[sym_base].append(px)

    if len(cl_buf[sym_base]) < EMA_SLOW:
        return

    fast, slow = ema_pair(list(cl_buf[sym_base]), EMA_FAST, EMA_SLOW)
    if fast is None or slow is None or slow == 0:
        return

    # Разница в процентах между EMA
    diff_pct = (fast - slow) / slow  # положит. -> BUY, отрицат. -> SELL

    # Определяем "зону": BUY/NEUTRAL/SELL с гистерезисом EPS_PCT
    if diff_pct > EPS_PCT:
        band = "BUY"
    elif diff_pct < -EPS_PCT:
        band = "SELL"
    else:
        band = "NEUTRAL"

    prev_band = last_band_state.get(sym_base)
    prev_hard = last_cross.get(sym_base)

    # 1) Стартовый статус один раз
    if prev_band is None and SEND_INITIAL_BIAS and band in ("BUY", "SELL"):
        price = candles[-1][4]
        side = "LONG (лонг)" if band == "BUY" else "SHORT (шорт)"
        msg = (f"✅ Стартовый статус {sym_base}{FUT_SUFFIX}\n"
               f"Идея: {side}\n"
               f"Цена: {price:.6f}\n"
               f"EMA {EMA_FAST}/{EMA_SLOW} • TF {GRANULARITY}\n"
               f"Δ={diff_pct*100:.3f}% (порог {EPS_PCT*100:.2f}%)")
        print(msg); send_telegram(msg)

    # 2) МЯГКИЙ сигнал (near-cross) при входе в нейтральную зону рядом с противоположной стороной
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

    # 3) ЖЁСТКИЙ сигнал при выходе из нейтральной зоны в противоположную сторону
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

    # 4) Heartbeat раз в час
    hb_now = time.time()
    if band and hb_now - last_heartbeat_time[sym_base] >= HEARTBEAT_SEC:
        price = candles[-1][4]
        side = {"BUY": "LONG", "SELL": "SHORT", "NEUTRAL": "NEUTRAL"}[band]
        hb = (f"ℹ️ {sym_base}{FUT_SUFFIX}: новых пересечений нет. Сейчас {side}, "
              f"цена {price:.6f}. Δ={diff_pct*100:.3f}% (порог {EPS_PCT*100:.2f}%), TF {GRANULARITY}, EMA {EMA_FAST}/{EMA_SLOW}.")
        print(hb); send_telegram(hb)
        last_heartbeat_time[sym_base] = hb_now

    last_band_state[sym_base] = band

# ================= Рабочий цикл =================
def worker_loop():
    hdr = (f"🤖 Фьючерсный сигнальный бот запущен\n"
           f"Пары: {', '.join(s + FUT_SUFFIX for s in SYMBOLS)}\n"
           f"TF: {GRANULARITY} • EMA {EMA_FAST}/{EMA_SLOW}\n"
           f"«Near-cross» порог: ±{EPS_PCT*100:.2f}% (cooldown {NEAR_COOLDOWN_SEC}s)\n"
           f"Жёсткие сигналы только при смене стороны (cooldown {COOLDOWN_SEC}s).")
    print(f"[{now_iso()}] worker started.")
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

# ================= HTTP keep-alive & сервисные маршруты =================
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
