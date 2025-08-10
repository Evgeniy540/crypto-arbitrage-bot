# === main.py v3.7 (AGGRESSIVE + autosell + timeout + trail) ===
# Bitget SPOT V2 • EMA 9/21 cross (5m) • LONG-only
# BUY: market (4 USDT по умолчанию) → SELL: TP +0.2% / SL -0.3%
# Доп.: таймер-выход 30 мин, трейлинг (trigger +0.2%, trail 0.15%)
# Цена: /spot/v1/market/ticker *_SPBL → /v2/tickers → /v2/candles 1min (все с ретраями)
# Эндпоинты: / (жив), /positions, /panic-sell/<symbol>

import os, time, json, hmac, hashlib, base64, logging, threading, requests
from datetime import datetime, timezone
from urllib.parse import urlencode
from flask import Flask

# ====== КЛЮЧИ (как просил; для прод лучше ENV) ======
BITGET_API_KEY        = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET     = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"
TELEGRAM_TOKEN        = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID      = "5723086631"

# ====== ПАРАМЕТРЫ СТРАТЕГИИ ======
SYMBOLS = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","TRXUSDT","DOGEUSDT","PEPEUSDT","BGBUSDT"]
G5M = "5min"
EMA_FAST, EMA_SLOW = 9, 21

# Мягкие уровни — чаще закрытия
TP_PCT = 0.002   # +0.2%
SL_PCT = 0.003   # -0.3%

# Трейлинг и таймер
MAX_HOLD_MINUTES = 30
TRAIL_TRIGGER    = 0.002    # +0.2% от entry
TRAIL_DISTANCE   = 0.0015   # 0.15% от пика после триггера

# Частоты
CHECK_EVERY_SEC     = 15    # поиск сигналов
POLL_SECONDS        = 8     # монитор открытых
PER_SYMBOL_COOLDOWN = 60*3
GLOBAL_OK_COOLDOWN  = 60*20

# ====== ТОРГОВЛЯ ======
AUTO_TRADE = True
TRADE_USDT = 4.0            # под текущий баланс
POSITIONS_FILE = "positions.json"

# ====== API ======
API_ROOT       = "https://api.bitget.com"
CANDLES_V2     = f"{API_ROOT}/api/v2/spot/market/candles"
TICKERS_V2     = f"{API_ROOT}/api/v2/spot/market/tickers"
SYMBOLS_V2     = f"{API_ROOT}/api/v2/spot/public/symbols"
TICKER_V1_SPOT = f"{API_ROOT}/api/spot/v1/market/ticker"
HEADERS_PUB    = {"User-Agent":"Mozilla/5.0"}

PRICE_FAILS_BEFORE_ALERT = 5
_price_fail_cnt = {}

# ====== LOG / FLASK ======
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("signals-v3.7")
app = Flask(__name__)

# ====== УТИЛИТЫ ======
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

# ====== ПОДПИСЬ PRIVATE V2 ======
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

def priv_get(path, query=None, timeout=12):
    ts=_ts_ms(); sign=_sign(ts,"GET",path,query,None)
    r=requests.get(API_ROOT+path, params=query, headers=_headers(ts,sign), timeout=timeout)
    r.raise_for_status(); return r.json()
def priv_post(path, payload, timeout=12):
    ts=_ts_ms(); body=json.dumps(payload, separators=(",",":"))
    sign=_sign(ts,"POST",path,None,body)
    r=requests.post(API_ROOT+path, data=body, headers=_headers(ts,sign), timeout=timeout)
    r.raise_for_status(); return r.json()

# ====== ИНДИКАТОРЫ ======
def ema(values, period):
    if len(values) < period: return []
    k=2/(period+1); out=[None]*(period-1)
    sma=sum(values[:period])/period; out.append(sma); v=sma
    for x in values[period:]:
        v = x*k + v*(1-k); out.append(v)
    return out

# ====== РЫНОЧНЫЕ ДАННЫЕ ======
def fetch_spot_candles(symbol, granularity, limit=300):
    r = requests.get(CANDLES_V2, params={"symbol":symbol,"granularity":granularity,"limit":str(limit)},
                     headers=HEADERS_PUB, timeout=15)
    r.raise_for_status()
    data = r.json().get("data", [])
    rows=[]
    for row in data:
        try: rows.append((int(row[0]), float(row[4])))
        except: pass
    rows.sort(key=lambda x:x[0])
    closes=[c for _,c in rows]
    return closes

