  # === main.py v3.5 (AGGRESSIVE + auto-sell fix) ===
# Bitget SPOT V2 • EMA 9/21 cross (5m) only • LONG-only AUTOTRADE
# TP +0.5% / SL -0.4% • price via *_SPBL with retries • frequent checks
# Recalc TP/SL from actual filled price • /panic-sell/<symbol>

import os, time, threading, logging, requests, json, hmac, hashlib, base64
from datetime import datetime, timezone
from urllib.parse import urlencode
from flask import Flask

# ====== KEYS (по твоей просьбе вставлены прямо в код; для прод — ENV) ======
BITGET_API_KEY        = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET     = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"
TELEGRAM_TOKEN        = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID      = "5723086631"

# ====== STRATEGY ======
SYMBOLS = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","TRXUSDT","DOGEUSDT","PEPEUSDT","BGBUSDT"]
G5M = "5min"
EMA_FAST, EMA_SLOW = 9, 21
TP_PCT = 0.005    # +0.5%
SL_PCT = 0.004    # -0.4%

# Частые проверки
CHECK_EVERY_SEC     = 15     # цикл сигналов
POLL_SECONDS        = 8      # монитор TP/SL
PER_SYMBOL_COOLDOWN = 60*3   # 3 минуты
GLOBAL_OK_COOLDOWN  = 60*20

# ====== AUTOTRADE ======
AUTO_TRADE   = True
TRADE_USDT   = 10.0
POSITIONS_FILE = "positions.json"
TRADES_FILE    = "trades.json"

# ====== HTTP/API ======
HEADERS_PUB = {"User-Agent":"Mozilla/5.0"}
API_ROOT       = "https://api.bitget.com"
CANDLES_V2     = f"{API_ROOT}/api/v2/spot/market/candles"
TICKERS_V2     = f"{API_ROOT}/api/v2/spot/market/tickers"
SYMBOLS_V2     = f"{API_ROOT}/api/v2/spot/public/symbols"
TICKER_V1_SPOT = f"{API_ROOT}/api/spot/v1/market/ticker"

# Alerts if price endpoint keeps failing
PRICE_FAILS_BEFORE_ALERT = 5
_price_fail_cnt = {}

# ====== LOGGING/FLASK ======
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("signals-v3.5")
app = Flask(__name__)

