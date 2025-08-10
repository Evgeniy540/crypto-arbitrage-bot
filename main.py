# === main.py v3.2 (Bitget SPOT V2, EMA 9/21, LONG-only AUTOTRADE, TP +0.5% / SL -0.4%) ===
import os, time, threading, logging, requests, json, hmac, hashlib, base64
from datetime import datetime, timezone
from urllib.parse import urlencode
from flask import Flask

# ====== ВШИТЫЕ КЛЮЧИ (по просьбе пользователя; небезопасно для продакшена) ======
BITGET_API_KEY        = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET     = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"

TELEGRAM_TOKEN   = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

# ====== СТРАТЕГИЯ ======
SYMBOLS = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","TRXUSDT","DOGEUSDT","PEPEUSDT","BGBUSDT"]
G5M, G1H = "5min", "1h"
EMA_FAST, EMA_SLOW = 9, 21
RSI_PERIOD = 14
VOL_MA, VOL_SPIKE_K = 20, 1.2
TP_PCT = 0.005   # +0.5%
SL_PCT = 0.004   # -0.4%
CHECK_EVERY_SEC       = 30
PER_SYMBOL_COOLDOWN   = 60*20    # 20 минут
GLOBAL_OK_COOLDOWN    = 60*60    # 1 час

# ====== АВТОТОРГОВЛЯ ======
AUTO_TRADE   = True
TRADE_USDT   = 10.0
POSITIONS_FILE = "positions.json"
TRADES_FILE    = "trades.json"

# ====== МОНИТОР TP/SL ======
POLL_SECONDS = 12
LOG_TPSL_EVERY_SEC = 60
_last_tp_sl_log_ts = {}

# ====== HTTP / API ======
HEADERS_PUB = {"User-Agent":"Mozilla/5.0"}
API_ROOT    = "https://api.bitget.com"
CANDLES_V2  = f"{API_ROOT}/api/v2/spot/market/candles"
TICKERS_V2  = f"{API_ROOT}/api/v2/spot/market/tickers"
SYMBOLS_V2  = f"{API_ROOT}/api/v2/spot/public/symbols"

# ====== PRIVATE API (V2) ======
def _ts_ms() -> str:
    return str(int(time.time() * 1000))

