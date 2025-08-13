# =========================
# main.py — Bitget SPOT EMA 7/14 (стабильный buy)
# =========================
import os, time, json, hmac, base64, hashlib, threading, logging, requests
from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext
from flask import Flask

# ---- Decimal ----
getcontext().prec = 28

# ---- Ключи / TG ----
API_KEY        = "bg_7bd202760f36727cedf11a481dbca611"
API_SECRET     = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
API_PASSPHRASE = "Evgeniy84"

TELEGRAM_TOKEN   = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

# ---- Настройки ----
SYMBOLS = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","TRXUSDT","PEPEUSDT","BGBUSDT"]
BASE_TRADE_USDT = Decimal("10")      # базовая сумма заявки
TP_PCT = Decimal("0.010")            # 1.0%
SL_PCT = Decimal("0.007")            # 0.7%
EMA_FAST = 7
EMA_SLOW = 14
MIN_CANDLES = 5
CHECK_INTERVAL = 30                  # сек
MAX_OPEN_POS = 2
NO_SIGNAL_COOLDOWN_MIN = 60
MIN_NOTIONAL_BUFFER = Decimal("1.02")   # небольшой запас к минимуму
DAILY_REPORT_UTC = "20:47"

BITGET = "https://api.bitget.com"

# ---- Логи ----
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bot")

# ---- Flask (keep alive) ----
app = Flask(__name__)
@app.get("/")
def health(): return "OK", 200

# ---- Утилиты ----
def tg(text: str):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=8)
    except Exception as e:
        log.warning(f"TG error: {e}")

def now_ms() -> str: return str(int(time.time()*1000))

def _sign(ts: str, method: str, path: str, body: str="") -> str:
    msg = ts + method.upper() + path + body
    digest = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()

def _hdr(ts: str, sign: str):
    return {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": API_PASSPHRASE,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"
    }

def _json_or_raise(resp):
    txt = resp.text
    try:
        d = resp.json()
    except Exception:
        raise RuntimeError(f"HTTP {resp.status_code}: {txt}")
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {txt}")
    return d

# ---- Справочники пар ----
_PRODUCTS, _PRODUCTS_AT = None, 0