# ====== HELPERS ======
def tg_send(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        log.error(f"Telegram error: {e}")

def pct(x): return f"{x*100:.2f}%"

def fmt_price(x: float) -> str:
    if x >= 1: return f"{x:.6f}"
    elif x >= 0.01: return f"{x:.8f}"
    else: return f"{x:.10f}"

def load_json(path: str, default):
    if not os.path.exists(path): return default
    try:
        with open(path, "r") as f: return json.load(f)
    except: return default

def save_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def to_spbl(symbol: str) -> str:
    return symbol if symbol.endswith("_SPBL") else f"{symbol}_SPBL"

# ====== PRIVATE API SIGN ======
def _ts_ms() -> str: return str(int(time.time() * 1000))

def _sign(ts: str, method: str, path: str, query: dict|None, body: str|None) -> str:
    q = ""
    if query: q = "?" + urlencode(sorted([(k, str(v)) for k, v in query.items()]))
    presign = f"{ts}{method.upper()}{path}{q}{body or ''}"
    h = hmac.new(BITGET_API_SECRET.encode(), presign.encode(), hashlib.sha256).digest()
    return base64.b64encode(h).decode()

def _auth_headers(ts: str, sign: str) -> dict:
    return {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json",
        "locale": "en-US",
        "User-Agent": "Mozilla/5.0"
    }

def priv_get(path: str, query: dict|None=None, timeout=12):
    ts = _ts_ms(); sign = _sign(ts, "GET", path, query, None)
    r = requests.get(API_ROOT + path, params=query, headers=_auth_headers(ts, sign), timeout=timeout)
    r.raise_for_status(); return r.json()

def priv_post(path: str, payload: dict, timeout=12):
    ts = _ts_ms(); body = json.dumps(payload, separators=(",", ":"))
    sign = _sign(ts, "POST", path, None, body)
    r = requests.post(API_ROOT + path, data=body, headers=_auth_headers(ts, sign), timeout=timeout)
    r.raise_for_status(); return r.json()

# ====== INDICATORS ======
def ema(values, period):
    if len(values) < period: return []
    k = 2/(period+1); out = [None]*(period-1)
    sma = sum(values[:period])/period; out.append(sma); v = sma
    for x in values[period:]:
        v = x*k + v*(1-k); out.append(v)
    return out

# ====== MARKET DATA ======
def fetch_spot_candles(symbol: str, granularity: str, limit: int = 300):
    r = requests.get(CANDLES_V2,
                     params={"symbol": symbol, "granularity": granularity, "limit": str(limit)},
                     headers=HEADERS_PUB, timeout=15)
    r.raise_for_status()
    data = r.json().get("data", [])
    rows = []
    for row in data:
        try:
            rows.append((int(row[0]), float(row[4])))
        except: pass
    rows.sort(key=lambda x: x[0])
    closes = [c for _,c in rows]
    return closes, None

def get_last_price(symbol: str) -> float:
    # V1 spot ticker with *_SPBL (3 tries) -> fallback V2
    spbl = to_spbl(symbol)
    for i in range(3):
        try:
            r = requests.get(TICKER_V1_SPOT, params={"symbol": spbl}, headers=HEADERS_PUB, timeout=10)
            r.raise_for_status()
            last = (r.json().get("data") or {}).get("last")
            if last is not None:
                return float(last)
        except Exception as e:
            log.warning(f"[PRICE V1] {symbol} try {i+1}/3: {e}")
        time.sleep(0.35)
    try:
        r = requests.get(TICKERS_V2, params={"symbol": symbol}, headers=HEADERS_PUB, timeout=10)
        r.raise_for_status()
        d = r.json().get("data")
        if isinstance(d, list) and d and d[0].get("last") is not None:
            return float(d[0]["last"])
        if isinstance(d, dict) and "last" in d:
            return float(d["last"])
    except Exception as e:
        log.warning(f"[PRICE V2] {symbol} fallback: {e}")
    raise RuntimeError(f"Нет последней цены для {symbol}")

# ====== SYMBOL CONFIG ======
_symbol_cfg = {}
def load_symbol_cfg():
    global _symbol_cfg
    r = requests.get(SYMBOLS_V2, headers=HEADERS_PUB, timeout=15)
    r.raise_for_status()
    arr = r.json().get("data", [])
    _symbol_cfg = {d["symbol"]: d for d in arr if "symbol" in d}
    log.info(f"[INIT] Loaded symbol config for {len(_symbol_cfg)} spot pairs")

def min_usdt(symbol: str) -> float:
    d = _symbol_cfg.get(symbol) or {}
    return float(d.get("minTradeUSDT", "1"))

def quote_precision(symbol: str) -> int:
    d = _symbol_cfg.get(symbol) or {}
    return int(d.get("quotePrecision", "8"))

def quantity_precision(symbol: str) -> int:
    d = _symbol_cfg.get(symbol) or {}
    return int(d.get("quantityPrecision", "6"))

def qfmt(symbol: str, x: float, kind: str) -> str:
    prec = quote_precision(symbol) if kind=="quote" else quantity_precision(symbol)
    return f"{x:.{prec}f}"

# ====== ACCOUNT & TRADING ======
def get_usdt_available() -> float:
    data = priv_get("/api/v2/spot/account/assets", {"coin":"USDT"})
    arr = data.get("data") or []
    if not arr: return 0.0
    return float(arr[0].get("available","0"))

def place_market_buy(symbol: str, spend_usdt: float) -> dict:
    payload = {
        "symbol": symbol, "side": "buy", "orderType": "market",
        "size": qfmt(symbol, spend_usdt, "quote"),
        "clientOid": f"buy-{symbol}-{int(time.time()*1000)}"
    }
    res = priv_post("/api/v2/spot/trade/place-order", payload)
    if res.get("code") != "00000": raise RuntimeError(f"Buy error: {res}")
    order_id = (res.get("data") or {}).get("orderId")
    time.sleep(0.7)
    info = priv_get("/api/v2/spot/trade/orderInfo", {"orderId": order_id, "symbol": symbol})
    od = (info.get("data") or {})
    return {"orderId": order_id, "baseQty": float(od.get("baseVolume","0")), "avgPrice": float(od.get("priceAvg","0") or "0")}

def place_market_sell(symbol: str, qty_base: float) -> dict:
    payload = {
        "symbol": symbol, "side": "sell", "orderType": "market",
        "size": qfmt(symbol, qty_base, "base"),
        "clientOid": f"sell-{symbol}-{int(time.time()*1000)}"
    }
    res = priv_post("/api/v2/spot/trade/place-order", payload)
    if res.get("code") != "00000": raise RuntimeError(f"Sell error: {res}")
    order_id = (res.get("data") or {}).get("orderId")
    time.sleep(0.7)
    info = priv_get("/api/v2/spot/trade/orderInfo", {"orderId": order_id, "symbol": symbol})
    od = (info.get("data") or {})
    return {"orderId": order_id, "quoteVolume": float(od.get("quoteVolume","0")), "avgPrice": float(od.get("priceAvg","0") or "0")}

# ====== LEVELS ======
def price_levels(price, direction="long"):
    tp = price*(1+TP_PCT); sl = price*(1-SL_PCT)
    return float(tp), float(sl)

# ====== POSITIONS ======
def load_positions(): return load_json(POSITIONS_FILE, {})
def save_positions(pos): save_json(POSITIONS_FILE, pos)

def register_signal(symbol: str, entry: float, tp: float, sl: float, source: str = "EMA 9/21 cross"):
    pos = load_positions()
    if symbol in pos and pos[symbol].get("is_open"): return
    pos[symbol] = {
        "is_open": True, "symbol": symbol, "side": "LONG",
        "entry": float(entry), "tp": float(tp), "sl": float(sl),
        "opened_at": datetime.utcnow().isoformat(timespec="seconds"), "source": source,
        "orderId_buy": None, "baseQty": None
    }
    save_positions(pos)
    log.info(f"[OPEN] {symbol} LONG | entry={fmt_price(entry)} tp={fmt_price(tp)} sl={fmt_price(sl)}")

# ====== SIGNALS (AGGRESSIVE): EMA9 crosses EMA21 on 5m ======
def analyze_symbol(sym: str):
    closes5, _ = fetch_spot_candles(sym, G5M, 300)
    if len(closes5) < EMA_SLOW + 2: return None
    ema9_5, ema21_5 = ema(closes5, EMA_FAST), ema(closes5, EMA_SLOW)
    f_prev, s_prev, f_cur, s_cur = ema9_5[-2], ema21_5[-2], ema9_5[-1], ema21_5[-1]
    price = closes5[-1]
    if any(v is None for v in (f_prev, s_prev, f_cur, s_cur)): return None

    bull_cross = (f_prev <= s_prev) and (f_cur > s_cur)
    if not bull_cross: return None

    tp, sl = price_levels(price, "long")
    return {"symbol": sym, "price": float(price), "tp": float(tp), "sl": float(sl),
            "ema5": (round(f_cur,6), round(s_cur,6))}

# ====== AUTOBUY (с пересчётом TP/SL от фактической цены сделки) ======
def try_autobuy(symbol: str, entry_price: float, tp_hint: float, sl_hint: float):
    if not AUTO_TRADE: return
    spend = max(TRADE_USDT, min_usdt(symbol))
    try:
        bal = get_usdt_available()
    except Exception as e:
        log.error(f"[BAL ERR] {e}"); tg_send(f"⚠️ Ошибка запроса баланса: {e}"); return
    if bal < spend:
        tg_send(f"⚠️ Недостаточно USDT ({bal:.2f}) для покупки {symbol} на {spend:.2f} USDT"); return

    try:
        info = place_market_buy(symbol, spend)
        base_qty  = float(info["baseQty"])
        avg_price = float(info["avgPrice"]) or float(entry_price)
    except Exception as e:
        log.error(f"[BUY ERR] {symbol}: {e}"); tg_send(f"❌ Покупка не выполнена {symbol}: {e}"); return

    # пересчёт уровней от реальной средней цены
    tp_new, sl_new = price_levels(avg_price, "long")

    pos = load_positions()
    pos[symbol] = {
        "is_open": True, "symbol": symbol, "side": "LONG",
        "entry": float(avg_price), "tp": float(tp_new), "sl": float(sl_new),
        "opened_at": datetime.utcnow().isoformat(timespec="seconds"),
        "orderId_buy": info["orderId"], "baseQty": base_qty, "source": "EMA 9/21 cross"
    }
    save_positions(pos)

    tg_send(
        "🟢 BUY (SPOT)\n"
        f"Пара: {symbol}\n"
        f"Сумма: {spend:.2f} USDT → {base_qty:.8f}\n"
        f"Средняя цена: {fmt_price(avg_price)}\n"
        f"TP: {fmt_price(tp_new)} ({pct(TP_PCT)}) | SL: {fmt_price(sl_new)} ({pct(SL_PCT)})\n"
        f"id: {info['orderId']}"
    )
    log.info(f"[BUY] {symbol} qty={base_qty:.8f} avg={fmt_price(avg_price)} id={info['orderId']} | TP={fmt_price(tp_new)} SL={fmt_price(sl_new)}")

# ====== TP/SL WATCH + AUTOSELL ======
_last_tp_sl_log_ts = {}
def _should_log(sym):
    now = time.time(); last = _last_tp_sl_log_ts.get(sym,0)
    if now-last>=60: _last_tp_sl_log_ts[sym]=now; return True
    return False

def _pnl(entry, close): return (close-entry)/entry*100.0

def check_positions_once():
    pos = load_positions(); changed=False
    for symbol, p in list(pos.items()):
        if not p.get("is_open") or p.get("side")!="LONG": continue
        entry, tp, sl = float(p["entry"]), float(p["tp"]), float(p["sl"])
        try:
            price = get_last_price(symbol)
            _price_fail_cnt[symbol] = 0  # сбрасываем счётчик ошибок цен
        except Exception as e:
            cnt = _price_fail_cnt.get(symbol, 0) + 1
            _price_fail_cnt[symbol] = cnt
            if cnt % PRICE_FAILS_BEFORE_ALERT == 0:
                tg_send(f"⚠️ Не удаётся получить цену {symbol} уже {cnt} раз. Продолжаю попытки.")
            log.warning(f"[PRICE] {symbol}: fetch failed ({cnt}): {e}")
            continue

        if _should_log(symbol):
            log.info(f"[WATCH] {symbol} | price={fmt_price(price)} | TP={fmt_price(tp)} | SL={fmt_price(sl)}")

        reason=None
        if price>=tp: reason="✅ TP"
        elif price<=sl: reason="❌ SL"
        if not reason: continue

        sell_info=None
        if AUTO_TRADE and p.get("baseQty"):
            try:
                sell_info = place_market_sell(symbol, float(p["baseQty"]))
            except Exception as e:
                log.error(f"[SELL ERR] {symbol}: {e}"); tg_send(f"⚠️ Ошибка продажи {symbol}: {e}")

        p["is_open"]=False
        p["closed_at"]=datetime.utcnow().isoformat(timespec="seconds")
        p["close_price"]=price
        pos[symbol]=p; save_positions(pos); changed=True

        pl=_pnl(entry, price)
        details = f"\nПродажа id={sell_info.get('orderId')} avg={fmt_price(sell_info.get('avgPrice',0))}" if sell_info else ""
        tg_send(f"{reason} по {symbol}\nЦена закрытия: {fmt_price(price)}\nP/L: {pl:.3f}%\nОткрыто: {p['opened_at']}\nЗакрыто: {p['closed_at']}{details}")
        log.info(f"[CLOSE] {symbol} {reason} P/L={pl:.3f}%")
    if changed: save_positions(pos)

def check_positions_loop():
    while True:
        try: check_positions_once()
        except Exception as e: log.error(f"check_positions_loop error: {e}")
        time.sleep(POLL_SECONDS)

def start_closer():
    open_pos=[k for k,v in load_positions().items() if v.get("is_open")]
    log.info(f"[INIT] Открытые позиции при старте: {', '.join(open_pos) if open_pos else 'нет'}")
    threading.Thread(target=check_positions_loop, daemon=True).start()

# ====== MAIN SIGNAL LOOP ======
last_signal_side = {s: None for s in SYMBOLS}
last_signal_ts   = {s: 0 for s in SYMBOLS}
last_no_signal_sent = 0

def run_loop():
    global last_no_signal_sent
    tg_send("🤖 v3.5 AGGRESSIVE запущен. SPOT автоторговля ON. TF 5m. TP+0.5%/SL-0.4%.")
    try: load_symbol_cfg()
    except Exception as e: log.error(f"Symbols cfg error: {e}")

    # sanity
    for s in SYMBOLS:
        try:
            c,_ = fetch_spot_candles(s, G5M, 50)
            log.info(f"{s}: свечей(5m)={len(c)}")
        except Exception as e:
            log.error(f"{s} start fetch error: {e}")

    while True:
        try:
            any_signal=False
            for sym in SYMBOLS:
                res = analyze_symbol(sym)
                if not res: continue
                now=time.time()
                if last_signal_side.get(sym)=="long" and (now-last_signal_ts.get(sym,0)<PER_SYMBOL_COOLDOWN):
                    continue
                last_signal_side[sym]="long"; last_signal_ts[sym]=now; any_signal=True

                tg_send(
                    f"🟢 LONG сигнал {res['symbol']}\n"
                    f"Цена: ~ {fmt_price(res['price'])}\n"
                    f"TP: {fmt_price(res['tp'])} ({pct(TP_PCT)}) | SL: {fmt_price(res['sl'])} ({pct(SL_PCT)})\n"
                    f"EMA5m 9/21: {res['ema5'][0]} / {res['ema5'][1]}"
                )
                register_signal(res['symbol'], res['price'], res['tp'], res['sl'], source="EMA 9/21 cross 5m")
                try_autobuy(res['symbol'], res['price'], res['tp'], res['sl'])

            now=time.time()
            if not any_signal and now-last_no_signal_sent>=GLOBAL_OK_COOLDOWN:
                last_no_signal_sent=now; tg_send("ℹ️ Пока без новых сигналов. Проверяю рынок…")
        except Exception as e:
            log.exception(f"Loop error: {e}")
        time.sleep(CHECK_EVERY_SEC)

# ====== FLASK ======
@app.route("/")
def home():
    return "Signals v3.5 (AGGRESSIVE) running. UTC: " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

@app.route("/positions")
def positions_view():
    try:
        pos = load_positions(); opened = {k:v for k,v in pos.items() if v.get("is_open")}
        return {"opened": opened, "server_time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
    except Exception as e:
        return {"error": str(e)}, 500

@app.route("/panic-sell/<symbol>")
def panic_sell(symbol):
    pos = load_positions()
    p = pos.get(symbol)
    if not p or not p.get("is_open") or not p.get("baseQty"):
        return {"ok": False, "msg": "Нет открытой позиции"}, 400
    try:
        info = place_market_sell(symbol, float(p["baseQty"]))
        p["is_open"] = False
        p["closed_at"] = datetime.utcnow().isoformat(timespec="seconds")
        p["close_price"] = float(info.get("avgPrice") or 0)
        pos[symbol] = p
        save_positions(pos)
        tg_send(f"🛑 PANIC SELL {symbol}: id={info.get('orderId')} avg={fmt_price(p['close_price'])}")
        return {"ok": True, "orderId": info.get("orderId")}
    except Exception as e:
        tg_send(f"❌ PANIC SELL ERROR {symbol}: {e}")
        return {"ok": False, "error": str(e)}, 500

def start_loop(): threading.Thread(target=run_loop, daemon=True).start()

if __name__ == "__main__":
    start_closer(); start_loop()
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
