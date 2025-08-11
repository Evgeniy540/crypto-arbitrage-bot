# === main.py (Bitget SPOT — autodetect symbol formats) ===
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
CACHE_FILE = "bitget_symbol_cache.json"
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
symbol_cache = load_json(CACHE_FILE, {})   # {desired_key: {"data": "...", "order": "..."}}

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

# ---- products ----
def fetch_products():
    r = _get("/api/spot/v1/public/products")
    if r.get("code") != "00000":
        raise Exception("products error: " + str(r))
    return r.get("data", [])

def products_symbol_for(pair_upper):
    for p in fetch_products():
        sym = (p.get("symbol") or "").upper()
        base = (p.get("baseCoin") or "").upper()
        quote = (p.get("quoteCoin") or "").upper()
        if base + quote == pair_upper:
            return sym
    return None

def probe_ticker(symbol):
    try:
        r = _get("/api/spot/v1/market/ticker", params={"symbol": symbol})
        return r.get("code") == "00000"
    except Exception:
        return False

def detect_forms(pair_upper):
    exact = products_symbol_for(pair_upper)
    candidates = []
    if exact: candidates.append(exact.upper())
    candidates += [pair_upper, to_spbl(pair_upper)]
    tried = []
    for cand in candidates:
        if cand in tried: continue
        tried.append(cand)
        if probe_ticker(cand):
            tg(f"🔎 Формат найден для {pair_upper}: {cand}")
            return {"data": cand, "order": cand}
    tg(f"⚠️ Не удалось подобрать формат для {pair_upper}. Буду использовать {pair_upper} как есть.")
    return {"data": pair_upper, "order": pair_upper}

def get_forms(pair_upper):
    forms = symbol_cache.get(pair_upper)
    if not forms:
        forms = detect_forms(pair_upper)
        symbol_cache[pair_upper] = forms
        save_json(CACHE_FILE, symbol_cache)
    return forms

def get_candles(symbol_data, limit=120):
    r = _get("/api/spot/v1/market/candles",
             params={"symbol": symbol_data, "period": period_str(TIMEFRAME_SEC), "limit": str(max(limit, EMA_SLOW+1))})
    rows = []
    if r.get("code") == "00000":
        rows = r.get("data", [])
    else:
        r2 = _get("/api/spot/v1/market/candles",
                  params={"symbol": symbol_data, "granularity": TIMEFRAME_SEC, "limit": str(max(limit, EMA_SLOW+1))})
        if r2.get("code") == "00000": rows = r2.get("data", [])
        else: raise Exception(f"{r} | {r2}")
    rows.reverse()
    closes = []
    for row in rows:
        if isinstance(row, (list,tuple)) and len(row) > 4:
            v = safe_float(row[4]); 
            if v is not None: closes.append(v)
    return closes

def get_price(symbol_data):
    r = _get("/api/spot/v1/market/ticker", params={"symbol": symbol_data})
    if r.get("code") == "00000":
        d = r.get("data", {})
        p = safe_float(d.get("lastPr") or d.get("last"))
        if p is not None: return p
    r2 = _get("/api/spot/v1/market/tickers", params={"symbol": symbol_data})
    if r2.get("code") == "00000" and r2.get("data"):
        d = r2["data"][0]
        p = safe_float(d.get("lastPr") or d.get("last"))
        if p is not None: return p
    raise Exception(f"no price for {symbol_data}: {r} | {r2}")

def get_balance(coin="USDT"):
    r = _get("/api/spot/v1/account/assets", params={"coin": coin}, auth=True)
    if r.get("code") != "00000": raise Exception(r.get("msg","unknown"))
    arr = r.get("data", [])
    return safe_float(arr[0].get("available")) if arr else 0.0

def try_order(payload):
    r = _post("/api/spot/v1/trade/orders", payload)
    if r.get("code") != "00000":
        raise Exception(r.get("msg","order failed"))
    return r.get("data", {})

def market_buy(pair_upper, forms, quote_usdt):
    candidates = [forms["order"], to_spbl(pair_upper), pair_upper, products_symbol_for(pair_upper) or pair_upper]
    tried=set(); last=None
    for sym in candidates:
        if sym in tried or sym is None: continue
        tried.add(sym)
        payload = {"symbol": sym, "side":"buy", "orderType":"market", "force":"normal",
                   "quoteOrderQty": f"{quote_usdt:.6f}"}
        try:
            data = try_order(payload)
            if sym != forms["order"]:
                forms["order"]=sym; symbol_cache[pair_upper]=forms; save_json(CACHE_FILE, symbol_cache)
                tg(f"🔁 Обновил order-символ для {pair_upper}: {sym}")
            return data
        except Exception as e:
            last=e
    raise last or Exception("order buy failed")

