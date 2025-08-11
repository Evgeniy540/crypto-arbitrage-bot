# === main.py (Bitget SPOT, SPBL-only, robust candles with time window) ===
import os, time, hmac, hashlib, base64, json, threading, logging
from flask import Flask
import requests
from datetime import datetime, timedelta, timezone

# -------- KEYS --------
BITGET_API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"

TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

# -------- SETTINGS --------
SYMBOLS = ["BTCUSDT_SPBL","ETHUSDT_SPBL","SOLUSDT_SPBL","TRXUSDT_SPBL","XRPUSDT_SPBL"]
TIMEFRAME_SEC = 300   # 5m
EMA_FAST = 9
EMA_SLOW = 21
TP_PCT = 0.015
SL_PCT = 0.010
CHECK_INTERVAL = 30
NO_SIGNAL_INTERVAL = 3600
TRADE_AMOUNT_USDT = 10.0
MIN_BALANCE_BUFFER = 0.5

POSITIONS_FILE = "positions.json"
PROFIT_FILE = "profit.json"
LOG_LEVEL = "INFO"

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s | %(levelname)s | %(message)s")

BASE_URL = "https://api.bitget.com"

# -------- Utils --------
def _ts(): return str(int(time.time()*1000))

def _sign(timestamp, method, path, body=""):
    msg = f"{timestamp}{method}{path}{body}"
    mac = hmac.new(BITGET_API_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.b64encode(mac).decode()

def _headers(method, path, body=""):
    ts = _ts()
    return {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": _sign(ts, method, path, body),
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json",
        "locale": "en-US"
    }

def _get(path, params=None, auth=False):
    url = BASE_URL + path
    params = params or {}
    if auth:
        from urllib.parse import urlencode
        qs = "?" + urlencode(params) if params else ""
        headers = _headers("GET", path + qs, "")
        r = requests.get(url + qs, headers=headers, timeout=20)
    else:
        r = requests.get(url, params=params, timeout=20)
    if r.status_code != 200:
        raise Exception(f"HTTP {r.status_code}: {r.text}")
    return r.json()

def _post(path, payload):
    url = BASE_URL + path
    body = json.dumps(payload, separators=(',',':'))
    headers = _headers("POST", path, body)
    r = requests.post(url, headers=headers, data=body, timeout=20)
    if r.status_code != 200:
        raise Exception(f"HTTP {r.status_code}: {r.text}")
    return r.json()

def tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        logging.error("Telegram error: %s", e)

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except: return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)

positions = load_json(POSITIONS_FILE, {})
profit = load_json(PROFIT_FILE, {"total_usdt": 0.0, "trades": []})

def ensure_spbl(sym: str) -> str:
    sym = str(sym or "").upper()
    return sym if sym.endswith("_SPBL") else f"{sym}_SPBL"

def safe_float(x):
    try: return float(x)
    except Exception: return None

# -------- Indicators --------
def ema(series, period):
    k = 2.0/(period+1.0)
    e = None
    out = []
    for v in series:
        e = v if e is None else v*k + e*(1.0-k)
        out.append(e)
    return out

def period_str(sec):
    m = int(sec/60)
    return {1:"1min",3:"3min",5:"5min",15:"15min",30:"30min",60:"1hour",240:"4hour",1440:"1day"}.get(m,"5min")

# -------- Robust candles with window & retries --------
def _ms(dt):  # datetime -> ms
    return int(dt.timestamp() * 1000)

def get_candles(symbol_spbl, limit=EMA_SLOW+60):
    symbol_spbl = ensure_spbl(symbol_spbl)
    need = max(limit, EMA_SLOW + 1)
    # Build a 7-day lookback window to be safe
    now = datetime.now(timezone.utc)
    start = now - timedelta(seconds=need * TIMEFRAME_SEC * 2)  # x2 margin
    start_ms = _ms(start)
    end_ms = _ms(now)

    # Try 1: period + limit only
    params1 = {"symbol": symbol_spbl, "period": period_str(TIMEFRAME_SEC), "limit": str(need)}
    # Try 2: granularity + limit
    params2 = {"symbol": symbol_spbl, "granularity": TIMEFRAME_SEC, "limit": str(need)}
    # Try 3: period + window
    params3 = {"symbol": symbol_spbl, "period": period_str(TIMEFRAME_SEC),
               "startTime": str(start_ms), "endTime": str(end_ms)}
    # Try 4: granularity + window
    params4 = {"symbol": symbol_spbl, "granularity": TIMEFRAME_SEC,
               "startTime": str(start_ms), "endTime": str(end_ms)}

    tries = [params1, params2, params3, params4]
    rows = None; last_err = None
    for pr in tries:
        try:
            r = _get("/api/spot/v1/market/candles", params=pr)
            if r.get("code") == "00000":
                rows = r.get("data", [])
                if isinstance(rows, list) and len(rows) > 0:
                    break
        except Exception as e:
            last_err = e
    if not rows:
        raise Exception(f"Empty candles for {symbol_spbl}. last_err={last_err}")

    rows.reverse()  # old -> new
    closes = []
    for row in rows:
        if isinstance(row, (list,tuple)) and len(row) > 4:
            v = safe_float(row[4])
            if v is not None:
                closes.append(v)
    if len(closes) < EMA_SLOW+1:
        raise Exception(f"Too few candles for {symbol_spbl}: {len(closes)}")
    return closes

