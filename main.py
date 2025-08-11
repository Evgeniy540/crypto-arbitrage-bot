# === main_bitget_spot_quantity_fix.py ===
import os, time, hmac, hashlib, base64, json, threading, logging, uuid
from flask import Flask
import requests

# --- Keys (from previous run) ---
BITGET_API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"

TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

BASE_URL = "https://api.bitget.com"

# --- Strategy ---
PAIRS = ["BTCUSDT","ETHUSDT","SOLUSDT","TRXUSDT","XRPUSDT"]
TRADE_AMOUNT_USDT = 10.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

def tg(text):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        logging.error("Telegram: %s", e)

def _ts(): return str(int(time.time()*1000))
def _sign(ts, method, path, body=""):
    msg=f"{ts}{method}{path}{body}"
    mac = hmac.new(BITGET_API_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
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
    url=BASE_URL+path; params=params or {}
    if auth:
        from urllib.parse import urlencode
        qs="?" + urlencode(params) if params else ""
        r=requests.get(url+qs, headers=_headers("GET", path+qs, ""), timeout=20)
    else:
        r=requests.get(url, params=params, timeout=20)
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

# --- Products cache with precision ---
PRODUCTS={"by_plain":{}, "ts":0}
def refresh_products():
    j=_get("/api/spot/v1/public/products")
    mp={}
    for d in (j.get("data") or []):
        try:
            plain = (d.get("baseCoin","")+d.get("quoteCoin","")).upper()
            mp[plain] = {
                "symbol": (d.get("symbol") or "").upper(),
                "minTradeAmount": float(d.get("minTradeAmount") or 0.0),
                "quantityPrecision": int(d.get("quantityPrecision") or 6),
                "pricePrecision": int(d.get("pricePrecision") or 6),
            }
        except Exception:
            continue
    if mp:
        PRODUCTS["by_plain"]=mp; PRODUCTS["ts"]=time.time()
        tg("ð Products loaded: " + ", ".join([mp[p]["symbol"] for p in PAIRS if p in mp]))
    else:
        tg("â Can't load products: " + str(j)[:240])

def product(plain):
    if time.time()-PRODUCTS["ts"]>3600 or not PRODUCTS["by_plain"]:
        refresh_products()
    return PRODUCTS["by_plain"].get(plain)

def get_price(plain):
    info=product(plain)
    sym = info["symbol"] if info else plain
    j=_get("/api/spot/v1/market/ticker", params={"symbol": sym})
    if j.get("code")=="00000":
        d=j.get("data",{})
        for k in ("lastPr","close","last"): 
            v=d.get(k)
            if v is not None:
                try: return float(v)
                except: pass
    raise Exception(f"No price for {plain}: {j}")

def quantize(q, prec):
    s=f"{{:.{prec}f}}".format(q)
    # strip trailing zeros
    if "." in s: s=s.rstrip("0").rstrip(".")
    return s

def market_buy_quantity(plain, usdt):
    info=product(plain)
    if not info: raise Exception(f"No product meta for {plain}")
    sym=info["symbol"]
    qprec=info["quantityPrecision"]; minAmt=info["minTradeAmount"] or 0.0
    price=get_price(plain)
    # compute quantity from USDT and enforce minTradeAmount (notional)
    qty = max(usdt/price, (minAmt/price)*1.05)
    # round DOWN to precision to avoid 'over precision' errors
    step = 10**(-qprec)
    qty = (int(qty/step))*step
    if qty<=0:
        raise Exception(f"Computed qty=0 (usdt={usdt}, price={price}, qprec={qprec})")
    payload={
        "symbol": sym,
        "side": "buy",
        "orderType": "market",
        "force": "normal",
        "clientOid": str(uuid.uuid4()),
        "quantity": quantize(qty, qprec)
    }
    r=_post("/api/spot/v1/trade/orders", payload)
    if r.get("code")!="00000":
        raise Exception(f"order resp: {r}")
    return r.get("data",{})

# --- tiny runner just to test buy path from logs ---
def run_once_test():
    refresh_products()
    for p in PAIRS:
        try:
            market_buy_quantity(p, TRADE_AMOUNT_USDT)
            tg(f"ð¢ TEST BUY {p} OK")
            break
        except Exception as e:
            tg(f"â TEST BUY {p} failed: {e}")
            continue

# Flask so Render reports 200
from flask import Flask
app=Flask(__name__)
@app.route("/")
def home(): return "bitget quantity fix alive", 200

if __name__=="__main__":
    # comment the test. Use in your main trading loop instead of old market_buy_dual
    # run_once_test()
    port=int(os.getenv("PORT","8000")); 
    app.run(host="0.0.0.0", port=port)