def _reload_products():
    global _PRODUCTS, _PRODUCTS_AT
    r = requests.get(BITGET + "/api/spot/v1/public/products",
                     headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
    d = _json_or_raise(r)
    if d.get("code") != "00000":
        raise RuntimeError(f"products error: {d}")
    _PRODUCTS = {p["symbol"]: p for p in d.get("data", [])}
    _PRODUCTS_AT = time.time()

def _ensure_products():
    if not _PRODUCTS or (time.time() - _PRODUCTS_AT) > 600:
        _reload_products()

def _sym_key(sym: str) -> str:
    return sym if sym.endswith("_SPBL") else sym + "_SPBL"

def get_rules(sym: str):
    _ensure_products()
    key = _sym_key(sym)
    if key not in _PRODUCTS: raise RuntimeError(f"symbol_not_found:{sym}")
    p = _PRODUCTS[key]
    return {
        "priceScale": int(p.get("priceScale", 6)),
        "quantityScale": int(p.get("quantityScale", 6)),
        "minTradeUSDT": Decimal(p.get("minTradeUSDT", "1"))
    }

# ---- Рынок ----
def get_price(sym: str) -> Decimal:
    r = requests.get(BITGET + "/api/spot/v1/market/tickers",
                     params={"symbol": _sym_key(sym)},
                     headers={"User-Agent":"Mozilla/5.0"}, timeout=10)
    d = _json_or_raise(r)
    if d.get("code") != "00000": raise RuntimeError(f"tickers error: {d}")
    arr = d.get("data") or []
    if not arr: raise RuntimeError("ticker empty")
    row = arr[0]
    for k in ("lastPr","close","last","c","bestAsk","askPr","bestAskPr"):
        v = row.get(k)
        if v not in (None,""):
            return Decimal(str(v))
    raise RuntimeError("ticker no price")

def get_candles(sym: str, limit: int=120):
    r = requests.get(BITGET + "/api/spot/v1/market/candles",
                     params={"symbol": _sym_key(sym), "period":"1min", "limit": limit},
                     headers={"User-Agent":"Mozilla/5.0"}, timeout=12)
    d = _json_or_raise(r)
    if d.get("code") != "00000": raise RuntimeError(f"candles error: {d}")
    rows = list(reversed(d.get("data") or []))
    closes = []
    for row in rows:
        if isinstance(row, (list,tuple)) and len(row) >= 5:
            closes.append(Decimal(str(row[4])))
        elif isinstance(row, dict):
            for k in ("close","lastPr","c","last"):
                if k in row:
                    closes.append(Decimal(str(row[k]))); break
    return closes

def get_usdt_balance() -> Decimal:
    ts = now_ms()
    path = "/api/spot/v1/account/assets"
    sign = _sign(ts, "GET", path + "?coin=USDT", "")
    r = requests.get(BITGET + path, params={"coin":"USDT"}, headers=_hdr(ts,sign), timeout=12)
    d = _json_or_raise(r)
    if d.get("code") != "00000": return Decimal("0")
    arr = d.get("data") or []
    if not arr: return Decimal("0")
    return Decimal(str(arr[0].get("available","0")))

# ---- EMA / сигнал ----
def ema(vals, period):
    if len(vals) < period: return []
    k = Decimal("2")/Decimal(period+1)
    out = [sum(vals[:period], Decimal("0"))/Decimal(period)]
    for v in vals[period:]:
        out.append(v*k + out[-1]*(Decimal("1")-k))
    return out

def ema_signal(closes):
    if len(closes) < EMA_SLOW: return None
    f = ema(closes, EMA_FAST)
    s = ema(closes, EMA_SLOW)
    n = min(len(f), len(s))
    if n < 2: return None
    f, s = f[-n:], s[-n:]
    if f[-2] <= s[-2] and f[-1] > s[-1]: return "long"
    if f[-2] >= s[-2] and f[-1] < s[-1]: return "short"
    return None

# ---- Ордеры ----
def _post_order(body: dict):
    ts = now_ms()
    path = "/api/spot/v1/trade/orders"
    payload = json.dumps(body, separators=(",",":"))
    sign = _sign(ts, "POST", path, payload)
    r = requests.post(BITGET + path, data=payload, headers=_hdr(ts,sign), timeout=20)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"code": f"HTTP{r.status_code}", "msg": r.text}

def _step(qscale: int) -> Decimal:
    return Decimal(1).scaleb(-qscale)  # 10^-qscale