def get_price(symbol_spbl):
    symbol_spbl = ensure_spbl(symbol_spbl)
    r = _get("/api/spot/v1/market/ticker", params={"symbol": symbol_spbl})
    if r.get("code") == "00000":
        d = r.get("data", {})
        p = safe_float(d.get("lastPr") or d.get("last"))
        if p is not None: return p
    r2 = _get("/api/spot/v1/market/tickers", params={"symbol": symbol_spbl})
    if r2.get("code") == "00000" and r2.get("data"):
        d = r2["data"][0]
        p = safe_float(d.get("lastPr") or d.get("last"))
        if p is not None: return p
    raise Exception(f"No price for {symbol_spbl}: {r} | {r2}")

def get_balance(coin="USDT"):
    r = _get("/api/spot/v1/account/assets", params={"coin": coin}, auth=True)
    if r.get("code") != "00000":
        raise Exception(r.get("msg","unknown"))
    arr = r.get("data", [])
    return safe_float(arr[0].get("available")) if arr else 0.0

def market_buy(symbol_spbl, quote_usdt):
    payload = {"symbol": ensure_spbl(symbol_spbl), "side":"buy", "orderType":"market", "force":"normal",
               "quoteOrderQty": f"{quote_usdt:.6f}"}
    r = _post("/api/spot/v1/trade/orders", payload)
    if r.get("code") != "00000":
        raise Exception(r.get("msg","order buy failed"))
    return r.get("data", {})

def market_sell(symbol_spbl, size):
    payload = {"symbol": ensure_spbl(symbol_spbl), "side":"sell", "orderType":"market", "force":"normal",
               "size": f"{size:.8f}"}
    r = _post("/api/spot/v1/trade/orders", payload)
    if r.get("code") != "00000":
        raise Exception(r.get("msg","order sell failed"))
    return r.get("data", {})

# -------- Strategy --------
last_no_signal = {}

def ema_signal(symbol_spbl):
    closes = get_candles(symbol_spbl, limit=EMA_SLOW+60)
    ef = ema(closes, EMA_FAST)
    es = ema(closes, EMA_SLOW)
    if ef[-1] > es[-1] and ef[-2] <= es[-2]:
        return {"signal":"LONG","price":closes[-1],"ema":(ef[-1],es[-1])}
    return {"signal":None,"reason":"ÐÐµÑ ÑÐ¸Ð³Ð½Ð°Ð»Ð°","ema":(ef[-1],es[-1])}

def monitor_positions():
    changed = False
    for sym, pos in list(positions.items()):
        try:
            price = get_price(sym)
        except Exception as e:
            logging.warning("price check failed %s: %s", sym, e)
            continue
        pnl = (price - pos["buy_price"]) / pos["buy_price"]
        if pnl >= TP_PCT or pnl <= -SL_PCT:
            side = "TP" if pnl >= TP_PCT else "SL"
            try:
                market_sell(sym, pos["qty"])
                pnl_usdt = price*pos["qty"] - pos["spent_usdt"]
                profit["total_usdt"] += pnl_usdt
                profit["trades"].append({
                    "symbol": sym, "side": side, "buy_price": pos["buy_price"],
                    "sell_price": price, "qty": pos["qty"],
                    "pnl_pct": round(pnl*100,4), "pnl_usdt": round(pnl_usdt,6),
                    "ts_close": int(time.time()*1000)
                })
                save_json(PROFIT_FILE, profit)
                tg(f"â {side} Ð¿Ð¾ {sym}\nÐÑÐ¾Ð´Ð°Ð¶Ð° ~{price:.6f}\nP/L: {pnl*100:.3f}% ({pnl_usdt:.4f} USDT)\nÐ¡ÑÐ¼Ð¼. Ð¿ÑÐ¸Ð±ÑÐ»Ñ: {profit['total_usdt']:.4f} USDT")
                positions.pop(sym, None); changed=True
            except Exception as e:
                tg(f"â ÐÑÐ¸Ð±ÐºÐ° Ð¿ÑÐ¾Ð´Ð°Ð¶Ð¸ {sym}: {e}")
                logging.error("sell failed %s: %s", sym, e)
    if changed:
        save_json(POSITIONS_FILE, positions)