def get_last_close_1m(symbol):
    # берём последний close 1m как запасной вариант цены
    closes = fetch_spot_candles(symbol, "1min", 2)
    return closes[-1] if closes else None

def get_last_price(symbol: str) -> float:
    spbl = to_spbl(symbol)
    # V1 /ticker с *_SPBL
    for i in range(3):
        try:
            r = requests.get(TICKER_V1_SPOT, params={"symbol": spbl}, headers=HEADERS_PUB, timeout=10)
            r.raise_for_status()
            d = r.json().get("data")
            if isinstance(d, dict) and d.get("last") is not None:
                return float(d["last"])
        except Exception as e:
            log.warning(f"[PRICE V1] {symbol} try {i+1}/3: {e}")
        time.sleep(0.3)
    # V2 /tickers
    try:
        r = requests.get(TICKERS_V2, params={"symbol": symbol}, headers=HEADERS_PUB, timeout=10)
        r.raise_for_status()
        d = r.json().get("data")
        if isinstance(d, list) and d and d[0].get("last") is not None:
            return float(d[0]["last"])
        if isinstance(d, dict) and d.get("last") is not None:
            return float(d["last"])
    except Exception as e:
        log.warning(f"[PRICE V2] {symbol} fallback: {e}")
    # Candles 1m close
    c = get_last_close_1m(symbol)
    if c is not None: return float(c)
    raise RuntimeError(f"Нет последней цены для {symbol}")

# ====== КОНФИГ СИМВОЛОВ ======
_symbol_cfg = {}
def load_symbol_cfg():
    global _symbol_cfg
    r=requests.get(SYMBOLS_V2, headers=HEADERS_PUB, timeout=15)
    r.raise_for_status()
    arr = r.json().get("data", [])
    _symbol_cfg = {d["symbol"]: d for d in arr if "symbol" in d}
    log.info(f"[INIT] symbols cfg = {len(_symbol_cfg)}")

def min_usdt(symbol): return float((_symbol_cfg.get(symbol) or {}).get("minTradeUSDT", "1"))
def quote_precision(symbol): return int((_symbol_cfg.get(symbol) or {}).get("quotePrecision","8"))
def quantity_precision(symbol): return int((_symbol_cfg.get(symbol) or {}).get("quantityPrecision","6"))
def qfmt(symbol, x, kind):
    prec = quote_precision(symbol) if kind=="quote" else quantity_precision(symbol)
    return f"{x:.{prec}f}"

# ====== ТОРГОВЫЕ ОПЕРАЦИИ ======
def get_usdt_available() -> float:
    data = priv_get("/api/v2/spot/account/assets", {"coin":"USDT"})
    arr = data.get("data") or []
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
            time.sleep(0.7)
            info = priv_get("/api/v2/spot/trade/orderInfo", {"orderId": oid, "symbol": symbol})
            od = info.get("data") or {}
            return {"orderId": oid, "baseQty": float(od.get("baseVolume","0")), "avgPrice": float(od.get("priceAvg","0") or "0")}
        except Exception as e:
            last_err=e; time.sleep(0.6*(i+1))
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
            time.sleep(0.7)
            info = priv_get("/api/v2/spot/trade/orderInfo", {"orderId": oid, "symbol": symbol})
            od = info.get("data") or {}
            return {"orderId": oid, "quoteVolume": float(od.get("quoteVolume","0")), "avgPrice": float(od.get("priceAvg","0") or "0")}
        except Exception as e:
            last_err=e; time.sleep(0.6*(i+1))
    raise RuntimeError(f"Sell failed after {tries} tries: {last_err}")

# ====== РАСЧЁТ УРОВНЕЙ ======
def price_levels(price):
    return float(price*(1+TP_PCT)), float(price*(1-SL_PCT))

# ====== ПОЗИЦИИ ======
def load_positions(): return load_json(POSITIONS_FILE, {})
def save_positions(d): save_json(POSITIONS_FILE, d)

