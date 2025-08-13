# =========================
# main.py — Bitget SPOT бот
# =========================
# Торговля по EMA 7/14. Ставим маркет-покупку каскадом:
#   quoteOrderQty → size → quantity
# Всегда округляем размер до precision биржи и не шлём сделки меньше минимума.
# В TG пишем только по факту сделок / ошибок. Есть self-heal и ежедневный отчёт.

import os, time, hmac, hashlib, base64, json, math, logging, requests, threading
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from flask import Flask, request

# ---------- ПАРАМЕТРЫ ----------
API_KEY       = "bg_7bd202760f36727cedf11a481dbca611"
API_SECRET    = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
API_PASSPHRASE= "Evgeniy84"

TELEGRAM_TOKEN   = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

SYMBOLS = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","TRXUSDT","PEPEUSDT","BGBUSDT"]

BASE_TRADE_AMOUNT = 10.0         # базовая сумма на сделку, USDT
TP_PCT = 0.010                   # тейк-профит 1.0%
SL_PCT = 0.007                   # стоп-лосс 0.7%
EMA_FAST = 7
EMA_SLOW = 14
MIN_CANDLES = 5
CHECK_INTERVAL = 30              # сек между проходами
MAX_OPEN_POSITIONS = 2
NO_SIGNAL_COOLDOWN_MIN = 60
MIN_NOTIONAL_BUFFER = 1.02       # небольшой запас к минимуму биржи

DAILY_REPORT_UTC = "20:47"       # HH:MM по UTC

BITGET = "https://api.bitget.com"

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bot")

# ---------- Flask (keep-alive + webhook опционально) ----------
app = Flask(__name__)

@app.get("/")
def health():
    return "OK", 200

# ---------- УТИЛИТЫ ----------
def tg(text: str):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=8)
    except Exception as e:
        log.warning(f"TG send error: {e}")

def now_ms() -> str:
    return str(int(time.time()*1000))

def sign_payload(ts: str, method: str, path: str, body: str="") -> str:
    msg = ts + method.upper() + path + body
    digest = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()

def headers(ts: str, sign: str):
    return {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": API_PASSPHRASE,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"
    }

def get_json_or_raise(resp):
    txt = resp.text
    try:
        d = resp.json()
    except Exception:
        raise RuntimeError(f"HTTP {resp.status_code}: {txt}")
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {txt}")
    return d

def floor_to_scale(x: float, scale: int) -> float:
    m = 10 ** max(0, scale)
    return math.floor(float(x) * m) / m

def floor_usdt(x: float, scale: int = 4) -> float:
    m = 10 ** scale
    return math.floor(float(x) * m) / m

