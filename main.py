# === main.py v2.9 (Bitget SPOT V2, EMA 9/21, LONG/SHORT, TP 0.5% / SL 0.4%, exit notifier + rich logs) ===
import os, time, threading, logging, requests, json
from datetime import datetime, timezone
from flask import Flask

# ====== TELEGRAM ======
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "5723086631")

def tg_send(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10
        )
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ====== ПАРАМЕТРЫ ======
SYMBOLS = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","TRXUSDT","DOGEUSDT","PEPEUSDT","BGBUSDT"]

G5M  = "5min"
G1H  = "1h"

EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
VOL_MA = 20
VOL_SPIKE_K = 1.2

TP_PCT = 0.005   # +0.5%
SL_PCT = 0.004   # -0.4%

CHECK_EVERY_SEC = 30         # частота поиска новых сигналов
PER_SYMBOL_COOLDOWN = 60*20  # 20 минут на символ
GLOBAL_OK_COOLDOWN  = 60*60  # раз в час "нет сигналов"

# Мониторинг TP/SL
POLL_SECONDS = 12            # частота проверки открытых сигналов
POSITIONS_FILE = "positions.json"

# Диагностические логи мониторинга TP/SL
LOG_TPSL_EVERY_SEC = 60
_last_tp_sl_log_ts = {}  # symbol -> ts

# ====== HTTP ======
HEADERS = {"User-Agent":"Mozilla/5.0"}
CANDLES_V2 = "https://api.bitget.com/api/v2/spot/market/candles"
TICKERS_V2 = "https://api.bitget.com/api/v2/spot/market/tickers"
TICKER_V1  = "https://api.bitget.com/api/spot/v1/market/ticker"   # запасной план

# ====== LOGGING ======
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("signals-v2.9")

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
        # [ts, open, high, low, close, baseVol, quoteVol, ...]
        try:
            rows.append((int(row[0]), float(row[4]), float(row[5]) if len(row)>5 else 0.0))
        except: pass
    rows.sort(key=lambda x: x[0])  # old -> new
    closes = [c for _,c,_ in rows]
    vols   = [v for *_,v in rows]
    return closes, vols

