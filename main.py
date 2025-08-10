# === main.py v2.7 (Bitget SPOT V2, candles, LONG/SHORT, TP 0.5 / SL 0.4) ===
import time, threading, os, logging, requests
from datetime import datetime, timezone
from flask import Flask

TELEGRAM_TOKEN   = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

SYMBOLS = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT",
           "TRXUSDT","DOGEUSDT","PEPEUSDT","BGBUSDT"]

G5M  = "5min"
G1H  = "1h"

EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
VOL_MA = 20
VOL_SPIKE_K = 1.2

TP_PCT = 0.005
SL_PCT = 0.004

CHECK_EVERY_SEC = 30
PER_SYMBOL_COOLDOWN = 60*20
GLOBAL_OK_COOLDOWN  = 60*60

HEADERS = {"User-Agent":"Mozilla/5.0"}
CANDLES_V2 = "https://api.bitget.com/api/v2/spot/market/candles"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("signals-v2.7")

def tg_send(text):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ----------------- indicators -----------------
def ema(values, period):
    if len(values) < period: return []
    k = 2/(period+1)
    out = [None]*(period-1)
    sma = sum(values[:period])/period
    out.append(sma)
    v = sma
    for x in values[period:]:
        v = x*k + v*(1-k)
        out.append(v)
    return out

def rsi(values, period=14):
    if len(values) < period+1: return []
    gains, losses = [], []
    for i in range(1, period+1):
        ch = values[i]-values[i-1]
        gains.append(max(ch,0.0)); losses.append(abs(min(ch,0.0)))
    ag = sum(gains)/period; al = sum(losses)/period
    out = [None]*period
    for i in range(period+1, len(values)):
        ch = values[i]-values[i-1]
        g = max(ch,0.0); l = abs(min(ch,0.0))
        ag = (ag*(period-1)+g)/period
        al = (al*(period-1)+l)/period
        rs = float('inf') if al==0 else ag/al
        out.append(100 - 100/(1+rs))
    return out