def register_signal(symbol, entry, tp, sl):
    pos=load_positions()
    if symbol in pos and pos[symbol].get("is_open"): return
    pos[symbol] = {"is_open": True, "symbol":symbol, "side":"LONG",
                   "entry": float(entry), "tp": float(tp), "sl": float(sl),
                   "opened_at": datetime.utcnow().isoformat(timespec="seconds"),
                   "orderId_buy": None, "baseQty": None,
                   "trail_active": False, "trail_top": float(entry)}
    save_positions(pos)

# ====== СИГНАЛЫ: только кросс EMA9/21 (5m) ======
def analyze_symbol(sym):
    closes = fetch_spot_candles(sym, G5M, 300)
    if len(closes) < EMA_SLOW+2: return None
    e9, e21 = ema(closes, EMA_FAST), ema(closes, EMA_SLOW)
    f_prev, s_prev, f_cur, s_cur = e9[-2], e21[-2], e9[-1], e21[-1]
    price = closes[-1]
    if any(v is None for v in (f_prev, s_prev, f_cur, s_cur)): return None
    bull_cross = (f_prev <= s_prev) and (f_cur > s_cur)
    if not bull_cross: return None
    tp, sl = price_levels(price)
    return {"symbol":sym, "price":float(price), "tp":float(tp), "sl":float(sl),
            "ema":(round(f_cur,6), round(s_cur,6))}

# ====== ПОКУПКА (TP/SL от фактической средней) ======
def try_autobuy(symbol, price_hint, tp_hint, sl_hint):
    if not AUTO_TRADE: return
    spend = max(TRADE_USDT, min_usdt(symbol))
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
    pos = load_positions()
    pos[symbol] = {"is_open": True, "symbol":symbol, "side":"LONG",
                   "entry": float(avg_price), "tp": float(tp_new), "sl": float(sl_new),
                   "opened_at": datetime.utcnow().isoformat(timespec="seconds"),
                   "orderId_buy": info["orderId"], "baseQty": base_qty,
                   "trail_active": False, "trail_top": float(avg_price)}
    save_positions(pos)

    tg_send(
        f"🟢 BUY (SPOT)\nПара: {symbol}\nСумма: {spend:.2f} USDT → {base_qty:.8f}\n"
        f"Средняя цена: {fmt_price(avg_price)}\nTP: {fmt_price(tp_new)} ({pct(TP_PCT)}) | SL: {fmt_price(sl_new)} ({pct(SL_PCT)})\n"
        f"id: {info['orderId']}"
    )
    log.info(f"[BUY] {symbol} qty={base_qty:.8f} avg={fmt_price(avg_price)} id={info['orderId']} TP={fmt_price(tp_new)} SL={fmt_price(sl_new)}")

# ====== МОНИТОРИНГ TP/SL + TRAIL + TIMEOUT ======
_last_watch = {}
def _should_watch(sym):
    now=time.time(); last=_last_watch.get(sym,0)
    if now-last>=60: _last_watch[sym]=now; return True
    return False

