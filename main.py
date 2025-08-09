# === main.py (Signals v2: LONG/SHORT, 5m+1h filters, TP 0.5% / SL 0.4%) ===
import time, threading, os, logging, requests, math
from datetime import datetime, timezone
from flask import Flask

# ---------- TELEGRAM ----------
TELEGRAM_TOKEN   = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

def tg_send(text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ---------- НАСТРОЙКИ ----------
SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","TRXUSDT",
    "DOGEUSDT","PEPEUSDT","BGBUSDT","TONUSDT","ADAUSDT","APTUSDT","ARBUSDT"
]

# Таймфреймы
TF_5M_SEC  = 300
TF_1H_SEC  = 3600

EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
VOL_MA = 20              # средний объём по 20 свечам
VOL_SPIKE_K = 1.2        # всплеск объёма > 1.2× среднего

# Цели
TP_PCT = 0.005   # 0.5%
SL_PCT = 0.004   # 0.4%

# Частоты и антиспам
CHECK_EVERY_SEC = 30
PER_SYMBOL_COOLDOWN = 60 * 20   # не чаще чем раз в 20 минут по одному символу (если нет нового кросса)
GLOBAL_OK_COOLDOWN  = 60 * 60   # «нет сигналов» – не чаще 1/ч

BITGET_SPOT_CANDLES = "https://api.bitget.com/api/spot/v1/market/candles"
HEADERS = {"User-Agent": "Mozilla/5.0"}

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("signals-v2")

# ---------- ИНДИКАТОРЫ ----------
def ema(values, period):
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    out = [None] * (period - 1)
    sma = sum(values[:period]) / period
    out.append(sma)
    val = sma
    for x in values[period:]:
        val = x * k + val * (1 - k)
        out.append(val)
    return out