# ---------- КЭШ ПРАВИЛ ПАР ----------
@lru_cache(maxsize=256)
def _products_index() -> dict:
    r = requests.get(BITGET + "/api/spot/v1/public/products",
                     headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
    d = get_json_or_raise(r)
    if d.get("code") != "00000":
        raise RuntimeError(f"products error: {d}")
    return {p["symbol"]: p for p in d.get("data", [])}

def normalize_symbol(sym: str) -> str:
    # для Bitget формат <BASE><QUOTE>_SPBL (например, BTCUSDT_SPBL)
    s = sym if sym.endswith("_SPBL") else f"{sym}_SPBL"
    idx = _products_index()
    if s in idx: return s
    raise RuntimeError(f"symbol_not_found:{sym}")

def get_symbol_rules(sym: str) -> dict:
    p = _products_index()[normalize_symbol(sym)]
    return {
        "priceScale":    int(p.get("priceScale", 4)),
        "quantityScale": int(p.get("quantityScale", 4)),
        "minTradeUSDT":  float(p.get("minTradeUSDT", 1.0))
    }

# ---------- РЫНОК ----------
def get_ticker_price(sym: str) -> float:
    r = requests.get(BITGET + "/api/spot/v1/market/tickers",
                     params={"symbol": normalize_symbol(sym)},
                     headers={"User-Agent":"Mozilla/5.0"}, timeout=10)
    d = get_json_or_raise(r)
    if d.get("code") != "00000":
        raise RuntimeError(f"ticker error: {d}")
    arr = d.get("data") or []
    if not arr: raise RuntimeError("ticker empty")
    last = arr[0].get("lastPr") or arr[0].get("close") or arr[0].get("last")
    if last is None: raise RuntimeError("ticker no last price")
    return float(last)

def get_candles(sym: str, limit: int = 100):
    params = {"symbol": normalize_symbol(sym), "period": "1min", "limit": limit}
    r = requests.get(BITGET + "/api/spot/v1/market/candles",
                     params=params, headers={"User-Agent":"Mozilla/5.0"}, timeout=12)
    d = get_json_or_raise(r)
    if d.get("code") != "00000":
        raise RuntimeError(f"candles error: {d}")
    rows = list(reversed(d.get("data") or []))
    closes = []
    for row in rows:
        # API может возвращать массив или словарь; вытаскиваем close
        if isinstance(row, (list, tuple)) and len(row) >= 5:
            closes.append(float(row[4]))
        elif isinstance(row, dict):
            for k in ("lastPr","close","c","last"):
                if k in row:
                    closes.append(float(row[k])); break
    return closes

# ---------- БАЛАНС ----------
def get_usdt_balance() -> float:
    ts = now_ms()
    path = "/api/spot/v1/account/assets"
    q    = "coin=USDT"
    sign = sign_payload(ts, "GET", path + "?" + q, "")
    r = requests.get(BITGET + path, params={"coin":"USDT"}, headers=headers(ts,sign), timeout=12)
    d = get_json_or_raise(r)
    if d.get("code") != "00000": return 0.0
    arr = d.get("data") or []
    if not arr: return 0.0
    return float(arr[0].get("available","0"))

# ---------- ТЕХАНАЛИТИКА ----------
def ema(values, period):
    if len(values) < period: return []
    k = 2/(period+1)
    out = [sum(values[:period])/period]
    for v in values[period:]:
        out.append(v*k + out[-1]*(1-k))
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

# ---------- ЗАКАЗЫ ----------
def _post_order(body: dict):
    ts = now_ms()
    path = "/api/spot/v1/trade/orders"
    payload = json.dumps(body, separators=(",",":"))
    sign = sign_payload(ts, "POST", path, payload)
    r = requests.post(BITGET + path, data=payload, headers=headers(ts,sign), timeout=20)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"code": f"HTTP{r.status_code}", "msg": r.text}

def place_market_buy(sym: str, quote_usdt: float, px: float, rules: dict, usdt_balance: float):
    """
    Строгая защита от:
    - пустого количества (size/quantity == 0);
    - суммы меньше минимума (45110).
    """
    qscale = int(rules["quantityScale"])
    min_usdt = max(1.0, float(rules["minTradeUSDT"]))
    need_usdt = max(quote_usdt, min_usdt * MIN_NOTIONAL_BUFFER)
    if need_usdt > usdt_balance:
        raise RuntimeError(f"balance_low:{usdt_balance:.4f} need:{need_usdt:.4f}")

    # 1) пробуем quoteOrderQty
    quote = floor_usdt(need_usdt, 4)
    body_q = {
        "symbol": normalize_symbol(sym), "side": "buy", "orderType": "market", "force": "gtc",
        "clientOrderId": f"q-{sym}-{int(time.time()*1000)}", "quoteOrderQty": f"{quote:.4f}"
    }
    st1, d1 = _post_order(body_q)
    if str(d1.get("code")) == "00000":
        return d1.get("data")

    # 2) считаем size (кол-во базовой монеты) и пробуем "size"
    size = floor_to_scale(quote / px, qscale)
    if size <= 0:
        # минимальный положительный шаг по масштабу
        size = round(10 ** (-qscale), qscale)
    body_s = {
        "symbol": normalize_symbol(sym), "side": "buy", "orderType": "market", "force": "gtc",
        "clientOrderId": f"s-{sym}-{int(time.time()*1000)}", "size": f"{size:.{qscale}f}"
    }
    st2, d2 = _post_order(body_s)
    if str(d2.get("code")) == "00000":
        return d2.get("data")

    # 3) пробуем альтернативно "quantity"
    body_qty = {
        "symbol": normalize_symbol(sym), "side": "buy", "orderType": "market", "force": "gtc",
        "clientOrderId": f"qty-{sym}-{int(time.time()*1000)}", "quantity": f"{size:.{qscale}f}"
    }
    st3, d3 = _post_order(body_qty)
    if str(d3.get("code")) == "00000":
        return d3.get("data")

    # Не получилось — отправляем подробный отчёт
    tg(
        f"❗ Не удалось купить {sym}:\n"
        f"quote={quote:.4f} USDT, size≈{size:.{qscale}f} (qscale={qscale}).\n"
        f"Ответы: quote→ {st1} {d1}; size→ {st2} {d2}; quantity→ {st3} {d3}"
    )
    # Если конкретно 45110 — это меньше минимума, больше не дожимаем.
    raise RuntimeError(f"order_error:{d3}")

