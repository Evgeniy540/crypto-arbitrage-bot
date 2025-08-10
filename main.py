# === main.py v4.3 (SPOT • fast scalps • TP 0.3% / SL 0.6% • EMA9/21 aggressive • reinvest • one-pos • PnL • selfcheck) ===
import os, time, json, hmac, hashlib, base64, logging, threading, requests
from datetime import datetime, timezone
from urllib.parse import urlencode
from flask import Flask, jsonify

# ----- KEYS (ENV приоритет, иначе — ниже) -----
BITGET_API_KEY        = os.getenv("BITGET_API_KEY",        "bg_ec8a64de58248985f9817cbd3db16977")
BITGET_API_SECRET     = os.getenv("BITGET_API_SECRET",     "b56b8e53af502bee4ba48c7e5eedcf67784526c53075bd1734b7f8ef3381c018")
BITGET_API_PASSPHRASE = os.getenv("BITGET_API_PASSPHRASE", "Evgeniy84")

TELEGRAM_TOKEN        = os.getenv("TELEGRAM_TOKEN",        "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA")
TELEGRAM_CHAT_ID      = os.getenv("TELEGRAM_CHAT_ID",      "5723086631")

# ----- Strategy / Risk -----
SYMBOLS = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","TRXUSDT","DOGEUSDT","PEPEUSDT","BGBUSDT"]
EMA_FAST, EMA_SLOW = 9, 21
G5M = "5min"

TP_PCT = 0.0030    # +0.30%
SL_PCT = 0.0060    # -0.60%

CHECK_EVERY_SEC     = 8
POLL_SECONDS        = 6
PER_SYMBOL_COOLDOWN = 40
GLOBAL_OK_COOLDOWN  = 60*5
MAX_HOLD_MINUTES    = 30

# ----- Sizing (REINVEST ON) -----
TRADE_MODE = os.getenv("TRADE_MODE", "percent")         # "percent" | "fixed"
TRADE_PCT  = float(os.getenv("TRADE_PCT", "0.25"))      # 25% от свободных USDT
MIN_TRADE_USDT = float(os.getenv("MIN_TRADE_USDT", "10.0"))
MAX_TRADE_USDT = float(os.getenv("MAX_TRADE_USDT", "100.0"))
TRADE_USDT     = float(os.getenv("TRADE_USDT", "10.0")) # если fixed

AUTO_TRADE = True
ONLY_ONE_POSITION = True

# ----- Files -----
POSITIONS_FILE = "positions.json"
PNL_FILE       = "profit.json"

# ----- API -----
API_ROOT       = "https://api.bitget.com"
CANDLES_V2     = f"{API_ROOT}/api/v2/spot/market/candles"
TICKERS_V2     = f"{API_ROOT}/api/v2/spot/market/tickers"
SYMBOLS_V2     = f"{API_ROOT}/api/v2/spot/public/symbols"
TICKER_V1_SPOT = f"{API_ROOT}/api/spot/v1/market/ticker"
HEADERS_PUB    = {"User-Agent":"Mozilla/5.0"}

PRICE_FAILS_BEFORE_ALERT = 5
_price_fail_cnt = {}

# ----- Logging / Flask -----
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("spot-bot-v4.3")
app = Flask(__name__)