def get_last_price(symbol: str) -> float:
    """Пробуем V2 тикер, потом V1 тикер."""
    try:
        r = requests.get(TICKERS_V2, params={"symbol": symbol}, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json().get("data")
        if isinstance(data, list) and data:
            last = data[0].get("last")
            if last is not None:
                return float(last)
        if isinstance(data, dict) and "last" in data:
            return float(data["last"])
    except Exception as e:
        log.warning(f"tickers V2 failed for {symbol}: {e}")

    # запасной план — V1
    r = requests.get(TICKER_V1, params={"symbol": symbol}, headers=HEADERS, timeout=10)
    r.raise_for_status()
    last = (r.json().get("data") or {}).get("last")
    if last is None:
        raise RuntimeError(f"No last price for {symbol}")
    return float(last)

# ----------------- helpers -----------------
def pct(x): return f"{x*100:.2f}%"

def _should_log_symbol(sym: str) -> bool:
    now = time.time()
    last = _last_tp_sl_log_ts.get(sym, 0)
    if now - last >= LOG_TPSL_EVERY_SEC:
        _last_tp_sl_log_ts[sym] = now
        return True
    return False

def _progress_to_tp(side: str, entry: float, price: float, tp: float) -> float:
    """Сколько % пути к TP пройдено (может быть отрицательным)."""
    if side == "LONG":
        span = tp - entry
        return 0.0 if span == 0 else (price - entry) / span * 100.0
    else:  # SHORT
        span = entry - tp
        return 0.0 if span == 0 else (entry - price) / span * 100.0

def price_levels(price, direction):
    if direction=="long":
        tp = price*(1+TP_PCT); sl = price*(1-SL_PCT)
    else:
        tp = price*(1-TP_PCT); sl = price*(1+SL_PCT)
    return round(tp,6), round(sl,6)

# ----------------- хранение открытых сигналов (для выхода по TP/SL) -----------------
def load_positions() -> dict:
    if not os.path.exists(POSITIONS_FILE):
        return {}
    try:
        with open(POSITIONS_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_positions(pos: dict):
    tmp = POSITIONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(pos, f, ensure_ascii=False, indent=2)
    os.replace(tmp, POSITIONS_FILE)

def register_signal(symbol: str, side: str, entry: float, tp: float, sl: float, source: str = "EMA 9/21"):
    side = side.upper().strip()
    if side not in ("LONG","SHORT"): return
    pos = load_positions()
    if symbol in pos and pos[symbol].get("is_open"):
        return
    pos[symbol] = {
        "is_open": True,
        "symbol": symbol,
        "side": side,
        "entry": float(entry),
        "tp": float(tp),
        "sl": float(sl),
        "opened_at": datetime.utcnow().isoformat(timespec="seconds"),
        "source": source
    }
    save_positions(pos)
    log.info(f"[OPEN] {symbol} {side} | entry={entry} tp={tp} sl={sl} | src={source}")

def _pnl_pct(side: str, entry: float, close: float) -> float:
    if side == "LONG":
        return (close - entry) / entry * 100.0
    else:
        return (entry - close) / entry * 100.0

def check_positions_once():
    pos = load_positions()
    changed = False
    for symbol, p in list(pos.items()):
        if not p.get("is_open"):
            continue

        side  = p["side"]
        entry = float(p["entry"])
        tp    = float(p["tp"])
        sl    = float(p["sl"])

        try:
            price = get_last_price(symbol)
        except Exception as e:
            log.warning(f"[PRICE] {symbol}: fetch failed: {e}")
            continue

        # --- диагностический лог раз в LOG_TPSL_EVERY_SEC сек на символ ---
        if _should_log_symbol(symbol):
            prog = _progress_to_tp(side, entry, price, tp)
            dist_tp = (abs(tp - price) / price) * 100.0
            dist_sl = (abs(price - sl) / price) * 100.0
            log.info(
                f"[WATCH] {symbol} {side} | price={price:.6f} | "
                f"TP={tp:.6f} (dist ~{dist_tp:.3f}%) | SL={sl:.6f} (dist ~{dist_sl:.3f}%) | "
                f"progress_to_TP={prog:.2f}%"
            )

        close_reason = None
        if side == "LONG":
            if price >= tp: close_reason = "✅ Закрыто по TP"
            elif price <= sl: close_reason = "❌ Закрыто по SL"
        else:  # SHORT
            if price <= tp: close_reason = "✅ Закрыто по TP (SHORT)"
            elif price >= sl: close_reason = "❌ Закрыто по SL (SHORT)"

        if close_reason:
            p["is_open"] = False
            p["closed_at"] = datetime.utcnow().isoformat(timespec="seconds")
            p["close_price"] = price
            pos[symbol] = p
            save_positions(pos)
            changed = True

            pl = _pnl_pct(side, entry, price)
            tg_send(
                f"{close_reason} по {symbol}\n"
                f"Цена закрытия: {price}\n"
                f"P/L: {pl:.3f}%\n"
                f"Открыто: {p['opened_at']}\nЗакрыто: {p['closed_at']}"
            )
            log.info(f"[CLOSE] {symbol} {side} @ {price} | {close_reason} | P/L={pl:.3f}%")

    if changed:
        save_positions(pos)

def check_positions_loop():
    while True:
        try:
            check_positions_once()
        except Exception as e:
            log.error(f"check_positions_loop error: {e}")
        time.sleep(POLL_SECONDS)

def start_closer():
    # показать, что сейчас в файле
    open_pos = [k for k,v in load_positions().items() if v.get("is_open")]
    if open_pos:
        log.info(f"[INIT] Открытые позиции при старте мониторинга: {', '.join(open_pos)}")
    else:
        log.info("[INIT] Открытых позиций нет")
    threading.Thread(target=check_positions_loop, daemon=True).start()

# ----------------- сигналка (EMA 9/21 + фильтры) -----------------
last_signal_side = {s: None for s in SYMBOLS}
last_signal_ts   = {s: 0 for s in SYMBOLS}
last_no_signal_sent = 0

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
    tg_send("🤖 Signals v2.9 запущен (Bitget SPOT V2). TF: 5m/1h. TP 0.5% / SL 0.4%. Полный цикл TP/SL и логи включены.")

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

                # --- отправка входного сигнала ---
                arrow = "🟢 LONG" if direction=="long" else "🔴 SHORT"
                conf = "✅ A" if res["confidence"]=="A" else "✔️ B"
                tg_send(
                    f"{arrow} сигнал {res['symbol']}\n"
                    f"Цена: ~ {res['price']}\n"
                    f"TP: {res['tp']} ({pct(TP_PCT)}) | SL: {res['sl']} ({pct(SL_PCT)})\n"
                    f"RSI(5m): {res['rsi']} | Объём спайк: {'да' if res['vol_spike'] else 'нет'} | Уверенность: {conf}\n"
                    f"EMA5m 9/21: {res['ema5'][0]} / {res['ema5'][1]} | Тренд 1h: {res['ema1h'][0]} / {res['ema1h'][1]}"
                )

                # --- регистрируем позицию для последующего уведомления TP/SL ---
                side = "LONG" if direction=="long" else "SHORT"
                register_signal(res['symbol'], side, res['price'], res['tp'], res['sl'], source="EMA 9/21")

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
    return "Signals v2.9 running (SPOT V2 + TP/SL notifier). UTC: " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

@app.route("/positions")
def positions_view():
    try:
        pos = load_positions()
        opened = {k:v for k,v in pos.items() if v.get("is_open")}
        return {
            "opened": opened,
            "server_time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        }
    except Exception as e:
        return {"error": str(e)}, 500

def start_loop():
    threading.Thread(target=run_loop, daemon=True).start()

if __name__ == "__main__":
    # запускаем монитор TP/SL
    start_closer()
    # запускаем поиск новых сигналов
    start_loop()
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
