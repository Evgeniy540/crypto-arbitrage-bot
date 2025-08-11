# === main.py — Bitget SPOT (soft EMA, TP/SL, cooldown, quantity-buy) ===
import os, time, json, hmac, hashlib, base64, uuid, threading, logging
import requests
from flask import Flask

# ====== KEYS ======
BITGET_API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"

TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

# ====== SETTINGS ======
PAIRS = ["BTCUSDT","ETHUSDT","SOLUSDT","TRXUSDT","XRPUSDT"]  # plain form
TRADE_AMOUNT_USDT = 10.0
TIMEFRAME_SEC = 300           # 5m
EMA_FAST = 9
EMA_SLOW = 21
THRESHOLD_PCT = 0.0005        # 0.05% — мягкое условие EMA9>EMA21
CONFIRM_BARS  = 2             # сколько баров подряд для мягкого условия
CROSS_LOOKBACK = 3            # перескок EMA9/EMA21 в последних N барах
TP_PCT = 0.015                # +1.5%
SL_PCT = 0.010                # -1.0%
ENTRY_COOLDOWN_SEC = 900      # 15 минут на повторный вход по символу
CHECK_INTERVAL = 30
NO_SIGNAL_INTERVAL = 3600     # как часто слать "нет сигнала"
MIN_BALANCE_BUFFER = 0.5      # запас к USDT

# ====== FILES ======
POSITIONS_FILE = "positions.json"
PROFIT_FILE    = "profit.json"
ENTRY_STATE_FILE = "entry_state.json"

LOG_LEVEL = "INFO"
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s | %(levelname)s | %(message)s")

BASE_URL = "https://api.bitget.com"

# ---------- Helpers ----------
def tg(text):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        logging.error("Telegram error: %s", e)

def _ts(): return str(int(time.time()*1000))
def _sign(ts, method, path, body=""):
    mac = hmac.new(BITGET_API_SECRET.encode(), f"{ts}{method}{path}{body}".encode(), hashlib.sha256).digest()
    return base64.b64encode(mac).decode()

def _headers(method, path, body=""):
    ts=_ts()
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
        r = requests.get(url+qs, headers=_headers("GET", path+qs, ""), timeout=20)
    else:
        r = requests.get(url, params=params, timeout=20)
    try: j=r.json()
    except: j={"code":str(r.status_code), "raw": r.text[:400]}
    if r.status_code!=200: j.setdefault("http", r.status_code)
    return j

def _post(path, payload):
    body=json.dumps(payload, separators=(',',':'))
    r=requests.post(BASE_URL+path, headers=_headers("POST", path, body), data=body, timeout=20)
    try: j=r.json()
    except: j={"code":str(r.status_code), "raw": r.text[:400]}
    if r.status_code!=200: j.setdefault("http", r.status_code)
    return j

def load_json(path, default):
    try:
        with open(path,"r",encoding="utf-8") as f: return json.load(f)
    except: return default
def save_json(path, data):
    with open(path,"w",encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)

positions  = load_json(POSITIONS_FILE, {})
profit     = load_json(PROFIT_FILE, {"total_usdt":0.0, "trades":[]})
entry_state= load_json(ENTRY_STATE_FILE, {})  # {"BTCUSDT":{"last_entry_ts":...}}

# ---------- Products & precision ----------
PRODUCTS = {"map":{}, "ts":0}

def refresh_products():
    j=_get("/api/spot/v1/public/products")
    mp={}
    for d in (j.get("data") or []):
        try:
            plain = (d.get("baseCoin","")+d.get("quoteCoin","")).upper()
            mp[plain] = {
                "symbol": (d.get("symbol") or "").upper(),
                "quantityPrecision": int(d.get("quantityPrecision") or 6),
                "pricePrecision": int(d.get("pricePrecision") or 6),
                "minTradeAmount": float(d.get("minTradeAmount") or 0.0)
            }
        except Exception:
            continue
    if mp:
        PRODUCTS["map"]=mp; PRODUCTS["ts"]=time.time()
        tg("🔎 Bitget products loaded: " + ", ".join([mp.get(p,{}).get("symbol","?") for p in PAIRS]))
    else:
        tg("❗ Failed to load products: " + str(j)[:220])

def prod(plain):
    if time.time()-PRODUCTS["ts"]>3600 or not PRODUCTS["map"]:
        refresh_products()
    return PRODUCTS["map"].get(plain)

def period_str(sec):
    m=int(sec/60)
    return {1:"1min",3:"3min",5:"5min",15:"15min",30:"30min",60:"1hour",240:"4hour",1440:"1day"}.get(m,"5min")

def get_price(plain):
    p=prod(plain)
    sym=p["symbol"] if p else plain
    j=_get("/api/spot/v1/market/ticker", params={"symbol": sym})
    if j.get("code")=="00000":
        d=j.get("data",{})
        for k in ("lastPr","close","last"):
            v=d.get(k)
            if v is not None:
                try: return float(v)
                except: pass
    raise Exception(f"No price for {plain}: {j}")

