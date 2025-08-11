# === main.py (Bitget SPOT — final: plain for data, *_SPBL for orders, safe) ===
import os, time, hmac, hashlib, base64, json, threading, logging
from flask import Flask
import requests

# -------- KEYS --------
BITGET_API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"

TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

# -------- SETTINGS --------
DESIRED = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","TRXUSDT"]
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

# ---- helpers ----
def ema(series, period):
    k = 2/(period+1); e=None; out=[]
    for v in series:
        e = v if e is None else v*k + e*(1-k)
        out.append(e)
    return out

def period_str(sec):
    m=int(sec/60)
    return {1:"1min",3:"3min",5:"5min",15:"15min",30:"30min",60:"1hour",240:"4hour",1440:"1day"}.get(m,"5min")

def safe_float(x):
    try: return float(x)
    except Exception: return None

def to_plain(sym: str) -> str:
    return sym.replace("_SPBL","")

def to_spbl(sym: str) -> str:
    return sym if sym.endswith("_SPBL") else (sym + "_SPBL")

# ---- products resolver ----
def fetch_products():
    r = _get("/api/spot/v1/public/products")
    if r.get("code") != "00000":
        raise Exception("products error: " + str(r))
    return r.get("data", [])

def resolve_symbols(desired):
    prods = fetch_products()
    by_symbol = {(p.get("symbol","") or "").upper(): p for p in prods}
    by_pair = {((p.get("baseCoin","")+p.get("quoteCoin","")).upper()): p for p in prods}
    out = []
    missed = []
    for want in desired:
        key = want.upper()
        p = by_symbol.get(key) or by_pair.get(key)
        if p and p.get("symbol"):
            sym = (p["symbol"] or "").upper()
            out.append({"plain": to_plain(sym), "spbl": to_spbl(sym)})
        else:
            out.append({"plain": key, "spbl": to_spbl(key)})
            missed.append(want)
    if missed:
        tg("⚠️ Не нашёл пары в products, использую по умолчанию: " + ", ".join(missed))
    return out

# ---- market data (plain) ----
def get_candles(plain, limit=120):
    r = _get("/api/spot/v1/market/candles",
             params={"symbol": plain, "period": period_str(TIMEFRAME_SEC), "limit": str(max(limit, EMA_SLOW+1))})
    rows = []
    if r.get("code") == "00000":
        rows = r.get("data", [])
    else:
        r2 = _get("/api/spot/v1/market/candles",
                  params={"symbol": plain, "granularity": TIMEFRAME_SEC, "limit": str(max(limit, EMA_SLOW+1))})
        if r2.get("code") == "00000":
            rows = r2.get("data", [])
        else:
            raise Exception(f"{r} | {r2}")
    rows.reverse()
    closes = []
    for row in rows:
        if isinstance(row, (list,tuple)) and len(row) > 4:
            v = safe_float(row[4])
            if v is not None:
                closes.append(v)
    return closes

def get_price(plain):
    r = _get("/api/spot/v1/market/ticker", params={"symbol": plain})
    if r.get("code") == "00000":
        d = r.get("data", {})
        p = safe_float(d.get("lastPr") or d.get("last"))
        if p is not None: return p
    r2 = _get("/api/spot/v1/market/tickers", params={"symbol": plain})
    if r2.get("code") == "00000" and r2.get("data"):
        d = r2["data"][0]
        p = safe_float(d.get("lastPr") or d.get("last"))
        if p is not None: return p
    raise Exception(f"no price for {plain}: {r} | {r2}")

def get_balance(coin="USDT"):
    r = _get("/api/spot/v1/account/assets", params={"coin": coin}, auth=True)
    if r.get("code") != "00000": raise Exception(r.get("msg","unknown"))
    arr = r.get("data", [])
    return safe_float(arr[0].get("available")) if arr else 0.0

# ---- orders (spbl) ----
def market_buy(spbl, quote_usdt):
    payload = {"symbol": spbl, "side": "buy", "orderType": "market", "force": "normal",
               "quoteOrderQty": f"{quote_usdt:.6f}"}
    r = _post("/api/spot/v1/trade/orders", payload)
    if r.get("code") != "00000": raise Exception(r.get("msg","order buy failed"))
    return r.get("data", {})

def market_sell(spbl, size):
    payload = {"symbol": spbl, "side": "sell", "orderType": "market", "force": "normal",
               "size": f"{size:.8f}"}
    r = _post("/api/spot/v1/trade/orders", payload)
    if r.get("code") != "00000": raise Exception(r.get("msg","order sell failed"))
    return r.get("data", {})

# ---- strategy ----
SYMBOLS = []  # list of {'plain','spbl'}
last_no_signal = {}

def ema_signal(plain):
    closes = get_candles(plain, limit=max(EMA_SLOW+10,60))
    if len(closes) < EMA_SLOW+1:
        return {"signal": None, "reason": "Недостаточно данных"}
    ef = ema(closes, EMA_FAST); es = ema(closes, EMA_SLOW)
    if ef[-1] > es[-1] and ef[-2] <= es[-2]:
        return {"signal": "LONG", "price": closes[-1], "ema": (ef[-1], es[-1])}
    return {"signal": None, "reason": "Нет сигнала", "ema": (ef[-1], es[-1])}

