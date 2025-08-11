# === main.py (Bitget SPOT — DATA=PLAIN, ORDERS=SPBL, verbose candle logs) ===
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
SYMBOLS_SPBL = ["BTCUSDT_SPBL","ETHUSDT_SPBL","SOLUSDT_SPBL","TRXUSDT_SPBL","XRPUSDT_SPBL"]
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
    return r

def _get_json(path, params=None, auth=False):
    r = _get(path, params=params, auth=auth)
    try:
        j = r.json()
    except Exception:
        j = {"code": str(r.status_code), "raw": r.text[:400]}
    if r.status_code != 200:
        j.setdefault("http", r.status_code)
    return j

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

def ensure_spbl(sym): 
    sym = str(sym or "").upper()
    return sym if sym.endswith("_SPBL") else f"{sym}_SPBL"

def to_plain(sym_spbl: str) -> str:
    return str(sym_spbl or "").upper().replace("_SPBL","")

def safe_float(x):
    try: return float(x)
    except Exception: return None

def ema(series, period):
    k = 2.0/(period+1.0)
    e=None; out=[]
    for v in series:
        e = v if e is None else v*k + e*(1.0-k)
        out.append(e)
    return out

def period_str(sec):
    m=int(sec/60)
    return {1:"1min",3:"3min",5:"5min",15:"15min",30:"30min",60:"1hour",240:"4hour",1440:"1day"}.get(m,"5min")

# ---- DATA uses PLAIN symbol only ----
def get_candles(symbol_spbl, limit=EMA_SLOW+60):
    plain = to_plain(symbol_spbl)
    need = max(limit, EMA_SLOW+1)
    now = datetime.now(timezone.utc)
    start_ms = int((now - timedelta(seconds=need*TIMEFRAME_SEC*2)).timestamp()*1000)
    end_ms   = int(now.timestamp()*1000)

    # 1) period string
    j = _get_json("/api/spot/v1/market/candles",
                  params={"symbol": plain, "period": period_str(TIMEFRAME_SEC), "limit": str(need)})
    data = j.get("data", [])
    if not (isinstance(data, list) and len(data)>0):
        # 2) granularity seconds
        j = _get_json("/api/spot/v1/market/candles",
                      params={"symbol": plain, "granularity": TIMEFRAME_SEC, "limit": str(need)})
        data = j.get("data", [])
    if not (isinstance(data, list) and len(data)>0):
        # 3) history endpoint with window
        j = _get_json("/api/spot/v1/market/history-candles",
                      params={"symbol": plain, "granularity": TIMEFRAME_SEC, "startTime": str(start_ms), "endTime": str(end_ms)})
        data = j.get("data", [])
    if not (isinstance(data, list) and len(data)>0):
        tg(f"❗ Raw candle resp for {plain}: {str(j)[:200]}")
        raise Exception(f"No candles for {plain}")

    # parse
    rows = list(data); rows.reverse()
    closes = []
    for row in rows:
        if isinstance(row,(list,tuple)) and len(row)>4:
            v = safe_float(row[4])
            if v is not None: closes.append(v)
        elif isinstance(row, dict):
            v = safe_float(row.get('close') or row.get('c'))
            if v is not None: closes.append(v)
    if len(closes) < EMA_SLOW+1:
        tg(f"❗ Too few parsed candles for {plain}: {len(closes)} (raw-count={len(rows)})")
        raise Exception(f"Too few candles for {plain}: {len(closes)}")
    return closes

def get_price(symbol_spbl):
    # price via PLAIN first (more stable), fallback to SPBL
    plain = to_plain(symbol_spbl)
    j = _get_json("/api/spot/v1/market/ticker", params={"symbol": plain})
    if j.get("code") == "00000":
        d=j.get("data",{}); p=safe_float(d.get("lastPr") or d.get("last"))
        if p is not None: return p
    j2 = _get_json("/api/spot/v1/market/ticker", params={"symbol": ensure_spbl(symbol_spbl)})
    if j2.get("code") == "00000":
        d=j2.get("data",{}); p=safe_float(d.get("lastPr") or d.get("last"))
        if p is not None: return p
    raise Exception(f"No price for {plain}: {j} | {j2}")

def get_balance(coin="USDT"):
    j = _get_json("/api/spot/v1/account/assets", params={"coin": coin}, auth=True)
    if j.get("code") != "00000": raise Exception(j.get("msg","unknown"))
    arr=j.get("data", [])
    return safe_float(arr[0].get("available")) if arr else 0.0

def market_buy(symbol_spbl, quote_usdt):
    payload={"symbol": ensure_spbl(symbol_spbl), "side":"buy","orderType":"market","force":"normal",
             "quoteOrderQty": f"{quote_usdt:.6f}"}
    j=_post("/api/spot/v1/trade/orders", payload)
    if j.get("code") != "00000": raise Exception(j.get("msg","order buy failed"))
    return j.get("data", {})