def place_market_buy(sym: str, quote_usdt: Decimal, rules: dict, usdt_balance: Decimal):
    """Покупка: сперва quoteOrderQty; при 40019 — фолбэк с дублированием size и quantity."""
    min_usdt = rules["minTradeUSDT"]
    need = max(quote_usdt, (min_usdt * MIN_NOTIONAL_BUFFER)).quantize(Decimal("0.0001"))
    if need > usdt_balance:
        raise RuntimeError(f"balance_low:{usdt_balance} need:{need}")

    # 1) Основной путь — сумма в USDT
    body = {
        "symbol": _sym_key(sym),
        "side": "buy",
        "orderType": "market",
        "force": "normal",
        "clientOrderId": f"q-{sym}-{int(time.time()*1000)}",
        "quoteOrderQty": f"{need}"          # <- строго строкой
    }
    st, d = _post_order(body)
    code = str(d.get("code"))
    if code == "00000":
        return d.get("data")

    # 2) Фолбэк: рассчитываем количество и передаём И size, И quantity
    if code in {"40019","43010","43005","40034"}:
        px = get_price(sym)
        qscale = rules["quantityScale"]
        step = _step(qscale)
        qty = ((need/px) // step) * step
        if qty <= 0:
            raise RuntimeError(f"qty_zero_fallback need:{need} px:{px}")
        qty_str = f"{qty.normalize()}"
        body2 = {
            "symbol": _sym_key(sym),
            "side": "buy",
            "orderType": "market",
            "force": "normal",
            "clientOrderId": f"s-{sym}-{int(time.time()*1000)}",
            "size": qty_str,                 # Bitget
            "quantity": qty_str              # на случай «привета» 40019 от шлюза
        }
        st2, d2 = _post_order(body2)
        if str(d2.get("code")) != "00000":
            raise RuntimeError(f"order_error_fallback:{st2}:{d2}")
        return d2.get("data")

    raise RuntimeError(f"order_error:{st}:{d}")

def place_market_sell(sym: str, qty: Decimal, rules: dict):
    qscale = rules["quantityScale"]
    step = _step(qscale)
    size = (qty // step) * step
    if size <= 0: raise RuntimeError("sell_size_zero")
    qty_str = f"{size.normalize()}"
    body = {
        "symbol": _sym_key(sym),
        "side": "sell",
        "orderType": "market",
        "force": "normal",
        "clientOrderId": f"sl-{sym}-{int(time.time()*1000)}",
        "size": qty_str,
        "quantity": qty_str                 # симметрично на продажу
    }
    st, d = _post_order(body)
    if str(d.get("code")) != "00000":
        raise RuntimeError(f"sell_error:{st}:{d}")
    return d.get("data")

# ---- Локальные файлы ----
STATE_FILE  = "positions.json"
PROFIT_FILE = "profit.json"

def _load(path, default):
    try:
        with open(path,"r",encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save(path, data):
    with open(path,"w",encoding="utf-8") as f:
        json.dump(data,f,ensure_ascii=False,indent=2)

positions = _load(STATE_FILE, {})
profits   = _load(PROFIT_FILE, {"total":0.0,"trades":[]})

_last_no_signal = datetime.now(timezone.utc) - timedelta(minutes=NO_SIGNAL_COOLDOWN_MIN+1)

# ---- Торговля ----
def ema_maybe_buy():
    global positions, _last_no_signal
    if len(positions) >= MAX_OPEN_POS:
        return

    chosen = None
    for sym in SYMBOLS:
        if sym in positions: continue
        try:
            closes = get_candles(sym, limit=max(EMA_SLOW+20, 120))
            if len(closes) < MIN_CANDLES: continue
            if ema_signal(closes) == "long":
                chosen = sym; break
        except Exception as e:
            log.warning(f"{sym} candles error: {e}")

    if not chosen:
        if datetime.now(timezone.utc) - _last_no_signal > timedelta(minutes=NO_SIGNAL_COOLDOWN_MIN):
            tg(f"По рынку нет сигнала (EMA {EMA_FAST}/{EMA_SLOW}).")
            _last_no_signal = datetime.now(timezone.utc)
        return

    sym = chosen
    try:
        rules = get_rules(sym)
        bal = get_usdt_balance()
        want = min(BASE_TRADE_USDT, bal)
        need = max(want, rules["minTradeUSDT"]*MIN_NOTIONAL_BUFFER).quantize(Decimal("0.0001"))
        if need > bal:
            tg(f"❕ {sym}: покупка пропущена — нужно {need} USDT (мин {rules['minTradeUSDT']}), баланс {bal}.")
            return

        place_market_buy(sym, need, rules, bal)

        # оценим количество для статуса
        px = get_price(sym)
        qscale = rules["quantityScale"]
        step = _step(qscale)
        qty_est = ((need/px) // step) * step
        positions[sym] = {
            "qty": float(qty_est),
            "avg": float(px),
            "amount": float(qty_est*px),
            "opened": datetime.now(timezone.utc).isoformat()
        }
        _save(STATE_FILE, positions)
        tg(f"✅ Покупка {sym}: сумма={need} USDT, ~qty={qty_est}, цена≈{px}.")
    except Exception as e:
        tg(f"❗ Ошибка покупки {sym}: {e}")

def manage_positions():
    global positions, profits
    to_close = []
    for sym, pos in list(positions.items()):
        try:
            rules = get_rules(sym)
            px  = get_price(sym)
            avg = Decimal(str(pos["avg"]))
            qty = Decimal(str(pos["qty"]))
            chg = (px - avg)/avg
            reason = None
            if chg >= TP_PCT: reason = "TP"
            elif chg <= -SL_PCT: reason = "SL"
            if not reason: continue

            place_market_sell(sym, qty, rules)
            pnl = (px - avg) * qty
            profits["total"] = float(Decimal(str(profits["total"])) + pnl)
            profits["trades"].append({
                "symbol": sym, "qty": float(qty), "buy": float(avg), "sell": float(px),
                "pnl": float(pnl), "closed": datetime.now(timezone.utc).isoformat(), "reason": reason
            })
            _save(PROFIT_FILE, profits)
            tg(f"💰 {reason} {sym}: qty={qty}, {avg}→{px}, PnL={pnl:.6f} USDT. "
               f"Итого: {profits['total']:.6f} USDT.")
            to_close.append(sym)
        except Exception as e:
            log.warning(f"manage {sym} error: {e}")
    for s in to_close: positions.pop(s, None)
    if to_close: _save(STATE_FILE, positions)

# ---- Отчёты / команды ----
def format_profit():
    total = profits.get("total",0.0)
    rows  = profits.get("trades",[])
    lines = [f"📊 Итоговая прибыль: {total:.6f} USDT"]
    if positions:
        lines.append("Открытые позиции:")
        for s,p in positions.items():
            lines.append(f"• {s}: qty={p['qty']}, avg={p['avg']:.8f}")
    if rows:
        lines.append("Последние сделки:")
        for t in rows[-5:]:
            lines.append(f"• {t['symbol']} ({t['reason']}): {t['qty']} шт, "
                         f"{t['buy']:.6f}→{t['sell']:.6f}, PnL={t['pnl']:.6f}")
    else:
        lines.append("Сделок ещё не было.")
    return "\n".join(lines)

def format_status():
    try: bal = get_usdt_balance()
    except Exception: bal = Decimal("0")
    lines = [
        "🛠 Статус",
        f"Баланс USDT: {bal}",
        f"Сделка: {BASE_TRADE_USDT} USDT",
        f"Открытых позиций: {len(positions)}/{MAX_OPEN_POS}",
        f"EMA {EMA_FAST}/{EMA_SLOW}, TP {TP_PCT*100:.1f}%, SL {SL_PCT*100:.1f}%, MIN_CANDLES {MIN_CANDLES}",
    ]
    if positions:
        for s,p in positions.items():
            lines.append(f"• {s}: qty={p['qty']}, avg={p['avg']:.8f}")
    return "\n".join(lines)

def tg_loop():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    offset = None
    last_daily = None
    while True:
        try:
            params = {"timeout": 25}
            if offset is not None: params["offset"] = offset
            r = requests.get(url, params=params, timeout=30)
            d = r.json()
            if d.get("ok"):
                for upd in d.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or upd.get("edited_message") or {}
                    text = (msg.get("text") or "").strip().lower()
                    chat = str((msg.get("chat") or {}).get("id") or TELEGRAM_CHAT_ID)
                    if text.startswith("/profit"):
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                      data={"chat_id": chat, "text": format_profit()}, timeout=8)
                    elif text.startswith("/status"):
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                      data={"chat_id": chat, "text": format_status()}, timeout=8)
        except Exception:
            time.sleep(2)

        try:
            hhmm = datetime.now(timezone.utc).strftime("%H:%M")
            if hhmm == DAILY_REPORT_UTC and last_daily != hhmm:
                last_daily = hhmm
                tg("🗓 Ежедневный отчёт:\n" + format_profit())
        except Exception:
            pass

def trade_loop():
    while True:
        try:
            manage_positions()
            ema_maybe_buy()
        except Exception as e:
            log.exception(f"loop error: {e}")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    threading.Thread(target=trade_loop, daemon=True).start()
    threading.Thread(target=tg_loop,    daemon=True).start()
    tg(f"🤖 Бот запущен! EMA {EMA_FAST}/{EMA_SLOW}, TP {TP_PCT*100:.1f}%, SL {SL_PCT*100:.1f}%. "
       f"MIN_CANDLES={MIN_CANDLES}. Сообщения — только по факту сделок.")
    port = int(os.environ.get("PORT","5000"))
    app.run(host="0.0.0.0", port=port)