def rsi(values, period=14):
    if len(values) < period + 1:
        return []
    gains, losses = [], []
    for i in range(1, period + 1):
        ch = values[i] - values[i-1]
        gains.append(max(ch, 0.0))
        losses.append(abs(min(ch, 0.0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    rsis = [None]*period
    # Wilder smoothing
    for i in range(period + 1, len(values)):
        ch = values[i] - values[i-1]
        gain = max(ch, 0.0)
        loss = abs(min(ch, 0.0))
        avg_gain = (avg_gain*(period-1) + gain) / period
        avg_loss = (avg_loss*(period-1) + loss) / period
        if avg_loss == 0:
            rs = float('inf')
        else:
            rs = avg_gain / avg_loss
        r = 100 - (100/(1+rs))
        rsis.append(r)
    return rsis

# ---------- ДАННЫЕ ----------
def fetch_spot_candles(symbol: str, granularity_sec: int, limit: int = 300):
    """
    Возвращает списки по времени (возрастание):
    closes, base_volumes
    Bitget формат строки: [ts, open, high, low, close, baseVol, quoteVol]
    """
    try:
        params = {"symbol": symbol, "granularity": granularity_sec, "limit": str(limit)}
        r = requests.get(BITGET_SPOT_CANDLES, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            return [], []
        rows = []
        for row in data:
            ts = int(row[0])
            close = float(row[4])
            base_vol = float(row[5]) if len(row) > 5 else 0.0
            rows.append((ts, close, base_vol))
        rows.sort(key=lambda x: x[0])  # по времени старые -> новые
        closes = [c for _, c, _ in rows]
        vols   = [v for _, _, v in rows]
        return closes, vols
    except Exception as e:
        log.error(f"{symbol} fetch error: {e}")
        return [], []

# ---------- ЛОГИКА СИГНАЛОВ ----------
last_signal_side = {s: None for s in SYMBOLS}  # 'long'/'short'
last_signal_ts   = {s: 0 for s in SYMBOLS}
last_no_signal_sent = 0

def pct(x): return f"{x*100:.2f}%"

def price_levels(price, direction):
    if direction == "long":
        tp = price * (1 + TP_PCT)
        sl = price * (1 - SL_PCT)
    else:
        tp = price * (1 - TP_PCT)
        sl = price * (1 + SL_PCT)
    return round(tp, 6), round(sl, 6)

def analyze_symbol(sym: str):
    # 5m
    closes5, vols5 = fetch_spot_candles(sym, TF_5M_SEC, limit=300)
    if len(closes5) < max(EMA_SLOW+2, RSI_PERIOD+2, VOL_MA+2):
        return None  # мало данных

    ema9_5  = ema(closes5, EMA_FAST)
    ema21_5 = ema(closes5, EMA_SLOW)
    rsi5    = rsi(closes5, RSI_PERIOD)

    f_prev, s_prev = ema9_5[-2], ema21_5[-2]
    f_cur,  s_cur  = ema9_5[-1], ema21_5[-1]
    rsi_cur = rsi5[-1]
    price   = closes5[-1]

    if any(v is None for v in (f_prev, s_prev, f_cur, s_cur, rsi_cur)):
        return None

    # Объём: всплеск > 1.2× среднего по предыдущим 20 свечам
    if len(vols5) < VOL_MA + 1:
        vol_spike = False
    else:
        vol_ma = sum(vols5[-(VOL_MA+1):-1]) / VOL_MA
        vol_spike = vols5[-1] > VOL_SPIKE_K * vol_ma

    # Кроссы на 5m по закрытой свече
    bull_cross = (f_prev <= s_prev) and (f_cur > s_cur)
    bear_cross = (f_prev >= s_prev) and (f_cur < s_cur)

    # 1h тренд-фильтр (по закрытой 1h свече)
    closes1h, _ = fetch_spot_candles(sym, TF_1H_SEC, limit=200)
    if len(closes1h) < EMA_SLOW + 1:
        return None
    ema9_1h  = ema(closes1h, EMA_FAST)
    ema21_1h = ema(closes1h, EMA_SLOW)
    t_fast, t_slow = ema9_1h[-1], ema21_1h[-1]
    if any(v is None for v in (t_fast, t_slow)):
        return None
    uptrend = t_fast > t_slow
    downtrend = t_fast < t_slow

    # RSI фильтр:
    long_ok  = 45 <= rsi_cur <= 65
    short_ok = 35 <= rsi_cur <= 55

    # Сигналы
    long_signal  = bull_cross and uptrend and long_ok
    short_signal = bear_cross and downtrend and short_ok

    # Уверенность: A — если vol_spike и RSI в более узком коридоре, иначе B
    conf = None
    if long_signal:
        conf = "A" if (vol_spike and 50 <= rsi_cur <= 60) else "B"
        direction = "long"
    elif short_signal:
        conf = "A" if (vol_spike and 40 <= rsi_cur <= 50) else "B"
        direction = "short"
    else:
        return {
            "symbol": sym, "signal": None, "price": price,
            "rsi": round(rsi_cur,2), "vol_spike": vol_spike,
            "ema5": (round(f_cur,6), round(s_cur,6)),
            "ema1h": (round(t_fast,6), round(t_slow,6))
        }

    tp, sl = price_levels(price, direction)
    return {
        "symbol": sym,
        "signal": direction,
        "confidence": conf,
        "price": round(price,6),
        "tp": tp, "sl": sl,
        "tp_pct": TP_PCT, "sl_pct": SL_PCT,
        "rsi": round(rsi_cur,2),
        "vol_spike": vol_spike,
        "ema5": (round(f_cur,6), round(s_cur,6)),
        "ema1h": (round(t_fast,6), round(t_slow,6))
    }

def run_loop():
    global last_no_signal_sent
    tg_send("🤖 Signals v2 запущен: 5m EMA9/21 + тренд 1h + RSI + объём. Цели: TP 0.5% / SL 0.4%.")
    time.sleep(1)

    while True:
        try:
            any_signal = False
            for sym in SYMBOLS:
                res = analyze_symbol(sym)
                if not res:
                    continue

                if res["signal"] is None:
                    # (можно логировать, но не спамим)
                    continue

                direction = res["signal"]
                now = time.time()
                # антиспам: повтор по тому же направлению — не чаще PER_SYMBOL_COOLDOWN, если нет НОВОГО кросса
                # здесь считаем, что analyze_symbol уже отдаёт только при новом кроссе (т.к. bull/bear_cross по закрытой свече),
                # поэтому даём сообщение, но застрахуемся:
                if last_signal_side.get(sym) == direction and (now - last_signal_ts.get(sym, 0) < PER_SYMBOL_COOLDOWN):
                    continue

                last_signal_side[sym] = direction
                last_signal_ts[sym] = now
                any_signal = True

                arrow = "🟢 LONG" if direction == "long" else "🔴 SHORT"
                conf = "✅ A" if res["confidence"] == "A" else "✔️ B"
                msg = (
                    f"{arrow} сигнал {sym}\n"
                    f"Цена: ~ {res['price']}\n"
                    f"TP: {res['tp']} ({pct(res['tp_pct'])}) | SL: {res['sl']} ({pct(res['sl_pct'])})\n"
                    f"RSI(5m): {res['rsi']} | Объём спайк: {'да' if res['vol_spike'] else 'нет'} | Уверенность: {conf}\n"
                    f"EMA5m 9/21: {res['ema5'][0]} / {res['ema5'][1]}\n"
                    f"Тренд 1h EMA 9/21: {res['ema1h'][0]} / {res['ema1h'][1]}"
                )
                tg_send(msg)

            # общее сообщение «нет сигналов»
            now = time.time()
            if not any_signal and now - last_no_signal_sent >= GLOBAL_OK_COOLDOWN:
                last_no_signal_sent = now
                tg_send("ℹ️ Пока без новых сигналов. Проверяю рынок…")

        except Exception as e:
            log.exception(f"Loop error: {e}")

        time.sleep(CHECK_EVERY_SEC)

# ---------- FLASK (для Render) ----------
app = Flask(__name__)

@app.route("/")
def home():
    return "Signals v2 is running. UTC: " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def start_loop():
    t = threading.Thread(target=run_loop, daemon=True)
    t.start()

if __name__ == "__main__":
    start_loop()
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