def market_sell(pair_upper, forms, size):
    candidates = [forms["order"], to_spbl(pair_upper), pair_upper, products_symbol_for(pair_upper) or pair_upper]
    tried=set(); last=None
    for sym in candidates:
        if sym in tried or sym is None: continue
        tried.add(sym)
        payload = {"symbol": sym, "side":"sell", "orderType":"market", "force":"normal",
                   "size": f"{size:.8f}"}
        try:
            data = try_order(payload)
            if sym != forms["order"]:
                forms["order"]=sym; symbol_cache[pair_upper]=forms; save_json(CACHE_FILE, symbol_cache)
                tg(f"🔁 Обновил order-символ для {pair_upper}: {sym}")
            return data
        except Exception as e:
            last=e
    raise last or Exception("order sell failed")

SYMS=[]; last_no_signal={}

def ema_signal(symbol_data):
    closes = get_candles(symbol_data, limit=max(EMA_SLOW+10,60))
    if len(closes) < EMA_SLOW+1: return {"signal": None, "reason":"Недостаточно данных"}
    ef=ema(closes, EMA_FAST); es=ema(closes, EMA_SLOW)
    if ef[-1]>es[-1] and ef[-2]<=es[-2]: return {"signal":"LONG","price":closes[-1],"ema":(ef[-1],es[-1])}
    return {"signal": None, "reason":"Нет сигнала", "ema":(ef[-1],es[-1])}

def monitor_positions():
    changed=False
    for pair, pos in list(positions.items()):
        forms=pos["forms"]
        try: price=get_price(forms["data"])
        except Exception as e: logging.warning("price check failed %s: %s", pair, e); continue
        pnl=(price-pos["buy_price"])/pos["buy_price"]
        if pnl>=TP_PCT or pnl<=-SL_PCT:
            side="TP" if pnl>=TP_PCT else "SL"
            try:
                market_sell(pair, forms, pos["qty"])
                pnl_usdt=price*pos["qty"]-pos["spent_usdt"]
                profit["total_usdt"]+=pnl_usdt
                profit["trades"].append({"symbol":forms["data"],"side":side,"buy_price":pos["buy_price"],
                                         "sell_price":price,"qty":pos["qty"],
                                         "pnl_pct":round(pnl*100,4),"pnl_usdt":round(pnl_usdt,6),
                                         "ts_close":int(time.time()*1000)})
                save_json(PROFIT_FILE, profit)
                tg(f"✅ {side} по {pair}\nПродажа ~{price:.6f}\nP/L: {pnl*100:.3f}% ({pnl_usdt:.4f} USDT)\nСумм. прибыль: {profit['total_usdt']:.4f} USDT")
                positions.pop(pair, None); changed=True
            except Exception as e:
                tg(f"❗ Ошибка продажи {pair}: {e}"); logging.error("sell failed %s: %s", pair, e)
    if changed: save_json(POSITIONS_FILE, positions)

def trade_loop():
    global SYMS, last_no_signal
    SYMS=[] 
    for d in DESIRED:
        pair=d.upper()
        forms=get_forms(pair)
        SYMS.append({"pair": pair, "forms": forms})
    last_no_signal={s["pair"]:0 for s in SYMS}
    tg("🤖 Bitget (autodetect) пары:\n" + "\n".join([f"{s['pair']}: data={s['forms']['data']}, order={s['forms']['order']}" for s in SYMS]))

    while True:
        start=time.time()
        try: monitor_positions()
        except Exception as e: logging.error("monitor error: %s", e)

        for S in SYMS:
            pair=S["pair"]; forms=S["forms"]
            try:
                if pair in positions: continue
                sig=ema_signal(forms["data"])
                if sig["signal"]=="LONG":
                    try: usdt=get_balance("USDT")
                    except Exception as e: tg(f"❗ Ошибка баланса USDT: {e}"); continue
                    need=TRADE_AMOUNT_USDT
                    if usdt < need + MIN_BALANCE_BUFFER:
                        tg(f"ℹ️ Недостаточно USDT для {pair}. Баланс: {usdt:.4f}, нужно: {need:.2f}."); continue
                    try:
                        market_buy(pair, forms, need)
                        time.sleep(0.5)
                        price=get_price(forms["data"])
                        est_qty=(need*(1-0.001))/price
                        positions[pair]={"forms":forms,"qty":float(f"{est_qty:.8f}"),
                                         "buy_price":price,"spent_usdt":need,"ts":int(time.time()*1000)}
                        save_json(POSITIONS_FILE, positions)
                        tg(f"🟢 Покупка {pair}\nСумма: {need:.2f} USDT\nЦена ~ {price:.6f}\nEMA9/21: {sig['ema'][0]:.6f} / {sig['ema'][1]:.6f}")
                    except Exception as e:
                        tg(f"❗ Ошибка покупки {pair}: {e}"); logging.error("buy failed %s: %s", pair, e)
                else:
                    now=time.time()
                    if now - last_no_signal.get(pair,0) > NO_SIGNAL_INTERVAL:
                        last_no_signal[pair]=now
                        tg(f"ℹ️ По {pair} нет сигнала. EMA9/21: {sig['ema'][0]:.6f} / {sig['ema'][1]:.6f}")
            except Exception as e:
                logging.error("loop symbol %s error: %s", pair, e)

        time.sleep(max(1, CHECK_INTERVAL - int(time.time()-start)))

# ---- Flask ----
app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "Bitget SPOT bot (autodetect) is running", 200

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
