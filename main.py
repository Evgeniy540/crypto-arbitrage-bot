# -*- coding: utf-8 -*-
# Bitget Spot EMA 7/14 — сигнальный бот (без торговли).
# Шлёт BUY/SELL в Telegram, с антиспамом и health-сервером для Render.

import os
import time
import threading
from datetime import datetime, timezone
from typing import List, Dict, Optional

import requests
from flask import Flask, jsonify

# =========== ТЕЛЕГРАМ ===========
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"

def tg_send(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=10
        )
    except Exception:
        pass

# =========== НАСТРОЙКИ ===========
SYMBOLS = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","TRXUSDT","PEPEUSDT","BGBUSDT"]  # спот Bitget
GRANULARITY = 60            # 1m свечи (в секундах)
CANDLES_LIMIT = 220         # сколько свечей тянуть (для устойчивых EMA)
EMA_FAST = 7
EMA_SLOW = 14
MIN_EDGE = 0.001            # минимальный разрыв |EMA7-EMA14|/price (0.1%) — отсекает слабые кроссы
COOLDOWN_SEC = 60           # не чаще 1 сигнала в минуту на пару
POLL_SEC = 10               # период сканирования

BITGET = "https://api.bitget.com"
HEADERS = {"User-Agent": "bitget-ema-signal-bot/1.0"}

# память: когда и какой сигнал слали по символу
last_signal_time: Dict[str, float] = {s: 0.0 for s in SYMBOLS}
last_state: Dict[str, Optional[int]] = {s: None for s in SYMBOLS}  # 1=fast>slow, -1=fast<slow

# =========== УТИЛИТЫ ===========
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def ema(values: List[float], period: int) -> List[Optional[float]]:
    """EMA c прогревом: первые period-1 = None, затем классическая EMA."""
    n = len(values)
    if n < period: 
        return [None]*n
    k = 2 / (period + 1)
    out: List[Optional[float]] = [None]*(period-1)
    sma = sum(values[:period]) / period
    out.append(sma)
    prev = sma
    for v in values[period:]:
        prev = v*k + prev*(1-k)
        out.append(prev)
    return out

def fetch_candles(symbol: str, gran: int, limit: int = 200):
    """
    Bitget v2 spot candles:
    GET /api/v2/spot/market/candles?symbol=BTCUSDT&granularity=60&limit=200
    Ответ data: [[ts, o, h, l, c, baseVol, quoteVol], ...] (новые -> старые или наоборот)
    """
    url = f"{BITGET}/api/v2/spot/market/candles"
    params = {"symbol": symbol, "granularity": str(gran), "limit": str(limit)}
    r = requests.get(url, params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    rows = data.get("data") or []
    # приводим к возрастающему времени
    rows.sort(key=lambda x: int(x[0]))
    closes = [float(row[4]) for row in rows]
    return closes

def detect_signal(closes: List[float]) -> Optional[str]:
    """Возвращает 'BUY'|'SELL'|None по последнему закрытому бару (пересечение на закрытой свече)."""
    if len(closes) < max(EMA_SLOW, EMA_FAST) + 2:
        return None
    e_fast = ema(closes, EMA_FAST)
    e_slow = ema(closes, EMA_SLOW)
    # две последние закрытые свечи:
    f_prev, s_prev = e_fast[-2], e_slow[-2]
    f_now,  s_now  = e_fast[-1], e_slow[-1]
    if None in (f_prev, s_prev, f_now, s_now):
        return None

    # фильтр "силы" пересечения
    edge = abs(f_now - s_now) / max(closes[-1], 1e-12)
    if edge < MIN_EDGE:
        return None

    crossed_up   = (f_prev <= s_prev) and (f_now > s_now)
    crossed_down = (f_prev >= s_prev) and (f_now < s_now)
    if crossed_up:   return "BUY"
    if crossed_down: return "SELL"
    return None

def process_symbol(symbol: str):
    try:
        closes = fetch_candles(symbol, GRANULARITY, max(CANDLES_LIMIT, EMA_SLOW+50))
    except Exception as e:
        print(f"[{symbol}] fetch error: {e}")
        return

    if not closes:
        return

    signal = detect_signal(closes)
    price = closes[-1]

    # обновляем текущее состояние fast vs slow
    e_fast = ema(closes, EMA_FAST)
    e_slow = ema(closes, EMA_SLOW)
    if e_fast[-1] is None or e_slow[-1] is None:
        return
    state_now = 1 if e_fast[-1] > e_slow[-1] else -1
    last_state[symbol] = state_now

    if not signal:
        return

    # антиспам по времени
    if time.time() - last_signal_time.get(symbol, 0) < COOLDOWN_SEC:
        return

    last_signal_time[symbol] = time.time()

    msg = (
        f"🔔 {signal} {symbol}\n"
        f"Цена: {price:.6f}\n"
        f"EMA{EMA_FAST}/{EMA_SLOW} (TF 1m)\n"
        f"{now_iso()}"
    )
    tg_send(msg)

# =========== ФОНОВЫЙ ЦИКЛ ===========
def worker():
    tg_send(f"🤖 Бот запущен! EMA {EMA_FAST}/{EMA_SLOW}, TF 1m. "
            f"Сигналы — только по новым пересечениям (edge≥{MIN_EDGE*100:.1f}%).")
    while True:
        start = time.time()
        for s in SYMBOLS:
            process_symbol(s)
        # равномерный цикл
        dt = time.time() - start
        time.sleep(max(1.0, POLL_SEC - dt))

# =========== FLASK (health для Render) ===========
app = Flask(__name__)

@app.get("/")
def root():
    return jsonify({
        "ok": True,
        "time": now_iso(),
        "symbols": SYMBOLS,
        "tf": "1m",
        "ema": f"{EMA_FAST}/{EMA_SLOW}",
        "cooldown_sec": COOLDOWN_SEC
    })

# =========== ENTRYPOINT ===========
if __name__ == "__main__":
    # стартуем сканер в фоне
    threading.Thread(target=worker, daemon=True).start()
    # держим порт для Render
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