def get_candles(plain, limit=EMA_SLOW+60):
    p=prod(plain)
    sym=p["symbol"] if p else plain
    need=max(limit, EMA_SLOW+1, 50)
    j=_get("/api/spot/v1/market/candles",
           params={"symbol": sym, "period": period_str(TIMEFRAME_SEC), "limit": str(min(200,need))})
    data=j.get("data", [])
    if not (isinstance(data, list) and data):
        j=_get("/api/spot/v1/market/history-candles",
               params={"symbol": sym, "granularity": TIMEFRAME_SEC, "limit": str(min(200,need))})
        data=j.get("data", [])
    if not (isinstance(data, list) and data):
        tg(f"❗ Raw candle resp for {sym}: {str(j)[:220]}")
        raise Exception(f"No candles for {sym}")
    rows=list(data); rows.reverse()
    closes=[]
    for row in rows:
        if isinstance(row,(list,tuple)) and len(row)>4:
            try: closes.append(float(row[4]))
            except: pass
        elif isinstance(row, dict):
            v=row.get("close") or row.get("c")
            try: closes.append(float(v))
            except: pass
    if len(closes)<EMA_SLOW+1:
        raise Exception(f"Too few candles for {sym}: {len(closes)}")
    return closes

def ema(series, period):
    k=2.0/(period+1.0); e=None; out=[]
    for v in series:
        e=v if e is None else v*k + e*(1.0-k)
        out.append(e)
    return out

# ---------- Signals ----------
def ema_signal(plain):
    closes=get_candles(plain, limit=max(EMA_SLOW+60, 100))
    ef=ema(closes, EMA_FAST); es=ema(closes, EMA_SLOW)
    # (a) перескок
    cross=False
    for i in range(1, min(CROSS_LOOKBACK+1, len(ef))):
        if ef[-i] > es[-i] and ef[-i-1] <= es[-i-1]:
            cross=True; break
    # (b) мягкое условие
    ok=True
    for i in range(1, CONFIRM_BARS+1):
        gap=(ef[-i]-es[-i]) / es[-i]
        if gap < THRESHOLD_PCT: ok=False; break
    if cross or ok:
        return {"signal":"LONG","ema":(ef[-1],es[-1]),"why":"cross" if cross else "gap"}
    return {"signal":None,"ema":(ef[-1],es[-1])}

# ---------- Trading ----------
def quantize(q, prec):
    s=f"{{:.{prec}f}}".format(q)
    if "." in s: s=s.rstrip("0").rstrip(".")
    return s

def get_balance(coin="USDT"):
    j=_get("/api/spot/v1/account/assets", params={"coin": coin}, auth=True)
    if j.get("code")!="00000": return 0.0
    arr=j.get("data", [])
    try: return float(arr[0].get("available")) if arr else 0.0
    except: return 0.0

def market_buy_quantity(plain, usdt):
    """Place market buy using quantity. If Bitget returns 45110 (min notional),
    we retry once with the required minimum notional parsed from the error text."""
    meta=prod(plain)
    if not meta: raise Exception(f"No product meta for {plain}")
    sym=meta["symbol"]; qprec=meta["quantityPrecision"]; minAmt=float(meta.get("minTradeAmount") or 0.0)
    price=get_price(plain)
    qty=max(usdt/price, (minAmt/price)*1.05)
    # округляем вниз до шага
    step=10**(-qprec)
    qty=(int(qty/step))*step
    if qty<=0:
        raise Exception(f"Computed qty=0 (usdt={usdt}, price={price})")
    payload={
        "symbol": sym,
        "side": "buy",
        "orderType": "market",
        "force": "normal",
        "clientOid": str(uuid.uuid4()),
        "quantity": quantize(qty, qprec)
    }
    r=_post("/api/spot/v1/trade/orders", payload)
    if r.get("code")=="00000":
        return r.get("data",{}), qty
    # handle min notional: {'code':'45110','msg':'less than the minimum amount 1 USDT'}
    if r.get("code")=="45110":
        import re
        m=re.search(r"([0-9]+(?:\.[0-9]+)?)\s*USDT", str(r))
        need=float(m.group(1)) if m else float(meta.get("minTradeAmount") or 1.0)
        notional=max(usdt, need*1.05)
        price=get_price(plain)
        step=10**(-qprec)
        qty=(int((notional/price)/step))*step
        if qty<=0:
            raise Exception(f"min retry computed qty=0 (need={need}, price={price})")
        payload["quantity"]=quantize(qty, qprec)
        payload["clientOid"]=str(uuid.uuid4())
        r2=_post("/api/spot/v1/trade/orders", payload)
        if r2.get("code")=="00000":
            tg(f"↩️ Retry with min notional {notional:.4f} USDT accepted for {sym}")
            return r2.get("data",{}), qty
        raise Exception(f"retry failed: {r2}")
    raise Exception(f"{r}")