def _sign(ts: str, method: str, path: str, query: dict|None, body: str|None) -> str:
    q = ""
    if query:
        # querystring включаем в подпись (отсортированным)
        q = "?" + urlencode(sorted([(k, str(v)) for k, v in query.items()]))
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
    ts = _ts_ms()
    sign = _sign(ts, "GET", path, query, None)
    headers = _auth_headers(ts, sign)
    url = API_ROOT + path
    r = requests.get(url, params=query, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()

def priv_post(path: str, payload: dict, timeout=12):
    ts = _ts_ms()
    body = json.dumps(payload, separators=(",", ":"))
    sign = _sign(ts, "POST", path, None, body)
    headers = _auth_headers(ts, sign)
    url = API_ROOT + path
    r = requests.post(url, data=body, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()

# ====== LOGGING / FLASK ======
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("signals-v3.2")
app = Flask(__name__)

# ====== HELPERS ======
def tg_send(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
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

def _should_log_symbol(sym: str) -> bool:
    now = time.time()
    last = _last_tp_sl_log_ts.get(sym, 0)
    if now - last >= LOG_TPSL_EVERY_SEC:
        _last_tp_sl_log_ts[sym] = now
        return True
    return False

def load_json(path: str, default):
    if not os.path.exists(path): return default
    try:
        with open(path, "r") as f: return json.load(f)
    except: return default

def save_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

# ====== INDICATORS ======
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
            rows.append((int(row[0]), float(row[4]), float(row[5]) if len(row)>5 else 0.0))
        except: pass
    rows.sort(key=lambda x: x[0])
    closes = [c for _,c,_ in rows]
    vols   = [v for *_,v in rows]
    return closes, vols

def get_last_price(symbol: str) -> float:
    r = requests.get(TICKERS_V2, params={"symbol": symbol}, headers=HEADERS_PUB, timeout=10)
    r.raise_for_status()
    data = r.json().get("data")
    if isinstance(data, list) and data:
        last = data[0].get("last")
        if last is not None: return float(last)
    if isinstance(data, dict) and "last" in data:
        return float(data["last"])
    raise RuntimeError(f"No last price for {symbol}")

# ====== SYMBOL CONFIG ======
_symbol_cfg = {}  # symbol -> dict

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
    fmt = "{:0." + str(prec) + "f}"
    return fmt.format(x)

# ====== ACCOUNT & TRADING (SPOT V2) ======
def get_usdt_available() -> float:
    data = priv_get("/api/v2/spot/account/assets", {"coin":"USDT"})
    arr = data.get("data") or []
    if not arr: return 0.0
    return float(arr[0].get("available","0"))

def place_market_buy(symbol: str, spend_usdt: float) -> dict:
    payload = {
        "symbol": symbol,
        "side": "buy",
        "orderType": "market",
        "size": qfmt(symbol, spend_usdt, "quote"),
        "clientOid": f"buy-{symbol}-{int(time.time()*1000)}"
    }
    res = priv_post("/api/v2/spot/trade/place-order", payload)
    if res.get("code") != "00000":
        raise RuntimeError(f"Buy error: {res}")
    order_id = (res.get("data") or {}).get("orderId")
    time.sleep(0.8)  # даём бирже обработать
    info = priv_get("/api/v2/spot/trade/orderInfo", {"orderId": order_id, "symbol": symbol})
    od = (info.get("data") or {})
    filled_base = float(od.get("baseVolume","0"))
    avg_price   = float(od.get("priceAvg","0") or "0")
    return {"orderId": order_id, "baseQty": filled_base, "avgPrice": avg_price}

def place_market_sell(symbol: str, qty_base: float) -> dict:
    payload = {
        "symbol": symbol,
        "side": "sell",
        "orderType": "market",
        "size": qfmt(symbol, qty_base, "base"),
        "clientOid": f"sell-{symbol}-{int(time.time()*1000)}"
    }
    res = priv_post("/api/v2/spot/trade/place-order", payload)
    if res.get("code") != "00000":
        raise RuntimeError(f"Sell error: {res}")
    order_id = (res.get("data") or {}).get("orderId")
    time.sleep(0.8)
    info = priv_get("/api/v2/spot/trade/orderInfo", {"orderId": order_id, "symbol": symbol})
    od = (info.get("data") or {})
    filled_quote = float(od.get("quoteVolume","0"))
    avg_price    = float(od.get("priceAvg","0") or "0")
    return {"orderId": order_id, "quoteVolume": filled_quote, "avgPrice": avg_price}

# ====== LEVELS ======
def price_levels(price, direction):
    if direction == "long":
        tp = price*(1+TP_PCT); sl = price*(1-SL_PCT)
    else:
        tp = price*(1-TP_PCT); sl = price*(1+SL_PCT)
    return float(tp), float(sl)

# ====== POSITIONS / TRADES ======
def load_positions(): return load_json(POSITIONS_FILE, {})
def save_positions(pos): save_json(POSITIONS_FILE, pos)

def append_trade(entry: dict):
    trades = load_json(TRADES_FILE, [])
    trades.append(entry)
    save_json(TRADES_FILE, trades)

def register_signal(symbol: str, side: str, entry: float, tp: float, sl: float, source: str = "EMA 9/21"):
    side = side.upper().strip()
    pos = load_positions()
    if symbol in pos and pos[symbol].get("is_open"):
        return
    pos[symbol] = {
        "is_open": True,
        "symbol": symbol,
        "side": side,  # LONG/SHORT (на споте торгуем только LONG)
        "entry": float(entry),
        "tp": float(tp),
        "sl": float(sl),
        "opened_at": datetime.utcnow().isoformat(timespec="seconds"),
        "source": source,
        # торговые поля:
        "orderId_buy": None,
        "baseQty": None
    }
    save_positions(pos)
    log.info(f"[OPEN] {symbol} {side} | entry={fmt_price(entry)} tp={fmt_price(tp)} sl={fmt_price(sl)} | src={source}")

# ====== СИГНАЛКА ======
last_signal_side = {s: None for s in SYMBOLS}
last_signal_ts   = {s: 0 for s in SYMBOLS}
last_no_signal_sent = 0

def analyze_symbol(sym: str):
    closes5, vols5 = fetch_spot_candles(sym, G5M, 300)
    if len(closes5) < max(EMA_SLOW+2, RSI_PERIOD+2, VOL_MA+2): return None

    ema9_5, ema21_5 = ema(closes5, EMA_FAST), ema(closes5, EMA_SLOW)
    rsi5 = rsi(closes5, RSI_PERIOD)
    f_prev, s_prev, f_cur, s_cur = ema9_5[-2], ema21_5[-2], ema9_5[-1], ema21_5[-1]
    rsi_cur, price = rsi5[-1], closes5[-1]
    if any(v is None for v in (f_prev, s_prev, f_cur, s_cur, rsi_cur)): return None

    vol_spike = False
    if len(vols5) >= VOL_MA + 1:
        vol_ma = sum(vols5[-(VOL_MA+1):-1])/VOL_MA
        vol_spike = vols5[-1] > VOL_SPIKE_K * vol_ma

    bull_cross = (f_prev <= s_prev) and (f_cur > s_cur)

    closes1h, _ = fetch_spot_candles(sym, G1H, 200)
    if len(closes1h) < EMA_SLOW + 1: return None
    ema9_1h, ema21_1h = ema(closes1h, EMA_FAST), ema(closes1h, EMA_SLOW)
    t_fast, t_slow = ema9_1h[-1], ema21_1h[-1]
    if any(v is None for v in (t_fast, t_slow)): return None

    uptrend = t_fast > t_slow
    long_ok = (45 <= rsi_cur <= 65)

    long_signal  = bull_cross and uptrend and long_ok
    if not long_signal:
        return None

    direction = "long"
    conf = "A" if (vol_spike and (50<=rsi_cur<=60)) else "B"
    tp, sl = price_levels(price, direction)
    return {
        "symbol": sym, "direction": direction, "confidence": conf,
        "price": float(price), "tp": float(tp), "sl": float(sl),
        "tp_pct": TP_PCT, "sl_pct": SL_PCT,
        "rsi": round(rsi_cur,2), "vol_spike": vol_spike,
        "ema5": (round(ema9_5[-1],6), round(ema21_5[-1],6)),
        "ema1h": (round(ema9_1h[-1],6), round(ema21_1h[-1],6))
    }

# ====== ТОРГОВЫЙ ВХОД ПО СИГНАЛУ LONG ======
def try_autobuy(symbol: str, entry_price: float, tp: float, sl: float):
    if not AUTO_TRADE:
        return
    spend = max(TRADE_USDT, min_usdt(symbol))
    try:
        bal = get_usdt_available()
    except Exception as e:
        log.error(f"[BAL ERR] {e}")
        tg_send(f"⚠️ Ошибка запроса баланса: {e}")
        return
    if bal < spend:
        tg_send(f"⚠️ Недостаточно USDT ({bal:.2f}) для покупки {symbol} на {spend:.2f} USDT")
        return
    try:
        info = place_market_buy(symbol, spend)
        base_qty = info["baseQty"]
        avg_price = info["avgPrice"] or entry_price
    except Exception as e:
        log.error(f"[BUY ERR] {symbol}: {e}")
        tg_send(f"❌ Покупка не выполнена {symbol}: {e}")
        return
    pos = load_positions()
    p = pos.get(symbol) or {}
    p["orderId_buy"] = info["orderId"]
    p["baseQty"] = base_qty
    pos[symbol] = p
    save_positions(pos)
    tg_send(
        f"🟢 BUY исполнен (SPOT)\n"
        f"Пара: {symbol}\n"
        f"Сумма: {spend:.2f} USDT → {base_qty:.8f}\n"
        f"Цена покупки: {fmt_price(avg_price)}\n"
        f"TP: {fmt_price(tp)} ({pct(TP_PCT)}) | SL: {fmt_price(sl)} ({pct(SL_PCT)})\n"
        f"OrderId: {info['orderId']}"
    )
    log.info(f"[BUY] {symbol} qty={base_qty:.8f} avg={fmt_price(avg_price)} id={info['orderId']}")

# ====== МОНИТОР TP/SL + АВТОПРОДАЖА ======
def _pnl_pct(entry: float, close: float) -> float:
    return (close - entry) / entry * 100.0

def check_positions_once():
    pos = load_positions()
    changed = False
    for symbol, p in list(pos.items()):
        if not p.get("is_open"): continue
        if p.get("side") != "LONG": continue  # торгуем только LONG

        entry = float(p["entry"])
        tp    = float(p["tp"])
        sl    = float(p["sl"])

        try:
            price = get_last_price(symbol)
        except Exception as e:
            log.warning(f"[PRICE] {symbol}: fetch failed: {e}")
            continue

        if _should_log_symbol(symbol):
            dist_tp = (abs(tp - price) / price) * 100.0
            dist_sl = (abs(price - sl) / price) * 100.0
            span = tp - entry
            prog = 0.0 if span == 0 else (price - entry) / span * 100.0
            log.info(f"[WATCH] {symbol} LONG | price={fmt_price(price)} | TP={fmt_price(tp)} (~{dist_tp:.3f}%) | SL={fmt_price(sl)} (~{dist_sl:.3f}%) | progress={prog:.2f}%")

        close_reason = None
        if price >= tp: close_reason = "✅ TP"
        elif price <= sl: close_reason = "❌ SL"

        if close_reason:
            sell_info = None
            if AUTO_TRADE and p.get("baseQty"):
                try:
                    sell_info = place_market_sell(symbol, float(p["baseQty"]))
                except Exception as e:
                    log.error(f"[SELL ERR] {symbol}: {e}")
                    tg_send(f"⚠️ Ошибка продажи {symbol}: {e}")

            p["is_open"] = False
            p["closed_at"] = datetime.utcnow().isoformat(timespec="seconds")
            p["close_price"] = price
            pos[symbol] = p
            save_positions(pos)
            changed = True

            pl = _pnl_pct(entry, price)
            details = ""
            if sell_info:
                details = f"\nПродажа market: id={sell_info.get('orderId')} avg={fmt_price(sell_info.get('avgPrice',0))}"
            tg_send(
                f"{close_reason} по {symbol}\n"
                f"Цена закрытия: {fmt_price(price)}\n"
                f"P/L: {pl:.3f}%\n"
                f"Открыто: {p['opened_at']}\nЗакрыто: {p['closed_at']}{details}"
            )
            log.info(f"[CLOSE] {symbol} LONG @ {fmt_price(price)} | {close_reason} | P/L={pl:.3f}%")

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
    open_pos = [k for k,v in load_positions().items() if v.get("is_open")]
    if open_pos:
        log.info(f"[INIT] Открытые позиции при старте мониторинга: {', '.join(open_pos)}")
    else:
        log.info("[INIT] Открытых позиций нет")
    threading.Thread(target=check_positions_loop, daemon=True).start()

# ====== ОСНОВНОЙ ПОИСК СИГНАЛОВ ======
def run_loop():
    global last_no_signal_sent
    tg_send("🤖 Signals v3.2 запущен. SPOT автоторговля включена. TF: 5m/1h. TP +0.5% / SL -0.4%.")

    try:
        load_symbol_cfg()
    except Exception as e:
        log.error(f"Load symbols cfg error: {e}")

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

                now = time.time()
                if last_signal_side.get(sym) == res["direction"] and (now - last_signal_ts.get(sym,0) < PER_SYMBOL_COOLDOWN):
                    continue
                last_signal_side[sym] = res["direction"]
                last_signal_ts[sym] = now
                any_signal = True

                # инфо-сообщение о входе
                arrow = "🟢 LONG"
                conf = "✅ A" if res["confidence"]=="A" else "✔️ B"
                tg_send(
                    f"{arrow} сигнал {res['symbol']}\n"
                    f"Цена: ~ {fmt_price(res['price'])}\n"
                    f"TP: {fmt_price(res['tp'])} ({pct(TP_PCT)}) | SL: {fmt_price(res['sl'])} ({pct(SL_PCT)})\n"
                    f"RSI(5m): {res['rsi']} | Объём спайк: {'да' if res['vol_spike'] else 'нет'} | Уверенность: {conf}\n"
                    f"EMA5m 9/21: {res['ema5'][0]} / {res['ema5'][1]} | Тренд 1h: {res['ema1h'][0]} / {res['ema1h'][1]}"
                )

                # регистрируем и, если включено, покупаем
                register_signal(res['symbol'], "LONG", res['price'], res['tp'], res['sl'], source="EMA 9/21")
                try_autobuy(res['symbol'], res['price'], res['tp'], res['sl'])

            now = time.time()
            if not any_signal and now - last_no_signal_sent >= GLOBAL_OK_COOLDOWN:
                last_no_signal_sent = now
                tg_send("ℹ️ Пока без новых сигналов. Проверяю рынок…")
        except Exception as e:
            log.exception(f"Loop error: {e}")
        time.sleep(CHECK_EVERY_SEC)

# ====== FLASK ======
@app.route("/")
def home():
    return "Signals v3.2 running (SPOT V2 + AUTOTRADE). UTC: " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

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
    start_closer()
    start_loop()
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