def run_loop():
    global last_no_signal
    last_no_signal = {s:0 for s in SYMBOLS}
    tg("ð¤ Bitget SPOT (SPBL-only) Ð·Ð°Ð¿ÑÑÐµÐ½. ÐÐ°ÑÑ: " + ", ".join(SYMBOLS))

    # quick self-test
    for s in SYMBOLS:
        try:
            p = get_price(s)
            _ = get_candles(s, EMA_SLOW+30)
            tg(f"â Self-test {s}: last={p}")
        except Exception as e:
            tg(f"â ï¸ Self-test {s}: {e}")

    while True:
        start = time.time()
        try:
            monitor_positions()
        except Exception as e:
            logging.error("monitor error: %s", e)

        for sym in SYMBOLS:
            try:
                if sym in positions:
                    continue
                try:
                    sig = ema_signal(sym)
                except Exception as e:
                    # Not enough/empty data â log once per hour
                    now = time.time()
                    if now - last_no_signal.get(sym, 0) > NO_SIGNAL_INTERVAL:
                        last_no_signal[sym] = now
                        tg(f"â¹ï¸ ÐÑÐ¾Ð¿ÑÑÐº {sym}: {e}")
                    continue

                if sig["signal"] == "LONG":
                    try:
                        usdt = get_balance("USDT")
                    except Exception as e:
                        tg(f"â ÐÑÐ¸Ð±ÐºÐ° Ð±Ð°Ð»Ð°Ð½ÑÐ° USDT: {e}")
                        continue
                    need = TRADE_AMOUNT_USDT
                    if usdt < need + MIN_BALANCE_BUFFER:
                        tg(f"â¹ï¸ ÐÐµÐ´Ð¾ÑÑÐ°ÑÐ¾ÑÐ½Ð¾ USDT Ð´Ð»Ñ {sym}. ÐÐ°Ð»Ð°Ð½Ñ: {usdt:.6f}, Ð½ÑÐ¶Ð½Ð¾: {need:.2f}.")
                        continue
                    try:
                        market_buy(sym, need)
                        time.sleep(0.5)
                        price = get_price(sym)
                        est_qty = (need * (1 - 0.001)) / price
                        positions[sym] = {
                            "qty": float(f"{est_qty:.8f}"),
                            "buy_price": price,
                            "spent_usdt": need,
                            "ts": int(time.time()*1000)
                        }
                        save_json(POSITIONS_FILE, positions)
                        tg(f"ð¢ ÐÐ¾ÐºÑÐ¿ÐºÐ° {sym}\nÐ¡ÑÐ¼Ð¼Ð°: {need:.2f} USDT\nÐ¦ÐµÐ½Ð° ~ {price:.6f}\nEMA9/21: {sig['ema'][0]:.6f} / {sig['ema'][1]:.6f}")
                    except Exception as e:
                        tg(f"â ÐÑÐ¸Ð±ÐºÐ° Ð¿Ð¾ÐºÑÐ¿ÐºÐ¸ {sym}: {e}")
                        logging.error("buy failed %s: %s", sym, e)
                else:
                    now = time.time()
                    if now - last_no_signal.get(sym, 0) > NO_SIGNAL_INTERVAL:
                        last_no_signal[sym] = now
                        tg(f"â¹ï¸ ÐÐ¾ {sym} Ð½ÐµÑ ÑÐ¸Ð³Ð½Ð°Ð»Ð°. EMA9/21: {sig['ema'][0]:.6f} / {sig['ema'][1]:.6f}")
            except Exception as e:
                logging.error("loop symbol %s error: %s", sym, e)

        sleep_left = CHECK_INTERVAL - int(time.time() - start)
        if sleep_left > 0: time.sleep(sleep_left)

# ---- Flask ----
app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "Bitget SPOT bot (SPBL-only, robust candles) is running", 200

@app.route("/profit", methods=["GET"])
def profit_status():
    p = load_json(PROFIT_FILE, {"total_usdt": 0.0, "trades": []})
    return {"total_usdt": p.get("total_usdt", 0.0), "trades": p.get("trades", [])}, 200

def run_flask():
    port = int(os.getenv("PORT","8000"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    t = threading.Thread(target=run_loop, daemon=True)
    t.start()
    run_flask()
