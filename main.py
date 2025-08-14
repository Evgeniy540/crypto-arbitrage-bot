import os
import time
import threading
from datetime import datetime, timezone
import requests
from flask import Flask, jsonify

# ========= ПАРАМЕТРЫ =========
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT", "PEPEUSDT", "BGBUSDT"]
TF_SECONDS = 60                # 1m
EMA_FAST, EMA_SLOW = 7, 14     # EMA 7/14
CANDLES_LIMIT = 200            # сколько свечей тянем
COOLDOWN_SEC = 60              # защита от частых повторов
CHECK_INTERVAL = 5             # раз в N секунд пробегаемся по списку
RENDER_PORT = int(os.getenv("PORT", "10000"))

# ========= ВСПОМОГАТОРЫ =========
session = requests.Session()
session.headers.update({"User-Agent": "ema-bot/1.0"})

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def tsend(text: str):
    """Отправка сообщения в Telegram со страховкой от ошибок сети."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        r = session.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"[TELEGRAM] HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[TELEGRAM] exception: {e}")

def ema_series(closes, period):
    """Возвращает ряд EMA с тем же размером, что и closes."""
    k = 2 / (period + 1)
    ema = []
    s = None
    for i, c in enumerate(closes):
        if s is None:
            # стартуем с простой средней по первым 'period' точкам,
            # если данных меньше — берём обычное среднее из доступных
            wnd = closes[max(0, i - period + 1):i + 1]
            s = sum(wnd) / len(wnd)
        else:
            s = c * k + s * (1 - k)
        ema.append(s)
    return ema

def parse_float(x):
    try:
        return float(x)
    except Exception:
        return None

def get_candles_bitget(symbol: str, granularity: int, limit: int):
    """
    Пытаемся v2: /api/v2/spot/market/candles?symbol=BTCUSDT&granularity=60&limit=200
    Если код != 00000 или 400 — пробуем v1: /api/spot/v1/market/candles?symbol=BTCUSDT&period=1min&limit=200
    Возвращает список кортежей [(ts, open, high, low, close, volume), ...] по возрастанию ts.
    """
    # ---- v2
    try:
        url_v2 = "https://api.bitget.com/api/v2/spot/market/candles"
        params = {"symbol": symbol, "granularity": str(granularity), "limit": str(limit)}
        r = session.get(url_v2, params=params, timeout=10)
        data = r.json()
        if r.status_code == 200 and data.get("code") == "00000":
            rows = data.get("data", [])
            out = []
            # v2 формат: ["1700793600000","43019.9","43034.9","43019.9","43022.7","6.3"] (ts, o,h,l,c,vol) — ts в ms
            for row in rows:
                ts = int(row[0]) // 1000
                o = parse_float(row[1]); h = parse_float(row[2]); l = parse_float(row[3]); c = parse_float(row[4])
                v = parse_float(row[5])
                if None not in (o, h, l, c, v):
                    out.append((ts, o, h, l, c, v))
            out.sort(key=lambda x: x[0])
            if len(out) > 0:
                return out
        else:
            print(f"[{symbol}] v2 err: HTTP {r.status_code}, code={data.get('code')} msg={data.get('msg')}")
    except Exception as e:
        print(f"[{symbol}] v2 exception: {e}")

    # ---- v1 fallback
    try:
        url_v1 = "https://api.bitget.com/api/spot/v1/market/candles"
        period = {60: "1min", 300: "5min", 900: "15min", 3600: "1hour", 86400: "1day"}.get(granularity, "1min")
        params = {"symbol": symbol, "period": period, "limit": str(limit)}
        r = session.get(url_v1, params=params, timeout=10)
        data = r.json()
        # v1 тоже возвращает {"code":"00000", "data": [...]}
        if r.status_code == 200 and data.get("code") == "00000":
            rows = data.get("data", [])
            out = []
            # v1 формат, как правило, тот же порядок полей
            for row in rows:
                ts = int(row[0]) // 1000
                o = parse_float(row[1]); h = parse_float(row[2]); l = parse_float(row[3]); c = parse_float(row[4])
                v = parse_float(row[5])
                if None not in (o, h, l, c, v):
                    out.append((ts, o, h, l, c, v))
            out.sort(key=lambda x: x[0])
            if len(out) > 0:
                return out
        else:
            print(f"[{symbol}] v1 err: HTTP {r.status_code}, code={data.get('code')} msg={data.get('msg')}")
    except Exception as e:
        print(f"[{symbol}] v1 exception: {e}")

    return []

# сохраняем последнее зафиксированное состояние, чтобы не спамить
last_state = {}     # symbol -> {"side":"buy"/"sell","ts":close_ts}
last_sent  = {}     # symbol -> last send ts

def check_symbol(symbol: str):
    candles = get_candles_bitget(symbol, TF_SECONDS, CANDLES_LIMIT)
    if len(candles) < max(EMA_SLOW + 1, 25):
        print(f"[{symbol}] мало свечей: {len(candles)}")
        return

    closes = [c[4] for c in candles]
    fast = ema_series(closes, EMA_FAST)
    slow = ema_series(closes, EMA_SLOW)

    # берём две последние точки, чтобы отлавливать факт нового пересечения
    f_prev, f_now = fast[-2], fast[-1]
    s_prev, s_now = slow[-2], slow[-1]
    cross_up = f_prev <= s_prev and f_now > s_now
    cross_dn = f_prev >= s_prev and f_now < s_now

    close_ts = candles[-1][0]
    price = closes[-1]

    state = last_state.get(symbol)
    cool_ok = (time.time() - last_sent.get(symbol, 0)) >= COOLDOWN_SEC

    if cross_up and cool_ok and (not state or state.get("side") != "buy" or state.get("ts") != close_ts):
        txt = (
            f"🔔 BUY {symbol}\n"
            f"Цена: {price}\n"
            f"EMA{EMA_FAST} пересекла EMA{EMA_SLOW} ВВЕРХ (TF 1m)\n"
            f"{now_iso()}"
        )
        tsend(txt)
        last_state[symbol] = {"side": "buy", "ts": close_ts}
        last_sent[symbol] = time.time()
        print(f"[{symbol}] BUY signal sent")

    elif cross_dn and cool_ok and (not state or state.get("side") != "sell" or state.get("ts") != close_ts):
        txt = (
            f"🔔 SELL {symbol}\n"
            f"Цена: {price}\n"
            f"EMA{EMA_FAST} пересекла EMA{EMA_SLOW} ВНИЗ (TF 1m)\n"
            f"{now_iso()}"
        )
        tsend(txt)
        last_state[symbol] = {"side": "sell", "ts": close_ts}
        last_sent[symbol] = time.time()
        print(f"[{symbol}] SELL signal sent")

def worker_loop():
    # стартовое сообщение
    tsend(f"🤖 Бот запущен! EMA {EMA_FAST}/{EMA_SLOW}, TF 1m. Сообщения — только по факту новых пересечений.")
    print("Worker started")
    while True:
        start = time.time()
        for sym in SYMBOLS:
            try:
                check_symbol(sym)
            except Exception as e:
                print(f"[{sym}] loop exception: {e}")
        # поддерживаем частоту проверки
        sleep_left = CHECK_INTERVAL - (time.time() - start)
        if sleep_left > 0:
            time.sleep(sleep_left)

# ========= FLASK для Render =========
app = Flask(__name__)

@app.get("/")
def root_ok():
    return "ok"

@app.get("/status")
def status():
    return jsonify({
        "ok": True,
        "time": now_iso(),
        "tf": "1m",
        "ema": f"{EMA_FAST}/{EMA_SLOW}",
        "symbols": SYMBOLS,
        "cooldown_sec": COOLDOWN_SEC
    })

def run_http():
    # Хост 0.0.0.0 обязателен на Render
    app.run(host="0.0.0.0", port=RENDER_PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    # HTTP сервер в отдельном потоке
    threading.Thread(target=run_http, daemon=True).start()
    # рабочий цикл сигналов
    worker_loop()