def place_market_sell(sym: str, size: float, rules: dict):
    qscale = int(rules["quantityScale"])
    size = floor_to_scale(float(size), qscale)
    if size <= 0:
        raise RuntimeError("sell_size_zero")
    body = {
        "symbol": normalize_symbol(sym), "side": "sell", "orderType": "market", "force": "gtc",
        "clientOrderId": f"sl-{sym}-{int(time.time()*1000)}", "size": f"{size:.{qscale}f}"
    }
    st, d = _post_order(body)
    if str(d.get("code")) != "00000":
        raise RuntimeError(f"sell_error:{st}:{d}")
    return d["data"]

# ---------- ХРАНИЛКА СОСТОЯНИЯ ----------
STATE_FILE = "positions.json"
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

positions = _load(STATE_FILE, {})            # {sym: {qty, avg, amount}}
profits   = _load(PROFIT_FILE, {"total":0.0,"trades":[]})

_last_no_signal = datetime.now(timezone.utc) - timedelta(minutes=NO_SIGNAL_COOLDOWN_MIN+1)
_last_daily = None

# ---------- ЛОГИКА ТОРГОВЛИ ----------
def maybe_buy_signal():
    global positions, _last_no_signal
    if len(positions) >= MAX_OPEN_POSITIONS:
        return
    picked = None
    for sym in SYMBOLS:
        if sym in positions: continue
        try:
            closes = get_candles(sym, limit=max(EMA_SLOW+20, 100))
            if len(closes) < MIN_CANDLES: continue
            if ema_signal(closes) == "long":
                picked = sym; break
        except Exception as e:
            log.warning(f"{sym}: candles error {e}")

    if not picked:
        if datetime.now(timezone.utc) - _last_no_signal > timedelta(minutes=NO_SIGNAL_COOLDOWN_MIN):
            tg(f"По рынку нет сигнала (EMA {EMA_FAST}/{EMA_SLOW}).")
            _last_no_signal = datetime.now(timezone.utc)
        return

    sym = picked
    try:
        rules = get_symbol_rules(sym)
        px    = get_ticker_price(sym)
        bal   = get_usdt_balance()

        min_usdt = max(1.0, rules["minTradeUSDT"])
        quote    = min(BASE_TRADE_AMOUNT, bal)
        if quote < min_usdt:
            tg(f"❕ {sym}: покупка пропущена — сумма {quote:.4f} USDT < {min_usdt:.4f} (мин.). Баланс {bal:.4f}.")
            return

        data = place_market_buy(sym, quote, px, rules, bal)

        # примерная фиксация размера (для статуса)
        qscale = int(rules["quantityScale"])
        size_est = floor_to_scale(quote / px, qscale)
        positions[sym] = {"qty": size_est, "avg": px, "amount": size_est*px,
                          "opened": datetime.now(timezone.utc).isoformat()}
        _save(STATE_FILE, positions)
        tg(f"✅ Покупка {sym}: ~qty={size_est}, цена≈{px:.8f}, сумма≈{quote:.4f} USDT (EMA {EMA_FAST}/{EMA_SLOW}).")
    except Exception as e:
        tg(f"❗ Ошибка покупки {sym}: {e}")