# ----------------- data (V2 spot) -----------------
def fetch_spot_candles(symbol: str, granularity: str, limit: int = 300):
    """V2 spot endpoint: /api/v2/spot/market/candles  granularity: '5min','1h',..."""
    r = requests.get(CANDLES_V2,
                     params={"symbol": symbol, "granularity": granularity, "limit": str(limit)},
                     headers=HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json().get("data", [])
    rows = []
    for row in data:
        # [ts, open, high, low, close, baseVol, quoteVol, ?]
        try:
            rows.append((int(row[0]), float(row[4]), float(row[5]) if len(row)>5 else 0.0))
        except: pass
    rows.sort(key=lambda x: x[0])  # old -> new
    closes = [c for _,c,_ in rows]
    vols   = [v for *_,v in rows]
    return closes, vols

# ----------------- logic -----------------
last_signal_side = {s: None for s in SYMBOLS}
last_signal_ts   = {s: 0 for s in SYMBOLS}
last_no_signal_sent = 0

def pct(x): return f"{x*100:.2f}%"

def price_levels(price, direction):
    if direction=="long":
        tp = price*(1+TP_PCT); sl = price*(1-SL_PCT)
    else:
        tp = price*(1-TP_PCT); sl = price*(1+SL_PCT)
    return round(tp,6), round(sl,6)

def analyze_symbol(sym: str):
    closes5, vols5 = fetch_spot_candles(sym, G5M, 300)
    if len(closes5) < max(EMA_SLOW+2, RSI_PERIOD+2, VOL_MA+2): return None

    ema9_5, ema21_5, rsi5 = ema(closes5, EMA_FAST), ema(closes5, EMA_SLOW), rsi(closes5, RSI_PERIOD)
    f_prev, s_prev, f_cur, s_cur = ema9_5[-2], ema21_5[-2], ema9_5[-1], ema21_5[-1]
    rsi_cur, price = rsi5[-1], closes5[-1]
    if any(v is None for v in (f_prev, s_prev, f_cur, s_cur, rsi_cur)): return None

    vol_spike = False
    if len(vols5) >= VOL_MA + 1:
        vol_ma = sum(vols5[-(VOL_MA+1):-1])/VOL_MA
        vol_spike = vols5[-1] > VOL_SPIKE_K * vol_ma

    bull_cross = (f_prev <= s_prev) and (f_cur > s_cur)
    bear_cross = (f_prev >= s_prev) and (f_cur < s_cur)

    closes1h, _ = fetch_spot_candles(sym, G1H, 200)
    if len(closes1h) < EMA_SLOW + 1: return None
    ema9_1h, ema21_1h = ema(closes1h, EMA_FAST), ema(closes1h, EMA_SLOW)
    t_fast, t_slow = ema9_1h[-1], ema21_1h[-1]
    if any(v is None for v in (t_fast, t_slow)): return None

    uptrend, downtrend = t_fast > t_slow, t_fast < t_slow
    long_ok, short_ok = (45 <= rsi_cur <= 65), (35 <= rsi_cur <= 55)

    long_signal  = bull_cross and uptrend and long_ok
    short_signal = bear_cross and downtrend and short_ok
    if not (long_signal or short_signal): return None

    direction = "long" if long_signal else "short"
    conf = "A" if (vol_spike and ((direction=="long" and 50<=rsi_cur<=60) or (direction=="short" and 40<=rsi_cur<=50))) else "B"
    tp, sl = price_levels(price, direction)
    return {
        "symbol": sym, "direction": direction, "confidence": conf,
        "price": round(price,6), "tp": tp, "sl": sl,
        "tp_pct": TP_PCT, "sl_pct": SL_PCT,
        "ema5": (round(f_cur,6), round(s_cur,6)),
        "ema1h": (round(t_fast,6), round(t_slow,6)),
        "rsi": round(rsi_cur,2), "vol_spike": vol_spike
    }

def run_loop():
    global last_no_signal_sent
    tg_send("🤖 Signals v2.7 запущен (Bitget SPOT V2). TF: 5min/1h. TP 0.5% / SL 0.4%.")

    # sanity check
    for s in SYMBOLS:
        try:
            c,_ = fetch_spot_candles(s, G5M, 50)
            log.info(f"{s}: свечей(5m)={len(c)} (V2)")
        except Exception as e:
            log.error(f"{s} start fetch error: {e}")

    while True:
        try:
            any_signal = False
            for sym in SYMBOLS:
                res = analyze_symbol(sym)
                if not res:
                    continue
                direction = res["direction"]; now = time.time()
                if last_signal_side.get(sym) == direction and (now - last_signal_ts.get(sym,0) < PER_SYMBOL_COOLDOWN):
                    continue
                last_signal_side[sym] = direction
                last_signal_ts[sym] = now
                any_signal = True

                arrow = "🟢 LONG" if direction=="long" else "🔴 SHORT"
                conf = "✅ A" if res["confidence"]=="A" else "✔️ B"
                tg_send(
                    f"{arrow} сигнал {res['symbol']}\n"
                    f"Цена: ~ {res['price']}\n"
                    f"TP: {res['tp']} ({pct(res['tp_pct'])}) | SL: {res['sl']} ({pct(res['sl_pct'])})\n"
                    f"RSI(5m): {res['rsi']} | Объём спайк: {'да' if res['vol_spike'] else 'нет'} | Уверенность: {conf}\n"
                    f"EMA5m 9/21: {res['ema5'][0]} / {res['ema5'][1]} | Тренд 1h: {res['ema1h'][0]} / {res['ema1h'][1]}"
                )

            now = time.time()
            if not any_signal and now - last_no_signal_sent >= GLOBAL_OK_COOLDOWN:
                last_no_signal_sent = now
                tg_send("ℹ️ Пока без новых сигналов. Проверяю рынок…")
        except Exception as e:
            log.exception(f"Loop error: {e}")
        time.sleep(CHECK_EVERY_SEC)

# ----- FLASK -----
app = Flask(__name__)
@app.route("/")
def home():
    return "Signals v2.7 running (SPOT V2). UTC: " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def start_loop():
    threading.Thread(target=run_loop, daemon=True).start()

if __name__ == "__main__":
    start_loop()
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