def check_positions_once():
    pos = load_positions(); changed=False
    for symbol, p in list(pos.items()):
        if not p.get("is_open") or p.get("side")!="LONG": continue
        entry, tp, sl = float(p["entry"]), float(p["tp"]), float(p["sl"])
        reason=None; price=None

        # TIMEOUT: закрываем даже если цену не получили
        try:
            opened_dt = datetime.fromisoformat(p["opened_at"])
            age_min = (datetime.utcnow() - opened_dt).total_seconds()/60
            if age_min >= MAX_HOLD_MINUTES:
                reason = "⏱️ TIMEOUT"
        except Exception:
            pass

        # Цена для TP/SL/трейлинга
        if reason is None:
            try:
                price = get_last_price(symbol)
                _price_fail_cnt[symbol]=0
            except Exception as e:
                cnt=_price_fail_cnt.get(symbol,0)+1; _price_fail_cnt[symbol]=cnt
                if cnt % PRICE_FAILS_BEFORE_ALERT == 0:
                    tg_send(f"⚠️ Не удаётся получить цену {symbol} уже {cnt} раз. Продолжаю попытки.")
                log.warning(f"[PRICE] {symbol}: fail({cnt}): {e}")

        # TRAIL: активируем при движении выше trigger, тянем стоп
        if reason is None and price is not None:
            if (not p.get("trail_active")) and price >= entry*(1+TRAIL_TRIGGER):
                p["trail_active"]=True; p["trail_top"]=price
            if p.get("trail_active"):
                p["trail_top"]=max(p.get("trail_top", entry), price)
                trail_sl = p["trail_top"]*(1-TRAIL_DISTANCE)
                if price <= trail_sl:
                    reason="🔁 TRAIL"; sl = trail_sl  # для инфо

        # TP/SL обычные
        if reason is None and price is not None:
            if price >= tp: reason="✅ TP"
            elif price <= sl: reason="❌ SL"

        # Логи прогресса
        if _should_watch(symbol) and price is not None:
            log.info(f"[WATCH] {symbol} price={fmt_price(price)} entry={fmt_price(entry)} TP={fmt_price(tp)} SL={fmt_price(sl)} trail={p.get('trail_active')} top={fmt_price(p.get('trail_top'))}")

        if reason is None:
            # обновили trail_top? сохранить
            pos[symbol]=p; continue

        # ПРОДАЖА ПО РЫНКУ (без ожидания цены)
        sell_info=None
        if AUTO_TRADE and p.get("baseQty"):
            try:
                sell_info = place_market_sell(symbol, float(p["baseQty"]))
                if sell_info.get("avgPrice"): price=float(sell_info["avgPrice"])
            except Exception as e:
                log.error(f"[SELL ERR] {symbol}: {e}"); tg_send(f"⚠️ Ошибка продажи {symbol}: {e}")

        p["is_open"]=False
        p["closed_at"]=datetime.utcnow().isoformat(timespec="seconds")
        if price is not None:
            p["close_price"]=price
            pl = (price-entry)/entry*100.0
        else:
            pl = 0.0
        pos[symbol]=p; save_positions(pos); changed=True

        tg_send(f"{reason} по {symbol}\nЦена закрытия: {fmt_price(price)}\nP/L: {pl:.3f}%\nОткрыто: {p['opened_at']}\nЗакрыто: {p['closed_at']}")
        log.info(f"[CLOSE] {symbol} {reason} P/L={pl:.3f}%")

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

# ====== ГЛАВНЫЙ ЦИКЛ СИГНАЛОВ ======
last_signal_side = {s: None for s in SYMBOLS}
last_signal_ts   = {s: 0 for s in SYMBOLS}
last_no_signal_sent=0

def run_loop():
    global last_no_signal_sent
    tg_send("🤖 v3.7 запущен. SPOT автоторговля ON. TF 5m. TP+0.2%/SL-0.3%. Таймер 30м, трейлинг.")
    try: load_symbol_cfg()
    except Exception as e: log.error(f"symbols cfg error: {e}")

    # sanity
    for s in SYMBOLS:
        try:
            closes = fetch_spot_candles(s, G5M, 50)
            log.info(f"{s}: свечей(5m)={len(closes)}")
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
                    f"EMA5m 9/21: {res['ema'][0]} / {res['ema'][1]}"
                )
                register_signal(res['symbol'], res['price'], res['tp'], res['sl'])
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
    return "Signals v3.7 running. UTC: " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

@app.route("/positions")
def positions_view():
    try:
        pos = load_positions(); opened = {k:v for k,v in pos.items() if v.get("is_open")}
        return {"opened": opened, "server_time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
    except Exception as e:
        return {"error": str(e)}, 500

@app.route("/panic-sell/<symbol>")
def panic_sell(symbol):
    pos = load_positions(); p = pos.get(symbol)
    if not p or not p.get("is_open") or not p.get("baseQty"):
        return {"ok": False, "msg":"Нет открытой позиции"}, 400
    try:
        info = place_market_sell(symbol, float(p["baseQty"]))
        p["is_open"]=False
        p["closed_at"]=datetime.utcnow().isoformat(timespec="seconds")
        p["close_price"]=float(info.get("avgPrice") or 0)
        pos[symbol]=p; save_positions(pos)
        tg_send(f"🛑 PANIC SELL {symbol}: id={info.get('orderId')} avg={fmt_price(p['close_price'])}")
        return {"ok": True, "orderId": info.get("orderId")}
    except Exception as e:
        tg_send(f"❌ PANIC SELL ERROR {symbol}: {e}")
        return {"ok": False, "error": str(e)}, 500

def start_loop(): threading.Thread(target=run_loop, daemon=True).start()

if __name__ == "__main__":
    start_closer(); start_loop()
    port = int(os.environ.get("PORT","8000"))
    app.run(host="0.0.0.0", port=port)
