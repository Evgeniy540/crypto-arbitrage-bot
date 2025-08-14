import os
import time
import math
import json
import threading
from datetime import datetime, timezone
from typing import List, Dict

import requests
from flask import Flask, jsonify

# ==========[  НАСТРОЙКИ  ]==========
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"

# Монеты Bybit Spot (можешь менять)
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT", "PEPEUSDT", "BGBUSDT"]

# Таймфрейм и EMA
INTERVAL      = "1"          # 1 = 1 minute (Bybit v5)
EMA_FAST_LEN  = 7
EMA_SLOW_LEN  = 14

# Ограничения и поведение
POLL_SECONDS         = 8       # как часто опрашивать
MIN_CANDLES_REQUIRED = 120     # сколько свечей тянуть (EMA, фильтры)
SEND_ONLY_ON_CROSS   = True    # сигнал только при новом пересечении
MIN_SLOPE_ABS        = 0.0     # фильтр: минимальный наклон EMA(fast) (0 = выключить)
# ====================================


# ---- Telegram ----
def tg_send(text: str) -> None:
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
        for _ in range(2):
            r = requests.post(url, json=payload, timeout=10)
            if r.ok:
                return
            time.sleep(1)
    except Exception:
        pass


# ---- Bybit Market Data (v5) ----
BYBIT_BASE = "https://api.bybit.com"

def get_klines(symbol: str, interval: str = "1", limit: int = 200) -> List[Dict]:
    """
    Bybit v5 Kline:
    GET /v5/market/kline?category=spot&symbol=BTCUSDT&interval=1&limit=200
    Возвращает список свечей в хронологическом порядке (старые -> новые)
    """
    params = {
        "category": "spot",
        "symbol": symbol,
        "interval": interval,
        "limit": str(limit),
    }
    url = f"{BYBIT_BASE}/v5/market/kline"
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    d = r.json()
    if d.get("retCode") != 0:
        raise RuntimeError(f"Bybit error: {d.get('retMsg')}")
    # d['result']['list'] — массив свечей в ОБРАТНОМ порядке: newest first
    raw = d["result"]["list"]
    raw.reverse()  # теперь старые -> новые

    kl = []
    for it in raw:
        # формат: [startTime, open, high, low, close, volume, turnover]
        ts_ms = int(it[0])
        kl.append({
            "time": datetime.fromtimestamp(ts_ms/1000, tz=timezone.utc),
            "open": float(it[1]),
            "high": float(it[2]),
            "low":  float(it[3]),
            "close":float(it[4]),
            "vol":  float(it[5]),
        })
    return kl


# ---- Индикаторы ----
def ema(series: List[float], length: int) -> List[float]:
    if length <= 1 or len(series) == 0:
        return series[:]
    k = 2 / (length + 1)
    out = [series[0]]
    for i in range(1, len(series)):
        out.append(series[i] * k + out[-1] * (1 - k))
    return out

def slope(values: List[float], n: int = 3) -> float:
    """Простой наклон последних n значений."""
    if len(values) < 2:
        return 0.0
    n = min(n, len(values) - 1)
    return values[-1] - values[-1 - n]


# ---- Логика сигналов ----
last_cross_state: Dict[str, int] = {}   # 1 = fast>slow, -1 = fast<slow
last_signaled_candle_time: Dict[str, datetime] = {}

def build_signal_text(side: str, symbol: str, price: float) -> str:
    now = datetime.now(timezone.utc).isoformat()
    bell = "🔔"
    side_txt = "BUY" if side == "BUY" else "SELL"
    return (
        f"{bell} {side_txt} {symbol}\n"
        f"Цена: {price:.6f}\n"
        f"EMA{EMA_FAST_LEN} vs EMA{EMA_SLOW_LEN} (TF {INTERVAL}m)\n"
        f"{now}"
    )

def process_symbol(symbol: str):
    try:
        kl = get_klines(symbol, INTERVAL, max(MIN_CANDLES_REQUIRED, 50))
        if not kl:
            return

        closes = [x["close"] for x in kl]
        ef = ema(closes, EMA_FAST_LEN)
        es = ema(closes, EMA_SLOW_LEN)

        # текущее состояние
        fast = ef[-1]
        slow = es[-1]
        prev_fast = ef[-2] if len(ef) > 1 else fast
        prev_slow = es[-2] if len(es) > 1 else slow

        # отметка времени последней полной свечи
        # в Bybit kline последняя запись — текущая формирующаяся свеча.
        # Будем сигналить только когда сменился "время начала" текущей свечи,
        # а пересечение было на закрытой.
        last_closed_time = kl[-2]["time"] if len(kl) >= 2 else kl[-1]["time"]

        # фильтры
        ef_slope = slope(ef, 3)
        if abs(ef_slope) < MIN_SLOPE_ABS:
            return

        # состояние: 1 если fast>slow, -1 если fast<slow
        state_now = 1 if fast > slow else -1
        state_prev = 1 if prev_fast > prev_slow else -1

        sym_key = symbol.upper()
        prev_state_recorded = last_cross_state.get(sym_key, 0)
        last_candle_sent = last_signaled_candle_time.get(sym_key)

        crossed_up = (state_prev == -1) and (state_now == 1)
        crossed_dn = (state_prev == 1) and (state_now == -1)

        if SEND_ONLY_ON_CROSS:
            should_buy  = crossed_up
            should_sell = crossed_dn
        else:
            should_buy  = state_now == 1 and prev_state_recorded != 1
            should_sell = state_now == -1 and prev_state_recorded != -1

        # чтобы не слать множество сообщений в рамках одной и той же закрытой свечи:
        if last_candle_sent is not None and last_candle_sent == last_closed_time:
            # уже слали по этой свече
            pass
        else:
            price = closes[-1]
            if should_buy:
                tg_send(build_signal_text("BUY", sym_key, price))
                last_signaled_candle_time[sym_key] = last_closed_time
            elif should_sell:
                tg_send(build_signal_text("SELL", sym_key, price))
                last_signaled_candle_time[sym_key] = last_closed_time

        # обновляем «память» состояния
        last_cross_state[sym_key] = state_now

    except Exception as e:
        # тихий self-heal: просто пропускаем круг
        # но раз в несколько минут было бы полезно слать предупр. сообщение — не спамим.
        print(f"[WARN] {symbol} error: {e}")


def worker_loop():
    tg_send("🤖 Бот запущен! EMA {}/{}, TF {}m. Сообщения — только по факту новых пересечений."
            .format(EMA_FAST_LEN, EMA_SLOW_LEN, INTERVAL))
    while True:
        start = time.time()
        for sym in SYMBOLS:
            process_symbol(sym)
        # равномерный цикл
        dt = time.time() - start
        time.sleep(max(1.0, POLL_SECONDS - dt))


# ---- Flask (Render health + порт) ----
app = Flask(__name__)

@app.route("/")
def root():
    return jsonify({"ok": True, "time": datetime.now(timezone.utc).isoformat()})

@app.route("/healthz")
def healthz():
    return "ok", 200


# ---- Точка входа ----
if __name__ == "__main__":
    # Фоновый поток с сигналами
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()

    # Веб-сервер для Render (важно: слушаем PORT и 0.0.0.0)
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