def manage_positions():
    global positions, profits
    to_close = []
    for sym, pos in list(positions.items()):
        try:
            rules = get_symbol_rules(sym)
            px    = get_ticker_price(sym)
            avg   = pos["avg"]
            chg   = (px - avg)/avg
            reason = None
            if chg >= TP_PCT: reason = "TP"
            elif chg <= -SL_PCT: reason = "SL"
            if not reason: continue

            qscale = int(rules["quantityScale"])
            size   = floor_to_scale(float(pos["qty"]), qscale)
            if size <= 0:
                to_close.append(sym); continue

            place_market_sell(sym, size, rules)
            pnl = (px - avg) * size
            profits["total"] += pnl
            profits["trades"].append({
                "symbol": sym, "qty": size, "buy": avg, "sell": px,
                "pnl": pnl, "closed": datetime.now(timezone.utc).isoformat(), "reason": reason
            })
            _save(PROFIT_FILE, profits)
            tg(f"💰 {reason} {sym}: qty={size}, {avg:.8f}→{px:.8f}, PnL={pnl:.4f} USDT. Итого: {profits['total']:.4f} USDT.")
            to_close.append(sym)
        except Exception as e:
            log.warning(f"manage {sym} error: {e}")

    for s in to_close:
        positions.pop(s, None)
    if to_close:
        _save(STATE_FILE, positions)

# ---------- ОТЧЁТЫ ----------
def format_profit():
    total = profits.get("total",0.0)
    rows  = profits.get("trades",[])
    lines = [f"📊 Отчёт. Итоговая прибыль: {total:.4f} USDT"]
    if positions:
        lines.append("Открытые позиции:")
        for s,p in positions.items():
            lines.append(f"• {s}: qty={p['qty']}, avg={p['avg']:.8f}")
    if rows:
        lines.append("Последние сделки:")
        for t in rows[-5:]:
            lines.append(f"• {t['symbol']} ({t['reason']}): {t['qty']} шт, "
                         f"{t['buy']:.6f}→{t['sell']:.6f}, PnL={t['pnl']:.4f}")
    else:
        lines.append("Сделок ещё не было.")
    return "\n".join(lines)

def format_status():
    bal = 0.0
    try: bal = get_usdt_balance()
    except Exception: pass
    lines = [
        "🛠 Статус",
        f"Баланс USDT: {bal:.4f}",
        f"Сделка: {BASE_TRADE_AMOUNT:.4f} USDT",
        f"Открытых позиций: {len(positions)}/{MAX_OPEN_POSITIONS}",
        f"EMA {EMA_FAST}/{EMA_SLOW}, TP {TP_PCT*100:.1f}%, SL {SL_PCT*100:.1f}%, MIN_CANDLES {MIN_CANDLES}",
    ]
    if positions:
        for s,p in positions.items():
            lines.append(f"• {s}: qty={p['qty']}, avg={p['avg']:.8f}")
    return "\n".join(lines)

def daily_report_tick():
    global _last_daily
    hhmm = datetime.now(timezone.utc).strftime("%H:%M")
    if hhmm == DAILY_REPORT_UTC:
        today = datetime.now(timezone.utc).date()
        if _last_daily != today:
            tg("🗓 Ежедневный отчёт:\n" + format_profit())
            _last_daily = today

# ---------- TELEGRAM LONG POLL ----------
def tg_loop():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    offset = None
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
                        try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                           data={"chat_id": chat, "text": format_profit()}, timeout=8)
                        except: pass
                    elif text.startswith("/status"):
                        try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                           data={"chat_id": chat, "text": format_status()}, timeout=8)
                        except: pass
        except Exception:
            time.sleep(2)
        try: daily_report_tick()
        except Exception: pass

# ---------- ОСНОВНОЙ ЦИКЛ ----------
def trade_loop():
    while True:
        try:
            manage_positions()
            maybe_buy_signal()
        except Exception as e:
            log.exception(f"loop error: {e}")
        time.sleep(CHECK_INTERVAL)

# ---------- RUN ----------
if __name__ == "__main__":
    threading.Thread(target=trade_loop, daemon=True).start()
    threading.Thread(target=tg_loop,    daemon=True).start()
    tg(f"🤖 Бот запущен! EMA {EMA_FAST}/{EMA_SLOW}, TP {TP_PCT*100:.1f}%, SL {SL_PCT*100:.1f}%. "
       f"MIN_CANDLES={MIN_CANDLES}. Сообщения — только по факту сделок.")
    port = int(os.environ.get("PORT","5000"))
    app.run(host="0.0.0.0", port=port)