def monitor_positions():
    changed = False
    for plain, pos in list(positions.items()):
        try:
            price = get_price(plain)
        except Exception as e:
            logging.warning("price check failed %s: %s", plain, e)
            continue
        pnl = (price - pos["buy_price"]) / pos["buy_price"]
        if pnl >= TP_PCT or pnl <= -SL_PCT:
            side = "TP" if pnl >= TP_PCT else "SL"
            try:
                market_sell(pos["spbl"], pos["qty"])
                pnl_usdt = price * pos["qty"] - pos["spent_usdt"]
                profit["total_usdt"] += pnl_usdt
                profit["trades"].append({
                    "symbol": plain,
                    "side": side,
                    "buy_price": pos["buy_price"],
                    "sell_price": price,
                    "qty": pos["qty"],
                    "pnl_pct": round(pnl * 100, 4),
                    "pnl_usdt": round(pnl_usdt, 6),
                    "ts_close": int(time.time() * 1000)
                })
                save_json(PROFIT_FILE, profit)
                tg(f"✅ {side} по {plain}\nПродажа ~{price:.6f}\nP/L: {pnl*100:.3f}% ({pnl_usdt:.4f} USDT)\nСумм. прибыль: {profit['total_usdt']:.4f} USDT")
                positions.pop(plain, None)
                changed = True
            except Exception as e:
                tg(f"❗ Ошибка продажи {plain}: {e}")
                logging.error("sell failed %s: %s", plain, e)
    if changed:
        save_json(POSITIONS_FILE, positions)

def trade_loop():
    global SYMBOLS, last_no_signal
    SYMBOLS = resolve_symbols(DESIRED)
    if not SYMBOLS:
        tg("❗ Не нашёл ни одной пары."); return
    last_no_signal = {s['plain']:0 for s in SYMBOLS}
    tg("🤖 Бот запущен. Пары: " + ", ".join([f"{s['plain']}|{s['spbl']}" for s in SYMBOLS]))

    # self-test
    for S in SYMBOLS:
        p = S['plain']; sp = S['spbl']
        try:
            price = get_price(p)
            _ = get_candles(p, 30)
            tg(f"✅ Self-test {p}: OK (ticker {price})")
        except Exception as e:
            tg(f"⚠️ Self-test {p}: {e} (будут пропуски сигналов)")

    while True:
        start = time.time()
        try:
            monitor_positions()
        except Exception as e:
            logging.error("monitor error: %s", e)

        for S in SYMBOLS:
            plain, spbl = S['plain'], S['spbl']
            try:
                if plain in positions: 
                    continue
                sig = ema_signal(plain)
                if sig["signal"] == "LONG":
                    try:
                        usdt = get_balance("USDT")
                    except Exception as e:
                        tg(f"❗ Ошибка баланса USDT: {e}"); continue
                    need = TRADE_AMOUNT_USDT
                    if usdt < need + MIN_BALANCE_BUFFER:
                        tg(f"ℹ️ Недостаточно USDT для {plain}. Баланс: {usdt:.4f}, нужно: {need:.2f}."); continue
                    try:
                        market_buy(spbl, need)
                        time.sleep(0.5)
                        price = get_price(plain)
                        est_qty = (need * (1 - 0.001)) / price
                        positions[plain] = {"spbl": spbl, "qty": float(f"{est_qty:.8f}"),
                                            "buy_price": price, "spent_usdt": need,
                                            "ts": int(time.time()*1000)}
                        save_json(POSITIONS_FILE, positions)
                        tg(f"🟢 Покупка {plain}\nСумма: {need:.2f} USDT\nЦена ~ {price:.6f}\nEMA9/21: {sig['ema'][0]:.6f} / {sig['ema'][1]:.6f}")
                    except Exception as e:
                        tg(f"❗ Ошибка покупки {plain}: {e}"); logging.error("buy failed %s: %s", plain, e)
                else:
                    now = time.time()
                    if now - last_no_signal.get(plain, 0) > NO_SIGNAL_INTERVAL:
                        last_no_signal[plain] = now
                        tg(f"ℹ️ По {plain} сейчас нет сигнала. EMA9/21: {sig['ema'][0]:.6f} / {sig['ema'][1]:.6f}")
            except Exception as e:
                logging.error("loop symbol %s error: %s", plain, e)

        time.sleep(max(1, CHECK_INTERVAL - int(time.time() - start)))

# ---- Flask ----
app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "Bitget SPOT bot (final dual-safe) is running", 200

@app.route("/profit", methods=["GET"])
def profit_status():
    p = load_json(PROFIT_FILE, {"total_usdt": 0.0, "trades": []})
    return {"total_usdt": p.get("total_usdt", 0.0), "trades": p.get("trades", [])}, 200

def run_flask():
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    t = threading.Thread(target=trade_loop, daemon=True)
    t.start()
    run_flask()
