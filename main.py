# -*- coding: utf-8 -*-
# Bitget Spot — сигнал‑бот (EMA пересечения). НИКАКИХ ордеров, только алерты в Telegram.

import os, json, time, threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify
import ccxt  # pip install ccxt

# ====== ТВОИ КЛЮЧИ (уже вписаны) ======
TELEGRAM_TOKEN   = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

# ====== НАСТРОЙКИ СИГНАЛОВ ======
SYMBOLS   = ["BTC/USDT","ETH/USDT","SOL/USDT","XRP/USDT","TRX/USDT","PEPE/USDT","BGB/USDT"]  # Bitget Spot формат ccxt
TIMEFRAME = "1m"     # таймфрейм свечей
EMA_FAST  = 9
EMA_SLOW  = 21
MIN_BARS  = 60       # минимум данных, чтобы EMA была устойчивой
POLL_SEC  = 20       # как часто опрашивать рынок
COOLDOWN_MIN = 10    # антиспам: не слать одинаковый сигнал по инструменту чаще, чем раз в N минут

STATE_FILE = "signals_state.json"    # сохраняем последнее направление и время сигнала

# ====== ВСПОМОГАТЕЛЬНОЕ ======
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except Exception:
        return default

def save_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def tg_send(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode":"HTML", "disable_web_page_preview": True},
            timeout=10
        )
    except Exception:
        pass

def ema(values: List[float], period: int) -> List[float]:
    if len(values) < period: return []
    k = 2.0/(period+1.0)
    out = [sum(values[:period])/period]
    for v in values[period:]:
        out.append(v*k + out[-1]*(1.0-k))
    return out

def crossover_signal(closes: List[float]) -> str:
    if len(closes) < max(EMA_SLOW, EMA_FAST) + 2: return "NONE"
    f = ema(closes, EMA_FAST); s = ema(closes, EMA_SLOW)
    if len(f) < 2 or len(s) < 2: return "NONE"
    # используем закрытые свечи: предпоследняя и последняя
    if f[-2] <= s[-2] and f[-1] > s[-1]:  return "BUY"
    if f[-2] >= s[-2] and f[-1] < s[-1]:  return "SELL"
    return "NONE"

# ====== БИРЖА (только данные, без ключей) ======
exchange = ccxt.bitget({"enableRateLimit": True, "options": {"defaultType": "spot"}})
exchange.load_markets()

def fetch_closes(symbol: str, limit: int = 200) -> List[float]:
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=limit)
        return [float(x[4]) for x in ohlcv]  # close
    except Exception:
        return []

# ====== СОСТОЯНИЕ ======
state: Dict[str, Dict] = load_json(STATE_FILE, {})   # { "BTC/USDT": {"last":"BUY|SELL|NONE", "ts": 169... } }

def can_push(symbol: str, new_sig: str) -> bool:
    """антиспам: не слать повторный такой же сигнал чаще cooldown"""
    if new_sig in ("NONE", None): return False
    prev = state.get(symbol, {})
    last_sig = prev.get("last", "NONE")
    last_ts  = float(prev.get("ts", 0))
    # если направление не менялось — проверяем кулдаун
    if last_sig == new_sig:
        if time.time() - last_ts < COOLDOWN_MIN*60:
            return False
    return True

def remember(symbol: str, sig: str):
    state[symbol] = {"last": sig, "ts": time.time()}
    save_json(STATE_FILE, state)

# ====== ОСНОВНОЙ ЦИКЛ ======
def loop():
    tg_send(f"📡 Сигнальный бот запущен: EMA {EMA_FAST}/{EMA_SLOW}, TF={TIMEFRAME}. Монеты: {', '.join([s.replace('/','') for s in SYMBOLS])}")
    while True:
        try:
            for sym in SYMBOLS:
                closes = fetch_closes(sym, limit=max(MIN_BARS, EMA_SLOW+5))
                if len(closes) < max(MIN_BARS, EMA_SLOW+5):  # данных мало — пропустим
                    continue
                sig = crossover_signal(closes)
                if can_push(sym, sig):
                    price = closes[-1]
                    msg = f"🔔 {sig} {sym.replace('/','')}\nЦена: {price:.6f}\nEMA{EMA_FAST} vs EMA{EMA_SLOW} (TF {TIMEFRAME})\n{now_iso()}"
                    tg_send(msg)
                    remember(sym, sig)
            time.sleep(POLL_SEC)
        except Exception as e:
            tg_send(f"⚠️ Ошибка цикла: {e}")
            time.sleep(5)

# ====== FLASK (health для Render) ======
app = Flask(__name__)

@app.get("/")
def health():
    return jsonify({
        "ok": True,
        "time": now_iso(),
        "symbols": SYMBOLS,
        "tf": TIMEFRAME,
        "ema": f"{EMA_FAST}/{EMA_SLOW}",
        "cooldown_min": COOLDOWN_MIN,
        "state": state
    })

# ====== START ======
if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