# ----- Utils -----
def tg_send(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        log.error(f"Telegram error: {e}")

def fmt_price(x: float) -> str:
    if x is None: return "—"
    if x >= 1: return f"{x:.6f}"
    if x >= 0.01: return f"{x:.8f}"
    return f"{x:.10f}"

def pct(x): return f"{x*100:.2f}%"

def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def load_json(path, default):
    if not os.path.exists(path): return default
    try:
        with open(path, "r") as f: return json.load(f)
    except: return default

def to_spbl(symbol: str) -> str:
    return symbol if symbol.endswith("_SPBL") else f"{symbol}_SPBL"

# ----- Sign V2 -----
def _ts_ms() -> str: return str(int(time.time()*1000))
def _sign(ts, method, path, query, body):
    q = "?" + urlencode(sorted([(k, str(v)) for k, v in (query or {}).items()])) if query else ""
    presign = f"{ts}{method.upper()}{path}{q}{body or ''}"
    h = hmac.new(BITGET_API_SECRET.encode(), presign.encode(), hashlib.sha256).digest()
    return base64.b64encode(h).decode()
def _headers(ts, sign):
    return {"ACCESS-KEY":BITGET_API_KEY,"ACCESS-SIGN":sign,"ACCESS-TIMESTAMP":ts,
            "ACCESS-PASSPHRASE":BITGET_API_PASSPHRASE,"Content-Type":"application/json",
            "locale":"en-US","User-Agent":"Mozilla/5.0"}

def _raise_api_error(resp):
    try: j = resp.json()
    except Exception:
        resp.raise_for_status(); return
    code = str(j.get("code",""))
    if code and code != "00000":
        msg = j.get("msg","")
        hint = ""
        if code == "40006": hint = "Invalid ACCESS_KEY"
        elif code == "40005": hint = "Invalid SIGN (SECRET/PASSPHRASE)"
        elif code == "40015": hint = "IP not allowed (whitelist)"
        elif code == "40741": hint = "No spot permission"
        raise RuntimeError(f"Bitget error {code}: {msg}. {hint}".strip())

def priv_get(path, query=None, timeout=12):
    ts=_ts_ms(); sign=_sign(ts,"GET",path,query,None)
    r=requests.get(API_ROOT+path, params=query, headers=_headers(ts,sign), timeout=timeout)
    _raise_api_error(r); return r.json()

def priv_post(path, payload, timeout=12):
    ts=_ts_ms(); body=json.dumps(payload, separators=(",",":"))
    sign=_sign(ts,"POST",path,None,body)
    r=requests.post(API_ROOT+path, data=body, headers=_headers(ts,sign), timeout=timeout)
    _raise_api_error(r); return r.json()

# ----- Indicators -----
def ema(values, period):
    if len(values) < period: return []
    k=2/(period+1); out=[None]*(period-1)
    sma=sum(values[:period])/period; out.append(sma); v=sma
    for x in values[period:]: v = x*k + v*(1-k); out.append(v)
    return out

# ----- Market data -----
def fetch_spot_candles(symbol, granularity, limit=220):
    r=requests.get(CANDLES_V2, params={"symbol":symbol,"granularity":granularity,"limit":str(limit)},
                   headers=HEADERS_PUB, timeout=15)
    r.raise_for_status()
    data=r.json().get("data",[])
    rows=[]
    for row in data:
        try: rows.append((int(row[0]), float(row[4])))
        except: pass
    rows.sort(key=lambda x:x[0])
    return [c for _,c in rows]

def get_last_close_1m(symbol):
    arr = fetch_spot_candles(symbol, "1min", 2)
    return arr[-1] if arr else None

def get_last_price(symbol: str) -> float:
    spbl=to_spbl(symbol)
    for i in range(3):
        try:
            r=requests.get(TICKER_V1_SPOT, params={"symbol":spbl}, headers=HEADERS_PUB, timeout=10)
            r.raise_for_status()
            d=r.json().get("data")
            if isinstance(d,dict) and d.get("last") is not None: return float(d["last"])
        except Exception:
            time.sleep(0.25)
    try:
        r=requests.get(TICKERS_V2, params={"symbol":symbol}, headers=HEADERS_PUB, timeout=10)
        r.raise_for_status()
        d=r.json().get("data")
        if isinstance(d, list) and d and d[0].get("last") is not None: return float(d[0]["last"])
        if isinstance(d, dict) and d.get("last") is not None: return float(d["last"])
    except Exception:
        pass
    c=get_last_close_1m(symbol)
    if c is not None: return float(c)
    raise RuntimeError(f"Нет последней цены для {symbol}")

# ----- Symbols config -----
_symbol_cfg={}
def load_symbol_cfg():
    global _symbol_cfg
    r=requests.get(SYMBOLS_V2, headers=HEADERS_PUB, timeout=15)
    r.raise_for_status()
    arr=r.json().get("data",[])
    _symbol_cfg={d["symbol"]:d for d in arr if "symbol" in d}
    log.info(f"[INIT] symbols cfg = {len(_symbol_cfg)}")

def min_usdt(symbol): return float((_symbol_cfg.get(symbol) or {}).get("minTradeUSDT","1"))
def quote_precision(symbol): return int((_symbol_cfg.get(symbol) or {}).get("quotePrecision","8"))
def quantity_precision(symbol): return int((_symbol_cfg.get(symbol) or {}).get("quantityPrecision","6"))
def qfmt(symbol, x, kind):
    prec = quote_precision(symbol) if kind=="quote" else quantity_precision(symbol)
    return f"{x:.{prec}f}"

# ----- Account & Trading -----
def get_usdt_available() -> float:
    j = priv_get("/api/v2/spot/account/assets", {"coin":"USDT"})
    arr = j.get("data") or []
    return float(arr[0].get("available","0")) if arr else 0.0

def place_market_buy(symbol, spend_usdt, tries=3):
    payload = {"symbol":symbol,"side":"buy","orderType":"market",
               "size": qfmt(symbol, spend_usdt, "quote"),
               "clientOid": f"buy-{symbol}-{int(time.time()*1000)}"}
    last_err=None
    for i in range(tries):
        try:
            res = priv_post("/api/v2/spot/trade/place-order", payload)
            if res.get("code") != "00000": raise RuntimeError(res)
            oid = (res.get("data") or {}).get("orderId")
            time.sleep(0.6)
            info = priv_get("/api/v2/spot/trade/orderInfo", {"orderId": oid, "symbol": symbol})
            od = info.get("data") or {}
            return {"orderId": oid, "baseQty": float(od.get("baseVolume","0")),
                    "avgPrice": float(od.get("priceAvg","0") or "0")}
        except Exception as e:
            last_err=e; time.sleep(0.5*(i+1))
    raise RuntimeError(f"Buy failed after {tries} tries: {last_err}")

def place_market_sell(symbol, qty_base, tries=3):
    payload = {"symbol":symbol,"side":"sell","orderType":"market",
               "size": qfmt(symbol, qty_base, "base"),
               "clientOid": f"sell-{symbol}-{int(time.time()*1000)}"}
    last_err=None
    for i in range(tries):
        try:
            res = priv_post("/api/v2/spot/trade/place-order", payload)
            if res.get("code") != "00000": raise RuntimeError(res)
            oid = (res.get("data") or {}).get("orderId")
            time.sleep(0.6)
            info = priv_get("/api/v2/spot/trade/orderInfo", {"orderId": oid, "symbol": symbol})
            od = info.get("data") or {}
            return {"orderId": oid, "quoteVolume": float(od.get("quoteVolume","0")),
                    "avgPrice": float(od.get("priceAvg","0") or "0")}
        except Exception as e:
            last_err=e; time.sleep(0.5*(i+1))
    raise RuntimeError(f"Sell failed after {tries} tries: {last_err}")

def price_levels(price):
    return float(price*(1+TP_PCT)), float(price*(1-SL_PCT))

# ----- Positions & PnL -----
def load_positions(): return load_json(POSITIONS_FILE, {})
def save_positions(d): save_json(POSITIONS_FILE, d)

def any_position_open(pos: dict) -> bool:
    return any(v.get("is_open") for v in pos.values())

def register_signal(symbol, entry, tp, sl):
    pos=load_positions()
    if ONLY_ONE_POSITION and any_position_open(pos): return
    if symbol in pos and pos[symbol].get("is_open"): return
    pos[symbol] = {"is_open": True, "symbol":symbol, "side":"LONG",
                   "entry": float(entry), "tp": float(tp), "sl": float(sl),
                   "opened_at": datetime.utcnow().isoformat(timespec="seconds"),
                   "orderId_buy": None, "baseQty": None}
    save_positions(pos)

def pnl_append(record: dict):
    data = load_json(PNL_FILE, {"trades":[], "total_pct":0.0})
    data["trades"].append(record)
    data["total_pct"] = round(data.get("total_pct",0.0) + record.get("pl_pct",0.0), 6)
    save_json(PNL_FILE, data)

# ----- Aggressive signal: cross OR trend-up -----
def ema_signal_aggressive(sym):
    closes = fetch_spot_candles(sym, G5M, 220)
    if len(closes) < EMA_SLOW+2: return None
    e9, e21 = ema(closes, EMA_FAST), ema(closes, EMA_SLOW)
    f_prev, s_prev, f_cur, s_cur = e9[-2], e21[-2], e9[-1], e21[-1]
    price = closes[-1]
    if any(v is None for v in (f_prev, s_prev, f_cur, s_cur)): return None
    bull_cross = (f_prev <= s_prev) and (f_cur > s_cur)
    trend_up   = (f_cur > s_cur) and (price > s_cur)
    if not (bull_cross or trend_up): return None
    tp, sl = price_levels(price)
    return {"symbol":sym, "price":float(price), "tp":float(tp), "sl":float(sl),
            "ema":(round(f_cur,6), round(s_cur,6)),
            "mode":"cross" if bull_cross else "trend"}

# ----- Sizing (reinvest) -----
def calc_spend_usdt(symbol: str) -> float:
    if TRADE_MODE == "percent":
        avail = get_usdt_available()
        spend = max(MIN_TRADE_USDT, avail * TRADE_PCT)
        spend = min(spend, MAX_TRADE_USDT)
        spend = max(spend, min_usdt(symbol))
        return float(spend)
    return float(max(TRADE_USDT, min_usdt(symbol)))

# ----- Buy flow -----
def try_autobuy(symbol, price_hint, tp_hint, sl_hint):
    if not AUTO_TRADE: return
    pos = load_positions()
    if ONLY_ONE_POSITION and any_position_open(pos): return
    spend = calc_spend_usdt(symbol)
    try:
        bal = get_usdt_available()
    except Exception as e:
        tg_send(f"⚠️ Ошибка баланса: {e}"); return
    if bal < spend:
        tg_send(f"⚠️ Недостаточно USDT ({bal:.2f}) для покупки {symbol} на {spend:.2f} USDT"); return
    try:
        info = place_market_buy(symbol, spend)
        base_qty  = float(info["baseQty"])
        avg_price = float(info["avgPrice"]) or float(price_hint)
    except Exception as e:
        tg_send(f"❌ Покупка не выполнена {symbol}: {e}"); return
    tp_new, sl_new = price_levels(avg_price)
    pos[symbol] = {"is_open": True, "symbol":symbol, "side":"LONG",
                   "entry": float(avg_price), "tp": float(tp_new), "sl": float(sl_new),
                   "opened_at": datetime.utcnow().isoformat(timespec="seconds"),
                   "orderId_buy": info["orderId"], "baseQty": base_qty}
    save_positions(pos)
    tg_send(f"🟢 BUY {symbol}\nСумма: {spend:.2f} USDT → {base_qty:.8f}\n"
            f"Средняя: {fmt_price(avg_price)} | TP: {fmt_price(tp_new)} ({pct(TP_PCT)}) | SL: {fmt_price(sl_new)} ({pct(SL_PCT)})")

# ----- Close loop -----
_last_watch = {}
def _should_watch(sym):
    now=time.time(); last=_last_watch.get(sym,0)
    if now-last>=60: _last_watch[sym]=now; return True
    return False

def check_positions_once():
    pos = load_positions(); changed=False
    items = [(k,v) for k,v in pos.items() if v.get("is_open")]
    if ONLY_ONE_POSITION and items: items = [items[0]]

    for symbol, p in items:
        entry, tp, sl = float(p["entry"]), float(p["tp"]), float(p["sl"])
        reason=None; price=None
        # timeout
        try:
            opened_dt = datetime.fromisoformat(p["opened_at"])
            age_min = (datetime.utcnow() - opened_dt).total_seconds()/60
            if age_min >= MAX_HOLD_MINUTES: reason = "⏱️ TIMEOUT"
        except: pass

        if reason is None:
            try:
                price = get_last_price(symbol); _price_fail_cnt[symbol]=0
            except Exception as e:
                cnt=_price_fail_cnt.get(symbol,0)+1; _price_fail_cnt[symbol]=cnt
                if cnt % PRICE_FAILS_BEFORE_ALERT == 0:
                    tg_send(f"⚠️ Не удаётся получить цену {symbol} уже {cnt} раз.")
                log.warning(f"[PRICE] {symbol}: fail({cnt}): {e}")

        if reason is None and price is not None:
            if price >= tp: reason="✅ TP"
            elif price <= sl: reason="❌ SL"

        if _should_watch(symbol) and price is not None:
            log.info(f"[WATCH] {symbol} price={fmt_price(price)} entry={fmt_price(entry)} TP={fmt_price(tp)} SL={fmt_price(sl)}")

        if reason is None: continue

        # SELL market
        sell_info=None
        if AUTO_TRADE and p.get("baseQty"):
            try:
                sell_info = place_market_sell(symbol, float(p["baseQty"]))
                if sell_info.get("avgPrice"): price=float(sell_info["avgPrice"])
            except Exception as e:
                log.error(f"[SELL ERR] {symbol}: {e}")
                tg_send(f"⚠️ Ошибка продажи {symbol}: {e}")

        p["is_open"]=False
        p["closed_at"]=datetime.utcnow().isoformat(timespec="seconds")
        if price is not None:
            p["close_price"]=price
            pl_pct = (price-entry)/entry*100.0
        else:
            pl_pct = 0.0
        pos[symbol]=p; save_positions(pos); changed=True

        pnl_append({
            "symbol": symbol, "opened_at": p["opened_at"], "closed_at": p["closed_at"],
            "entry": entry, "close": price, "pl_pct": round(pl_pct, 5)
        })

        tg_send(f"{reason} по {symbol}\nЦена закрытия: {fmt_price(price)}\nP/L: {pl_pct:.3f}%")
        log.info(f"[CLOSE] {symbol} {reason} P/L={pl_pct:.3f}%")

    if changed: save_positions(pos)

def check_positions_loop():
    while True:
        try: check_positions_once()
        except Exception as e: log.error(f"check_positions_loop error: {e}")
        time.sleep(POLL_SECONDS)

def start_closer():
    open_pos=[k for k,v in load_positions().items() if v.get("is_open")]
    log.info(f"[INIT] Открытые при старте: {', '.join(open_pos) if open_pos else 'нет'}")
    threading.Thread(target=check_positions_loop, daemon=True).start()

# ----- Signals loop -----
last_signal_side = {s: None for s in SYMBOLS}
last_signal_ts   = {s: 0 for s in SYMBOLS}
last_no_signal_sent=0

def run_loop():
    global last_no_signal_sent
    tg_send("🤖 v4.3 запущен. SPOT. Агрессивные входы. TP+0.30% / SL-0.60%. One-position. Reinvest ON.")
    try: load_symbol_cfg()
    except Exception as e: log.error(f"symbols cfg error: {e}")

    # sanity
    for s in SYMBOLS:
        try:
            closes = fetch_spot_candles(s, G5M, 60)
            log.info(f"{s}: свечей(5m)={len(closes)}")
        except Exception as e:
            log.error(f"{s} start fetch error: {e}")

    while True:
        try:
            any_signal=False
            pos=load_positions()
            if ONLY_ONE_POSITION and any_position_open(pos):
                now = time.time()
                if now - last_no_signal_sent >= GLOBAL_OK_COOLDOWN:
                    last_no_signal_sent = now
                    tg_send("ℹ️ Позиция открыта, мониторю TP/SL…")
                time.sleep(CHECK_EVERY_SEC); continue

            for sym in SYMBOLS:
                res = ema_signal_aggressive(sym)
                if not res: continue
                now=time.time()
                if last_signal_side.get(sym)=="long" and (now-last_signal_ts.get(sym,0)<PER_SYMBOL_COOLDOWN):
                    continue
                last_signal_side[sym]="long"; last_signal_ts[sym]=now; any_signal=True

                tg_send(
                    f"🟢 LONG сигнал {res['symbol']} ({res['mode']})\n"
                    f"Цена: ~ {fmt_price(res['price'])}\n"
                    f"TP: {fmt_price(res['tp'])} ({pct(TP_PCT)}) | SL: {fmt_price(res['sl'])} ({pct(SL_PCT)})\n"
                    f"EMA5m 9/21: {res['ema'][0]} / {res['ema'][1]}"
                )
                register_signal(res['symbol'], res['price'], res['tp'], res['sl'])
                try_autobuy(res['symbol'], res['price'], res['tp'], res['sl'])
                break  # one-position: первый сигнал — берём и выходим

            now=time.time()
            if not any_signal and now-last_no_signal_sent>=GLOBAL_OK_COOLDOWN:
                last_no_signal_sent=now; tg_send("ℹ️ Пока без новых сигналов. Сканирую рынок…")
        except Exception as e:
            log.exception(f"Loop error: {e}")
        time.sleep(CHECK_EVERY_SEC)

# ----- Flask -----
@app.route("/")
def home():
    return "Signals v4.3 (SPOT) running. UTC: " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

@app.route("/positions")
def positions_view():
    try:
        pos = load_positions(); opened = {k:v for k,v in pos.items() if v.get("is_open")}
        return jsonify({"opened": opened, "server_time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/panic-sell/<symbol>")
def panic_sell(symbol):
    pos = load_positions(); p = pos.get(symbol)
    if not p or not p.get("is_open") or not p.get("baseQty"):
        return jsonify({"ok": False, "msg":"Нет открытой позиции"}), 400
    try:
        info = place_market_sell(symbol, float(p["baseQty"]))
        price = float(info.get("avgPrice") or 0)
        entry = float(p.get("entry", 0))
        pl_pct = ((price - entry)/entry*100.0) if entry else 0.0
        p["is_open"]=False
        p["closed_at"]=datetime.utcnow().isoformat(timespec="seconds")
        p["close_price"]=price
        pos[symbol]=p; save_positions(pos)
        pnl_append({"symbol":symbol,"opened_at":p["opened_at"],"closed_at":p["closed_at"],
                    "entry":entry,"close":price,"pl_pct":round(pl_pct,5)})
        tg_send(f"🛑 PANIC SELL {symbol}: {fmt_price(price)} | P/L {pl_pct:.3f}%")
        return jsonify({"ok": True, "orderId": info.get("orderId")})
    except Exception as e:
        tg_send(f"❌ PANIC SELL ERROR {symbol}: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/selfcheck")
def selfcheck():
    try:
        j = priv_get("/api/v2/spot/account/assets", {"coin":"USDT"})
        arr = j.get("data") or []
        avail = float(arr[0].get("available","0")) if arr else 0.0
        return jsonify({"ok": True, "usdt_available": avail})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/pnl")
def pnl_view():
    data = load_json(PNL_FILE, {"trades":[], "total_pct":0.0})
    return jsonify(data)

def start_loop():
    threading.Thread(target=run_loop, daemon=True).start()

if __name__ == "__main__":
    threading.Thread(target=check_positions_loop, daemon=True).start()
    start_loop()
    port = int(os.environ.get("PORT","8000"))
    app.run(host="0.0.0.0", port=port)
