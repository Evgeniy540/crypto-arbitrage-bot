# -*- coding: utf-8 -*-
# Сигнальный бот (без торговли): Bitget Spot, EMA 9/21, фильтр импульса, TP/SL в сообщении.

import time
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple

import requests
import ccxt  # pip install ccxt

# ========= ТВОЙ TELEGRAM (уже вставлено) =========
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
# =================================================

# Пары (формат ccxt для Bitget Spot)
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "TRX/USDT", "PEPE/USDT", "BGB/USDT"]

# Таймфреймы и параметры
TF = "1m"                 # рабочие свечи
EMA_FAST = 9
EMA_SLOW = 21
CANDLES_LIMIT = 150       # сколько свечей грузим
MIN_READY = 40            # минимум истории, чтобы начать слать сигналы

# Фильтры "не в молоко"
LOOKBACK_CROSS = 3        # пересечение должно случиться в последних N свечах
MOMENTUM_LOOKBACK = 3     # смотрим импульс за N свечей
MOMENTUM_THRESHOLD = 0.002  # 0.2% минимум: + для BUY, - для SELL
COOLDOWN_MINUTES = 5        # не слать один и тот же сигнал чаще, чем раз в N минут

# TP/SL (план сделки в сигнале)
ATR_LEN = 14
ATR_MULT_SL = 1.8          # SL = ATR * 1.8
RR_TARGET = 1.8            # TP по RR (примерно 1.8R)

POLL_SECONDS = 10          # пауза между обходами

# ============ Утилиты ============
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def tg_send(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception:
        pass

def ema(vals: List[float], period: int) -> List[float]:
    if len(vals) < period: return []
    k = 2 / (period + 1)
    out = [sum(vals[:period]) / period]
    for v in vals[period:]:
        out.append(out[-1] + k * (v - out[-1]))
    return [None]*(period-1) + out

def pct_change(a: float, b: float) -> float:
    return 0.0 if b == 0 else (a - b) / b

def true_range(h: List[float], l: List[float], c: List[float]) -> List[Optional[float]]:
    tr = [None]
    for i in range(1, len(c)):
        tr.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    return tr

def atr(h: List[float], l: List[float], c: List[float], length=14) -> List[Optional[float]]:
    tr = true_range(h,l,c)
    seq = [x for x in tr if x is not None]
    if len(seq) < length: return [None]*len(c)
    # RMA
    first = sum(seq[:length]) / length
    out = [first]
    for v in seq[length:]:
        out.append((out[-1]*(length-1) + v) / length)
    return [None]*(len(c)-len(out)) + out

# ============ Данные биржи ============
ex = ccxt.bitget({"enableRateLimit": True, "options": {"defaultType": "spot"}})
ex.load_markets()

def fetch_ohlcv(symbol: str, tf: str, limit: int):
    return ex.fetch_ohlcv(symbol, timeframe=tf, limit=limit)

# ============ Логика сигналов ============
last_side: Dict[str, Optional[str]] = {s: None for s in SYMBOLS}   # 'long'/'short'/None
last_ts:   Dict[str, float] = {s: 0.0 for s in SYMBOLS}            # время последнего сигнала

def can_alert(symbol: str, side: str) -> bool:
    # антиспам: если тот же side недавно — молчим
    same = (last_side.get(symbol) == side)
    recent = (time.time() - last_ts.get(symbol, 0)) < COOLDOWN_MINUTES*60
    return not (same and recent)

def detect_signal_for_symbol(symbol: str) -> Optional[Dict]:
    # OHLCV: [ts, open, high, low, close, volume]
    try:
        o = fetch_ohlcv(symbol, TF, CANDLES_LIMIT)
    except Exception as e:
        print(f"{symbol} fetch err:", e)
        return None
    if not o or len(o) < max(MIN_READY, EMA_SLOW+5): return None

    high = [x[2] for x in o]
    low  = [x[3] for x in o]
    close= [x[4] for x in o]

    e9  = ema(close, EMA_FAST)
    e21 = ema(close, EMA_SLOW)
    if not e9 or not e21: return None

    # пересечение в пределах последних LOOKBACK_CROSS свечей
    e9_now, e21_now = e9[-1], e21[-1]
    cross_up = False
    cross_dn = False
    for i in range(1, LOOKBACK_CROSS+1):
        if len(e9) - 1 - i < 0: break
        if e9[-1-i] is None or e21[-1-i] is None: continue
        if e9[-1-i] <= e21[-1-i] and e9_now > e21_now: cross_up = True
        if e9[-1-i] >= e21[-1-i] and e9_now < e21_now: cross_dn = True

    if not cross_up and not cross_dn:
        return None

    # импульс за последние MOMENTUM_LOOKBACK свечей
    if len(close) <= MOMENTUM_LOOKBACK: return None
    mom = pct_change(close[-1], close[-1 - MOMENTUM_LOOKBACK])

    side = None
    if cross_up and mom >= MOMENTUM_THRESHOLD:
        side = "long"
    elif cross_dn and mom <= -MOMENTUM_THRESHOLD:
        side = "short"
    else:
        return None  # импульс слабый — пропускаем

    if not can_alert(symbol, side):
        return None

    # ATR и план TP/SL
    atr_vals = atr(high, low, close, ATR_LEN)
    if not atr_vals or atr_vals[-1] is None or atr_vals[-1] <= 0:
        return None
    a = atr_vals[-1]
    entry = close[-1]
    if side == "long":
        sl = max(1e-10, entry - ATR_MULT_SL * a)
        tp = entry + RR_TARGET * (entry - sl)
    else:
        sl = entry + ATR_MULT_SL * a
        tp = entry - RR_TARGET * (sl - entry)

    return {
        "symbol": symbol,
        "side": side,
        "price": entry,
        "sl": sl,
        "tp": tp,
        "mom": mom,
        "atr": a
    }

def format_signal(sig: Dict) -> str:
    arrow = "🟢 LONG" if sig["side"] == "long" else "🔴 SHORT"
    sym = sig["symbol"].replace("/", "")
    return (
        f"🔔 {arrow} {sym}\n"
        f"Цена: {sig['price']:.6f}\n"
        f"Импульс: {sig['mom']*100:.2f}%\n"
        f"SL (ATR×{ATR_MULT_SL}): {sig['sl']:.6f}\n"
        f"TP (RR≈{RR_TARGET}): {sig['tp']:.6f}\n"
        f"{now_iso()}"
    )

def main():
    tg_send(f"📡 Сигнальный бот запущен. EMA {EMA_FAST}/{EMA_SLOW}, TF={TF}, "
            f"импульс ≥ {MOMENTUM_THRESHOLD*100:.1f}%, RR≈{RR_TARGET}, SL=ATR×{ATR_MULT_SL}.\n"
            f"Монеты: {', '.join([s.replace('/','') for s in SYMBOLS])}")

    while True:
        try:
            for s in SYMBOLS:
                sig = detect_signal_for_symbol(s)
                if sig:
                    tg_send(format_signal(sig))
                    last_side[s] = sig["side"]
                    last_ts[s] = time.time()
            time.sleep(POLL_SECONDS)
        except Exception as e:
            tg_send(f"⚠️ Ошибка цикла: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