def market_sell(symbol_spbl, size):
    payload={"symbol": ensure_spbl(symbol_spbl), "side":"sell","orderType":"market","force":"normal",
             "size": f"{size:.8f}"}
    j=_post("/api/spot/v1/trade/orders", payload)
    if j.get("code") != "00000": raise Exception(j.get("msg","order sell failed"))
    return j.get("data", {})

# ---- Strategy ----
def ema_signal(symbol_spbl):
    closes = get_candles(symbol_spbl, limit=EMA_SLOW+60)
    ef=ema(closes, EMA_FAST); es=ema(closes, EMA_SLOW)
    if ef[-1] > es[-1] and ef[-2] <= es[-2]:
        return {"signal":"LONG","price":closes[-1],"ema":(ef[-1],es[-1])}
    return {"signal":None,"reason":"Нет сигнала","ema":(ef[-1],es[-1])}

last_no_signal={}

def monitor_positions():
    changed=False
    for sym, pos in list(positions.items()):
        try: price=get_price(sym)
        except Exception as e: logging.warning("price check failed %s: %s", sym, e); continue
        pnl=(price-pos["buy_price"])/pos["buy_price"]
        if pnl>=TP_PCT or pnl<=-SL_PCT:
            side="TP" if pnl>=TP_PCT else "SL"
            try:
                market_sell(sym, pos["qty"])
                pnl_usdt=price*pos["qty"]-pos["spent_usdt"]
                profit["total_usdt"]+=pnl_usdt
                profit["trades"].append({
                    "symbol": sym, "side": side, "buy_price": pos["buy_price"],
                    "sell_price": price, "qty": pos["qty"],
                    "pnl_pct": round(pnl*100,4), "pnl_usdt": round(pnl_usdt,6),
                    "ts_close": int(time.time()*1000)
                })
                save_json(PROFIT_FILE, profit)
                tg(f"✅ {side} по {sym}\nПродажа ~{price:.6f}\nP/L: {pnl*100:.3f}% ({pnl_usdt:.4f} USDT)\nСумм. прибыль: {profit['total_usdt']:.4f} USDT")
                positions.pop(sym, None); changed=True
            except Exception as e:
                tg(f"❗ Ошибка продажи {sym}: {e}"); logging.error("sell failed %s: %s", sym, e)
    if changed: save_json(POSITIONS_FILE, positions)

def run_loop():
    global last_no_signal
    last_no_signal={s:0 for s in SYMBOLS_SPBL}
    tg("🤖 Bitget SPOT (data=PLAIN, orders=SPBL). Пары: " + ", ".join(SYMBOLS_SPBL))

    for s in SYMBOLS_SPBL:
        try:
            p=get_price(s); _=get_candles(s, EMA_SLOW+30)
            tg(f"✅ Self-test {s}: last={p}")
        except Exception as e:
            tg(f"⚠️ Self-test {s}: {e}")

    while True:
        start=time.time()
        try: monitor_positions()
        except Exception as e: logging.error("monitor error: %s", e)

        for sym in SYMBOLS_SPBL:
            try:
                if sym in positions: continue
                try:
                    sig=ema_signal(sym)
                except Exception as e:
                    now=time.time()
                    if now - last_no_signal.get(sym,0) > NO_SIGNAL_INTERVAL:
                        last_no_signal[sym]=now
                        tg(f"ℹ️ Пропуск {sym}: {e}")
                    continue
                if sig["signal"]=="LONG":
                    try: usdt=get_balance("USDT")
                    except Exception as e: tg(f"❗ Ошибка баланса USDT: {e}"); continue
                    need=TRADE_AMOUNT_USDT
                    if usdt < need + MIN_BALANCE_BUFFER:
                        tg(f"ℹ️ Недостаточно USDT для {sym}. Баланс: {usdt:.6f}, нужно: {need:.2f}."); continue
                    try:
                        market_buy(sym, need)
                        time.sleep(0.5)
                        price=get_price(sym)
                        est_qty=(need*(1-0.001))/price
                        positions[sym]={"qty": float(f"{est_qty:.8f}"),
                                        "buy_price": price, "spent_usdt": need,
                                        "ts": int(time.time()*1000)}
                        save_json(POSITIONS_FILE, positions)
                        tg(f"🟢 Покупка {sym}\nСумма: {need:.2f} USDT\nЦена ~ {price:.6f}\nEMA9/21: {sig['ema'][0]:.6f} / {sig['ema'][1]:.6f}")
                    except Exception as e:
                        tg(f"❗ Ошибка покупки {sym}: {e}"); logging.error("buy failed %s: %s", sym, e)
                else:
                    now=time.time()
                    if now - last_no_signal.get(sym,0) > NO_SIGNAL_INTERVAL:
                        last_no_signal[sym]=now
                        tg(f"ℹ️ По {sym} нет сигнала. EMA9/21: {sig['ema'][0]:.6f} / {sig['ema'][1]:.6f}")
            except Exception as e:
                logging.error("loop symbol %s error: %s", sym, e)

        sleep_left=CHECK_INTERVAL - int(time.time()-start)
        if sleep_left>0: time.sleep(sleep_left)

# ---- Flask ----
app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "Bitget SPOT bot (DATA=PLAIN, ORDERS=SPBL) is running", 200

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