def market_sell_all(plain, qty):
    meta=prod(plain); sym=meta["symbol"]; qprec=meta["quantityPrecision"]
    payload={
        "symbol": sym, "side":"sell","orderType":"market","force":"normal",
        "clientOid": str(uuid.uuid4()), "quantity": quantize(qty, qprec)
    }
    r=_post("/api/spot/v1/trade/orders", payload)
    if r.get("code")!="00000": raise Exception(f"{r}")
    return r.get("data",{})

def can_enter(sym):
    st=entry_state.get(sym,{}); last=st.get("last_entry_ts",0)
    return time.time()-last >= ENTRY_COOLDOWN_SEC
def mark_enter(sym):
    entry_state.setdefault(sym,{})["last_entry_ts"]=time.time()
    save_json(ENTRY_STATE_FILE, entry_state)

def monitor_positions():
    changed=False
    for sym, pos in list(positions.items()):
        try: price=get_price(sym)
        except Exception as e: logging.warning("price fail %s: %s", sym, e); continue
        pnl=(price-pos["buy_price"])/pos["buy_price"]
        if pnl>=TP_PCT or pnl<=-SL_PCT:
            side="TP" if pnl>=TP_PCT else "SL"
            try:
                market_sell_all(sym, pos["qty"])
                pnl_usdt = price*pos["qty"] - pos["spent_usdt"]
                profit["total_usdt"] += pnl_usdt
                profit["trades"].append({
                    "symbol": sym, "side": side, "buy_price": pos["buy_price"],
                    "sell_price": price, "qty": pos["qty"],
                    "pnl_pct": round(pnl*100,4), "pnl_usdt": round(pnl_usdt,6),
                    "ts_close": int(time.time()*1000)
                })
                save_json(PROFIT_FILE, profit)
                positions.pop(sym, None); changed=True
                tg(f"✅ {side} по {sym}\nP/L: {pnl*100:.3f}% ({pnl_usdt:.4f} USDT)")
            except Exception as e:
                tg(f"❗ Ошибка продажи {sym}: {e}")
    if changed: save_json(POSITIONS_FILE, positions)

def run_loop():
    refresh_products()
    tg("🤖 Bitget SPOT запущен (soft EMA, quantity-buy). Пары: " + ", ".join([prod(p)["symbol"] for p in PAIRS if prod(p)]))
    # self-test
    for p in PAIRS:
        try:
            pr=get_price(p); cls=get_candles(p, EMA_SLOW+30)
            tg(f"✅ Self-test {p} ({prod(p)['symbol']}): last={pr}, candles={len(cls)}")
        except Exception as e:
            tg(f"⚠️ Self-test {p}: {e}")
    # loop
    last_info={s:0 for s in PAIRS}
    while True:
        t0=time.time()
        try: monitor_positions()
        except Exception as e: logging.error("monitor: %s", e)
        for p in PAIRS:
            try:
                if p in positions or not can_enter(p): 
                    continue
                try:
                    sig=ema_signal(p)
                except Exception as e:
                    now=time.time()
                    if now-last_info[p]>NO_SIGNAL_INTERVAL:
                        last_info[p]=now; tg(f"ℹ️ Пропуск {p}: {e}")
                    continue
                if sig["signal"]=="LONG":
                    bal=get_balance("USDT")
                    if bal < TRADE_AMOUNT_USDT + MIN_BALANCE_BUFFER:
                        continue
                    try:
                        resp, qty = market_buy_quantity(p, TRADE_AMOUNT_USDT)
                        price=get_price(p)
                        positions[p]={"qty": float(f"{qty:.8f}"), "buy_price": price,
                                      "spent_usdt": TRADE_AMOUNT_USDT, "ts": int(time.time()*1000)}
                        save_json(POSITIONS_FILE, positions); mark_enter(p)
                        tg(f"🟢 Покупка {p} ({prod(p)['symbol']}) [{sig.get('why')}]\nСумма: {TRADE_AMOUNT_USDT:.2f} USDT\nЦена~{price:.6f}\nEMA9/21: {sig['ema'][0]:.6f} / {sig['ema'][1]:.6f}")
                    except Exception as e:
                        tg(f"❗ Ошибка покупки {p}: {e}")
                else:
                    now=time.time()
                    if now-last_info[p]>NO_SIGNAL_INTERVAL:
                        last_info[p]=now
                        tg(f"ℹ️ По {p} нет сигнала. EMA9/21: {sig['ema'][0]:.6f} / {sig['ema'][1]:.6f}")
            except Exception as e:
                logging.error("loop %s: %s", p, e)
        sleep=CHECK_INTERVAL - int(time.time()-t0)
        if sleep>0: time.sleep(sleep)

# ====== Flask (health) ======
app=Flask(__name__)

@app.route("/")
def home(): return "Bitget SPOT bot — running", 200

@app.route("/profit")
def profit_status(): return load_json(PROFIT_FILE, {"total_usdt":0.0,"trades":[]}), 200

def run_flask():
    port=int(os.getenv("PORT","8000"))
    app.run(host="0.0.0.0", port=port)

if __name__=="__main__":
    t=threading.Thread(target=run_loop, daemon=True); t.start()
    run_flask()
