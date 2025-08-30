# -*- coding: utf-8 -*-
import os
import time
import math
import threading
from datetime import datetime, timezone
import requests
from flask import Flask

# ========= ТВОИ ДАННЫЕ (как просил) =========
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
# ============================================

# ====== НАСТРОЙКИ БОТА ======
FUT_SUFFIX = "_UMCBL"        # Фьючерсы USDT-M на Bitget
# Добавил много монет. Можно убирать/добавлять.
SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","TRXUSDT",
    "BNBUSDT","ADAUSDT","DOGEUSDT","AVAXUSDT","DOTUSDT",
    "LTCUSDT","APTUSDT","ARBUSDT","OPUSDT","LINKUSDT",
    "ATOMUSDT","NEARUSDT","FILUSDT","SUIUSDT","PEPEUSDT",
    "SHIBUSDT","ETCUSDT","ICPUSDT","INJUSDT"
]
TF = "5min"                  # 1min / 5min / 15min / 30min / 1h
RSI_PERIOD = 14
EMA_FAST = 50
EMA_SLOW = 200              # Требуется >=200 свечей
CHECK_INTERVAL_SEC = 60     # Частота проверок
SEND_STARTUP_MESSAGE = True

# ====== ВЕБ-СЕРВЕР ДЛЯ KEEP-ALIVE ======
app = Flask(__name__)

@app.route("/")
def root():
    return "OK: crypto alert bot is running"

def run_flask():
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)

# ====== УТИЛИТЫ ======
def tg_send(text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
        requests.post(url, json=data, timeout=10)
    except Exception:
        pass

def ema(series, period):
    if len(series) < period:
        return [math.nan]*len(series)
    k = 2/(period+1)
    out = [math.nan]*(period-1)
    # SMA на первых period значениях
    sma = sum(series[:period])/period
    out.append(sma)
    prev = sma
    for x in series[period:]:
        val = x*k + prev*(1-k)
        out.append(val)
        prev = val
    return out

def rsi(values, period=14):
    if len(values) < period + 1:
        return [math.nan]*len(values)
    gains = [0.0]
    losses = [0.0]
    for i in range(1, len(values)):
        ch = values[i] - values[i-1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    # средние первые
    avg_gain = sum(gains[1:period+1]) / period
    avg_loss = sum(losses[1:period+1]) / period
    rsis = [math.nan]*(period)
    def rsi_from(gl, ll):
        if ll == 0:
            return 100.0
        rs = gl/ll
        return 100 - (100/(1+rs))
    rsis.append(rsi_from(avg_gain, avg_loss))
    for i in range(period+1, len(values)):
        avg_gain = (avg_gain*(period-1) + gains[i]) / period
        avg_loss = (avg_loss*(period-1) + losses[i]) / period
        rsis.append(rsi_from(avg_gain, avg_loss))
    return rsis

def bitget_granularity(tf: str) -> str:
    # Bitget history-candles принимает: 1min, 3min, 5min, 15min, 30min, 1h, 4h, 1day
    return tf

def fetch_candles(symbol: str, tf: str, limit: int = 300):
    """
    Возвращает список свечей (open_time, open, high, low, close, volume)
    в порядке от старых к новым. Использует фьючерсный endpoint.
    """
    url = "https://api.bitget.com/api/mix/v1/market/history-candles"
    params = {
        "symbol": symbol + FUT_SUFFIX,
        "granularity": bitget_granularity(tf),
        "limit": str(limit)
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "00000" or "data" not in data:
        return []
    rows = data["data"]            # Bitget отдаёт от новых к старым
    rows.reverse()
    # формат: ["1709292000000","open","high","low","close","volume","turnover"]
    out = []
    for row in rows:
        try:
            ts = int(row[0])//1000
            o,h,l,c,v = map(float, row[1:6])
            out.append((ts,o,h,l,c,v))
        except:
            continue
    return out

def make_status_line(sym: str, closes, ema50, ema200, rsi14):
    last_close = closes[-1]
    e50_prev, e200_prev = ema50[-2], ema200[-2]
    e50, e200 = ema50[-1], ema200[-1]
    r = rsi14[-1]

    # Логика сигналов: кросс EMA50/200 + простой фильтр RSI
    signal = "Сигнала нет"
    if not math.isnan(e50_prev) and not math.isnan(e200_prev):
        # кросс вверх
        if e50 > e200 and e50_prev <= e200_prev and r >= 50:
            signal = "🚀 LONG сигнал"
        # кросс вниз
        elif e50 < e200 and e50_prev >= e200_prev and r <= 50:
            signal = "🔻 SHORT сигнал"

    return (f"{sym}: close={round(last_close,6)} | "
            f"EMA50={round(e50,6)} | EMA200={round(e200,6)} | "
            f"RSI={round(r,2)} | {signal}")

def check_once():
    lines = []
    for sym in SYMBOLS:
        try:
            candles = fetch_candles(sym, TF, limit=max(EMA_SLOW+10, 250))
            if len(candles) < EMA_SLOW:
                lines.append(f"{sym}: Недостаточно данных ({len(candles)}/{EMA_SLOW})")
                continue

            closes = [c[4] for c in candles]
            ema50 = ema(closes, EMA_FAST)
            ema200 = ema(closes, EMA_SLOW)
            rsi14 = rsi(closes, RSI_PERIOD)

            if math.isnan(ema200[-1]) or math.isnan(rsi14[-1]):
                lines.append(f"{sym}: Недостаточно данных для индикаторов")
                continue

            lines.append(make_status_line(sym, closes, ema50, ema200, rsi14))

        except Exception as e:
            lines.append(f"{sym}: ошибка получения данных: {e}")

    # Отправляем одним сообщением, чтобы не спамить
    dt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg = f"🧾 Статус ({TF}) — {dt}\n" + "\n".join(lines)
    tg_send(msg)

def loop():
    if SEND_STARTUP_MESSAGE:
        tg_send("🤖 Бот запущен (логирование включено: EMA50/200 + RSI).")
    while True:
        check_once()
        time.sleep(CHECK_INTERVAL_SEC)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    loop()
