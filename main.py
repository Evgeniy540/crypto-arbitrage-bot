import os
import time
import math
import threading
import logging
from datetime import datetime, timezone
from typing import Dict, List

import requests
from flask import Flask, jsonify

# ================== НАСТРОЙКИ ==================

# Ваши данные Telegram (по вашей просьбе вписал прямо в код)
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"

# Пары SPOT на Bitget
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BGBUSDT", "TRXUSDT", "PEPEUSDT"]

# Таймфрейм свечей Bitget v2: "1m", "5m", "15m", "1h", ...
TIMEFRAME = "1m"

# EMA параметры
EMA_SHORT = 7
EMA_LONG  = 14

# Сколько свечей тянуть (должно быть > EMA_LONG * 3, чтобы сгладить старт)
CANDLES_LIMIT = 220

# Пауза между циклами опроса (сек)
SLEEP_SEC = 12

# Кулдаун после сигнала по конкретному символу (чтобы не дублировать)
SIGNAL_COOLDOWN_SEC = 60

# Сообщать только по факту новых пересечений
SEND_ONLY_ON_CROSS = True

# =================================================


# Логирование покороче
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("bitget_ema_bot")

session = requests.Session()
session.headers.update({"User-Agent": "ema-signal-bot/1.0"})

BITGET_BASE = "https://api.bitget.com"


def send_tg(text: str) -> None:
    """Отправка сообщения в Telegram."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
        r = session.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            log.warning("Telegram send error: %s %s", r.status_code, r.text[:300])
    except Exception as e:
        log.exception("Telegram exception: %s", e)


def get_candles(symbol: str, time_frame: str, limit: int) -> List[List]:
    """
    Bitget v2 SPOT candles.
    GET /api/v2/spot/market/candles?symbol=BTCUSDT&timeFrame=1m&limit=200

    Ответ: data -> список массивов, где обычно:
    [ts, open, high, low, close, volume, quoteVolume]
    Значения — строки, ts в миллисекундах/секундах (Bitget выдает мс).
    """
    url = f"{BITGET_BASE}/api/v2/spot/market/candles"
    params = {"symbol": symbol, "timeFrame": time_frame, "limit": str(limit)}
    r = session.get(url, params=params, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"{symbol} ошибка свечей: HTTP {r.status_code}: {r.text[:300]}")
    d = r.json()
    if not isinstance(d, dict) or "data" not in d:
        raise RuntimeError(f"{symbol} неожиданный ответ: {d}")
    return d["data"]


def to_closes(candles: List[List]) -> List[float]:
    """Достаём цены close из массива свечей Bitget. Реверсим, чтобы от старых к новым."""
    if not candles:
        return []
    # Bitget отдаёт новые -> старые. Развернем:
    arr = list(reversed(candles))
    closes = []
    for c in arr:
        # ожидаем [ts, open, high, low, close, volume, quote]
        if len(c) >= 5:
            val = c[4]
        else:
            # fallback (почти не случается)
            val = c[-1]
        try:
            closes.append(float(val))
        except:
            # пропустим битую свечу
            continue
    return closes


def ema(series: List[float], period: int) -> List[float]:
    """Простая EMA без сторонних библиотек."""
    if period <= 1 or len(series) < period:
        return []
    k = 2 / (period + 1)
    out = []
    # старт — SMA первых period значений
    sma = sum(series[:period]) / period
    out.extend([math.nan] * (period - 1))
    out.append(sma)
    prev = sma
    for price in series[period:]:
        val = price * k + prev * (1 - k)
        out.append(val)
        prev = val
    return out


def last_cross_signal(ema_fast: List[float], ema_slow: List[float]):
    """
    Определяем сигнал на последней свече:
      - BUY  если fast пересёк slow вверх
      - SELL если fast пересёк slow вниз
    Возвращаем ('BUY'|'SELL'|None)
    """
    if not ema_fast or not ema_slow:
        return None
    n = min(len(ema_fast), len(ema_slow))
    if n < 2:
        return None

    f1, s1 = ema_fast[n - 2], ema_slow[n - 2]
    f2, s2 = ema_fast[n - 1], ema_slow[n - 1]

    if math.isnan(f1) or math.isnan(s1) or math.isnan(f2) or math.isnan(s2):
        return None

    # пересечение вверх
    if f1 <= s1 and f2 > s2:
        return "BUY"
    # пересечение вниз
    if f1 >= s1 and f2 < s2:
        return "SELL"
    return None


def fmt_ts(ts: float = None) -> str:
    dt = datetime.now(timezone.utc) if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.isoformat()


def build_signal_text(side: str, symbol: str, price: float) -> str:
    bell = "🔔"
    return (
        f"{bell} {side} {symbol}\n"
        f"Цена: {price:.6f}\n"
        f"EMA{EMA_SHORT} vs EMA{EMA_LONG} (TF {TIMEFRAME})\n"
        f"{fmt_ts()}"
    )


class EmaWorker:
    def __init__(self, symbols: List[str]):
        self.symbols = symbols
        self.last_signal_at: Dict[str, float] = {}   # unix time последнего сигнала
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def run(self):
        send_tg(f"🤖 Бот запущен! EMA {EMA_SHORT}/{EMA_LONG}, TF {TIMEFRAME}.\nСообщения — только по факту новых пересечений.")
        while True:
            for sym in self.symbols:
                try:
                    candles = get_candles(sym, TIMEFRAME, CANDLES_LIMIT)
                    closes = to_closes(candles)
                    if len(closes) < EMA_LONG + 2:
                        log.warning("%s мало данных: %d", sym, len(closes))
                        continue

                    e_fast = ema(closes, EMA_SHORT)
                    e_slow = ema(closes, EMA_LONG)

                    sig = last_cross_signal(e_fast, e_slow)
                    if sig is None and SEND_ONLY_ON_CROSS:
                        continue

                    # Текущая цена = последний close
                    last_price = closes[-1]

                    if sig is not None:
                        now = time.time()
                        last_at = self.last_signal_at.get(sym, 0)
                        if now - last_at < SIGNAL_COOLDOWN_SEC:
                            # кулдаун
                            continue
                        self.last_signal_at[sym] = now
                        text = build_signal_text(sig, sym, last_price)
                        send_tg(text)
                        log.info("Signal %s %s @ %.8f", sig, sym, last_price)

                except Exception as e:
                    log.error("%s ошибка цикла: %s", sym, e)
                time.sleep(0.4)  # маленький промежуток между символами
            time.sleep(SLEEP_SEC)


# ---------------------- HTTP "живой" эндпоинт для Render ----------------------

app = Flask(__name__)
worker = EmaWorker(SYMBOLS)

@app.route("/", methods=["GET"])
def root():
    return "ok", 200

@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "ok": True,
        "time": fmt_ts(),
        "tf": TIMEFRAME,
        "ema": f"{EMA_SHORT}/{EMA_LONG}",
        "symbols": SYMBOLS,
        "cooldown_sec": SIGNAL_COOLDOWN_SEC
    }), 200


def main():
    # стартуем фонового работника
    worker.start()

    # поднимем веб-сервер (Render любит привязку к порту)
    port = int(os.environ.get("PORT", "10000"))
    log.info("Сервис слушает порт %d", port)
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
