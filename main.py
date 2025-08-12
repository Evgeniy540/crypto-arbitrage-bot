# === main.py (Bitget SPOT; self-heal; safe-size; EMA 7/14; TP 1.0% / SL 0.7%; MIN_CANDLES=5) ===
import os, time, hmac, hashlib, base64, json, threading, math, logging, requests
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from collections import defaultdict
from flask import Flask, request

# ====== КЛЮЧИ (примерные; оставь свои) ======
API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
API_PASSPHRASE = "Evgeniy84"

# ====== TELEGRAM ======
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"
DAILY_REPORT_HHMM = os.environ.get("DAILY_REPORT_HHMM", "20:47").strip()
USE_WEBHOOK = os.environ.get("TELEGRAM_WEBHOOK", "0") == "1"  # по умолчанию off

# ====== НАСТРОЙКИ ======
SYMBOLS = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","TRXUSDT","PEPEUSDT","BGBUSDT"]
BASE_TRADE_AMOUNT = 10.0          # USDT на сделку
TP_PCT = 0.010                    # +1.0%
SL_PCT = 0.007                    # -0.7%
EMA_FAST = 7
EMA_SLOW = 14
MIN_CANDLES = 5                   # чаще сигналы
CHECK_INTERVAL = 30               # сек
NO_SIGNAL_COOLDOWN_MIN = 60
MAX_OPEN_POSITIONS = 2

CANDLES_LIMIT = max(100, EMA_SLOW + 20)
STATE_FILE = "positions.json"
PROFIT_FILE = "profit.json"
BITGET = "https://api.bitget.com"

# ====== SELF-HEAL параметры ======
MAX_RETRIES = 4
RETRY_BASE_SLEEP = 0.5
QUARANTINE_MIN = 10               # мин

# ====== ЛОГГЕР ======
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bot")

# ====== Flask keep-alive ======
app = Flask(__name__)

@app.get("/")
def health(): return "OK", 200

# глотатель вебхуков, чтобы не было 404; вебхук можно включить через env
@app.post("/telegram")
def telegram_webhook():
    if not USE_WEBHOOK:
        return "webhook disabled", 200
    try:
        upd = request.get_json(force=True, silent=True) or {}
        handle_telegram_update(upd)
        return "ok", 200
    except Exception as e:
        log.warning(f"webhook error: {e}")
        return "err", 200

# ====== УТИЛИТЫ ======
def tg(text: str, chat_id: str | None = None):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": chat_id or TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        log.warning(f"TG send error: {e}")

def now_ms() -> str: return str(int(time.time() * 1000))

def sign_payload(ts: str, method: str, path: str, body: str = "") -> str:
    prehash = ts + method.upper() + path + body
    digest = hmac.new(API_SECRET.encode(), prehash.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()

def headers(ts: str, sign: str):
    return {"ACCESS-KEY": API_KEY, "ACCESS-SIGN": sign, "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": API_PASSPHRASE, "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0"}

def get_json_or_raise(resp):
    txt = resp.text
    try: data = resp.json()
    except Exception: raise RuntimeError(f"HTTP {resp.status_code}: {txt}")
    if resp.status_code >= 400: raise RuntimeError(f"HTTP {resp.status_code}: {txt}")
    return data

# ====== ХРАНИЛКИ ======
def load_json(path, default):
    try:
        with open(path,"r",encoding="utf-8") as f: return json.load(f)
    except Exception: return default

def save_json(path, data):
    with open(path,"w",encoding="utf-8") as f: json.dump(data,f,ensure_ascii=False,indent=2)

# positions[sym] = {"qty":..., "avg":..., "amount":..., "opened":...}
positions = load_json(STATE_FILE, {})
profits   = load_json(PROFIT_FILE, {"total":0.0,"trades":[]})
last_no_signal_sent = datetime.now(timezone.utc) - timedelta(minutes=NO_SIGNAL_COOLDOWN_MIN+1)
_last_daily_report_date = None

# ====== SELF-HEAL: индексы/карантин/кэш цены ======
_bad_until: dict[str, datetime] = {}     # {sym_no_sfx: until_utc}
_err_counter = defaultdict(int)
PRICE_CACHE = {}  # {sym_no_sfx: {"px": float, "ts": datetime}}

def _cache_set(sym, px):
    PRICE_CACHE[sym] = {"px": float(px), "ts": datetime.now(timezone.utc)}

def _cache_get(sym, max_age_sec=300):
    it = PRICE_CACHE.get(sym)
    if not it: return None
    if (datetime.now(timezone.utc) - it["ts"]).total_seconds() > max_age_sec:
        return None
    return it["px"]

@lru_cache(maxsize=256)
def _products_index() -> dict[str, dict]:
    r = requests.get(BITGET + "/api/spot/v1/public/products", timeout=15,
                     headers={"User-Agent":"Mozilla/5.0"})
    d = get_json_or_raise(r)
    if d.get("code") != "00000": raise RuntimeError(f"products error: {d}")
    return {p["symbol"]: p for p in d.get("data", [])}

def normalize_symbol(sym_no_sfx: str) -> str:
    cand = sym_no_sfx if sym_no_sfx.endswith("_SPBL") else f"{sym_no_sfx}_SPBL"
    idx = _products_index()
    if cand in idx: return cand
    alt = cand.upper()
    if alt in idx: return alt
    raise RuntimeError(f"symbol_not_found:{sym_no_sfx}")

def _sleep_backoff(attempt): time.sleep(RETRY_BASE_SLEEP * (2 ** attempt))

def _candle_close(row):
    # Bitget чаще отдаёт массив: [ts,open,high,low,close,vol,...]
    if isinstance(row, (list, tuple)) and len(row) >= 5:
        return float(row[4])
    # иногда словари
    if isinstance(row, dict):
        for k in ("close", "c", "last", "endClose"):
            if k in row: return float(row[k])
    raise KeyError("close-not-found")

# ====== MARKET ======
def get_symbol_rules(sym_no_sfx: str):
    sym = normalize_symbol(sym_no_sfx)
    p = _products_index()[sym]
    return {"priceScale": int(p.get("priceScale",4)),
            "quantityScale": int(p.get("quantityScale",4)),
            "minTradeUSDT": float(p.get("minTradeUSDT",1.0))}

def _price_from_candles(sym_no_sfx):
    """Резерв: последняя цена из свечи."""
    try:
        closes = get_candles(sym_no_sfx, limit=2)
        if closes:
            return float(closes[-1])
    except Exception as e:
        log.warning(f"{sym_no_sfx}: fallback candles price error {repr(e)}")
    return None

def get_ticker_price(sym_no_sfx) -> float:
    """Надёжная цена:
       1) /market/tickers lastPr/close/last
       2) если нет — последняя свеча
       3) если нет — свежий кэш (<5 мин)
       + ретраи, бэк-офф, карантин при повторных сбоях.
    """
    until = _bad_until.get(sym_no_sfx)
    if until and until > datetime.now(timezone.utc):
        raise RuntimeError(f"symbol_quarantined:{sym_no_sfx}")

    sym = normalize_symbol(sym_no_sfx)
    last_exc = None

    # 1) основной тикер с ретраями
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(
                BITGET + "/api/spot/v1/market/tickers",
                params={"symbol": sym},
                headers={"User-Agent":"Mozilla/5.0"},
                timeout=15
            )
            d = get_json_or_raise(r)
            if d.get("code") == "00000":
                arr = d.get("data") or []
                if arr:
                    px = arr[0].get("lastPr") or arr[0].get("close") or arr[0].get("last")
                    if px is not None:
                        _cache_set(sym_no_sfx, px)
                        return float(px)
                last_exc = RuntimeError(f"ticker_no_lastPr:{arr}")
            else:
                if d.get("code") in ("40034","41018","400"):
                    last_exc = RuntimeError(f"param_error:{d.get('code')}:{d.get('msg')}")
                    break
                last_exc = RuntimeError(f"ticker_error:{d}")
        except Exception as e:
            last_exc = e
        _sleep_backoff(attempt)

    # 2) резерв — цена из свечи
    px_fallback = _price_from_candles(sym_no_sfx)
    if px_fallback is not None:
        _cache_set(sym_no_sfx, px_fallback)
        return float(px_fallback)

    # 3) последний кэш (до 5 мин давности)
    px_cached = _cache_get(sym_no_sfx, max_age_sec=300)
    if px_cached is not None:
        log.warning(f"{sym_no_sfx}: using cached price {px_cached} due to {repr(last_exc)}")
        return float(px_cached)

    # повторные сбои — карантин монеты
    _err_counter[sym_no_sfx] += 1
    if _err_counter[sym_no_sfx] >= 2:
        _bad_until[sym_no_sfx] = datetime.now(timezone.utc) + timedelta(minutes=QUARANTINE_MIN)
    raise RuntimeError(f"ticker_unavailable:{sym_no_sfx}:{last_exc}")

def get_candles(sym_no_sfx, limit=CANDLES_LIMIT):
    until = _bad_until.get(sym_no_sfx)
    if until and until > datetime.now(timezone.utc):
        raise RuntimeError(f"symbol_quarantined:{sym_no_sfx}")

    sym = normalize_symbol(sym_no_sfx)
    variants = [
        {"symbol": sym, "period": "1min", "limit": limit},
        {"symbol": sym, "granularity": "60", "limit": limit},
    ]

    last_err = None
    for params in variants:
        for attempt in range(MAX_RETRIES):
            try:
                r = requests.get(BITGET + "/api/spot/v1/market/candles",
                                 params=params, headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
                data = get_json_or_raise(r)
                code = data.get("code")
                if code != "00000":
                    if code in ("40034","41018","400"):
                        last_err = RuntimeError(f"param_error:{code}:{data.get('msg')}")
                        break  # к след. варианту
                    raise RuntimeError(f"bitget_error:{data}")
                rows = data.get("data") or []
                closes = []
                for row in reversed(rows):  # от старых к новым
                    try: closes.append(_candle_close(row))
                    except Exception: continue
                if len(closes) >= MIN_CANDLES:
                    _err_counter[sym_no_sfx] = 0
                    return closes
                last_err = RuntimeError(f"too_few_candles:{len(closes)}")
            except Exception as e:
                last_err = e
                _sleep_backoff(attempt)
                continue

    _err_counter[sym_no_sfx] += 1
    if _err_counter[sym_no_sfx] >= 2:
        _bad_until[sym_no_sfx] = datetime.now(timezone.utc) + timedelta(minutes=QUARANTINE_MIN)
    raise RuntimeError(f"candles_unavailable:{sym_no_sfx}:{last_err}")

# ====== ACCOUNT / ORDERS ======
def get_usdt_balance() -> float:
    ts = now_ms()
    path = "/api/spot/v1/account/assets"
    q = "coin=USDT"
    sign = sign_payload(ts,"GET",path+"?"+q,"")
    r = requests.get(BITGET+path, params={"coin":"USDT"}, headers=headers(ts,sign), timeout=15)
    d = get_json_or_raise(r)
    if d.get("code") != "00000": raise RuntimeError(f"balance error: {d}")
    arr = d.get("data", [])
    if not arr: return 0.0
    return float(arr[0].get("available","0"))

def floor_to_scale(x, scale):
    m = 10 ** max(0, scale)
    return math.floor(x*m)/m

def compute_order_qty(sym_no_sfx: str, amount_usdt: float, price: float):
    """
    Возвращает (qty_str, info) для Bitget.
    qty_str — уже округлено до quantityScale и готово для отправки;
    info=None если всё ок, иначе краткая причина отказа.
    """
    if price is None or price <= 0:
        return None, "no_price"

    rules = get_symbol_rules(sym_no_sfx)
    qscale = int(rules.get("quantityScale", 4))
    min_usdt = float(max(1.0, rules.get("minTradeUSDT", 1.0)))

    if amount_usdt < min_usdt:
        return None, f"amount<{min_usdt:.4f}"

    qty = floor_to_scale(amount_usdt / price, qscale)
    if qty <= 0:
        qty = floor_to_scale((min_usdt / price) * 1.0001, qscale)

    if qty <= 0:
        return None, "qty_zero"
    if qty * price < min_usdt:
        return None, f"notional<{min_usdt:.4f}"

    return f"{qty:.{qscale}f}", None

def place_market_order(sym_no_sfx, side, size_str):
    if not size_str:
        raise RuntimeError("empty_size")
    ts = now_ms()
    path = "/api/spot/v1/trade/orders"
    body = {"symbol": normalize_symbol(sym_no_sfx), "side": side.lower(),
            "orderType":"market","force":"gtc","size": size_str}
    payload = json.dumps(body, separators=(",",":"))
    sign = sign_payload(ts,"POST",path,payload)
    r = requests.post(BITGET+path, headers=headers(ts,sign), data=payload, timeout=20)
    d = get_json_or_raise(r)
    if d.get("code") != "00000": raise RuntimeError(f"order error: {d}")
    return d["data"]

# ====== ТЕХНИКА ======
def ema(values, period):
    if len(values) < period: return []
    k = 2/(period+1)
    out = [sum(values[:period])/period]
    for v in values[period:]:
        out.append(v*k + out[-1]*(1-k))
    return out

def ema_signal(closes):
    if len(closes) < EMA_SLOW: return None
    f, s = ema(closes, EMA_FAST), ema(closes, EMA_SLOW)
    if len(f) > len(s): f = f[-len(s):]
    if len(s) > len(f): s = s[-len(f):]
    if len(f) < 2: return None
    if f[-2] <= s[-2] and f[-1] > s[-1]: return "long"
    if f[-2] >= s[-2] and f[-1] < s[-1]: return "short"
    return None

# ====== ЛОГИКА ТОРГОВЛИ ======
def maybe_buy_signal():
    global positions, last_no_signal_sent
    if len(positions) >= MAX_OPEN_POSITIONS: return

    chosen = None
    for sym in SYMBOLS:
        if sym in positions: continue
        try:
            closes = get_candles(sym, CANDLES_LIMIT)
            if len(closes) < MIN_CANDLES: continue
            if ema_signal(closes) == "long":
                chosen = sym; break
        except Exception as e:
            log.warning(f"{sym}: candles_error {repr(e)}")

    if not chosen:
        if datetime.now(timezone.utc) - last_no_signal_sent >= timedelta(minutes=NO_SIGNAL_COOLDOWN_MIN):
            tg(f"По рынку нет сигнала (EMA {EMA_FAST}/{EMA_SLOW}).")
            last_no_signal_sent = datetime.now(timezone.utc)
        return

    sym = chosen
    try:
        price = get_ticker_price(sym)  # теперь с fallback и кэшем
        usdt_avail = get_usdt_balance()
        amount = min(BASE_TRADE_AMOUNT, usdt_avail)

        size_str, why = compute_order_qty(sym, amount, price)
        if why is not None:
            tg(f"❕ {sym}: пропуск покупки ({why}). Баланс {usdt_avail:.4f} USDT.")
            return

        place_market_order(sym, "buy", size_str)
        qty = float(size_str)
        notional = qty * price

        positions[sym] = {"qty": qty, "avg": price, "amount": notional,
                          "opened": datetime.now(timezone.utc).isoformat()}
        save_json(STATE_FILE, positions)
        tg(f"✅ Покупка {sym}: qty={qty}, цена≈{price:.8f}, сумма≈{notional:.4f} USDT. (EMA {EMA_FAST}/{EMA_SLOW})")
    except Exception as e:
        tg(f"❗ Ошибка покупки {sym}: {e}")
        log.exception(f"buy error {sym}: {e}")

def manage_positions():
    global positions, profits
    to_close = []
    for sym, pos in list(positions.items()):
        try:
            price = get_ticker_price(sym)
            avg = pos["avg"]
            change = (price - avg) / avg
            reason = None
            if change >= TP_PCT: reason = "TP"
            elif change <= -SL_PCT: reason = "SL"

            if reason:
                rules = get_symbol_rules(sym)
                qscale = int(rules["quantityScale"])
                qty = floor_to_scale(float(pos["qty"]), qscale)
                if qty <= 0: to_close.append(sym); continue
                min_usdt = max(1.0, rules["minTradeUSDT"])
                if qty*price < min_usdt:
                    tg(f"❗ Продажа {sym} отклонена: сумма {qty*price:.6f} < {min_usdt:.6f} USDT.")
                    to_close.append(sym); continue

                place_market_order(sym, "sell", f"{qty:.{qscale}f}")
                pnl = (price - avg)*qty
                profits["total"] += pnl
                profits["trades"].append({"symbol":sym,"qty":qty,"buy":avg,"sell":price,
                                          "pnl":pnl,"closed":datetime.now(timezone.utc).isoformat(),"reason":reason})
                save_json(PROFIT_FILE, profits)
                tg(f"💰 {reason} {sym}: qty={qty}, {avg:.8f}→{price:.8f}, PnL={pnl:.4f} USDT. Итого: {profits['total']:.4f} USDT.")
                to_close.append(sym)
        except Exception as e:
            log.warning(f"manage error {sym}: {e}")

    for sym in to_close: positions.pop(sym, None)
    if to_close: save_json(STATE_FILE, positions)

# ====== ОТЧЁТЫ/КОМАНДЫ ======
def format_profit_report():
    total = profits.get("total", 0.0)
    trades = profits.get("trades", [])
    lines = [f"📊 Отчёт", f"Итоговая прибыль: {total:.4f} USDT"]
    if positions:
        lines.append("Открытые позиции:")
        for s,p in positions.items():
            lines.append(f"• {s}: qty={p['qty']}, avg={p['avg']:.8f}")
    if trades:
        lines.append("Последние сделки:")
        for t in trades[-5:]:
            lines.append(f"• {t['symbol']} ({t.get('reason','')}): {t['qty']} шт, {t['buy']:.6f}→{t['sell']:.6f}, PnL={t['pnl']:.4f}")
    else:
        lines.append("Сделок ещё не было.")
    return "\n".join(lines)

def format_status(balance_now: float):
    lines = [
        "🛠 Статус",
        f"Баланс USDT: {balance_now:.4f}",
        f"Сделка: {BASE_TRADE_AMOUNT:.4f} USDT",
        f"Открытых позиций: {len(positions)}/{MAX_OPEN_POSITIONS}",
        f"EMA {EMA_FAST}/{EMA_SLOW}, TP {TP_PCT*100:.1f}%, SL {SL_PCT*100:.1f}%, MIN_CANDLES {MIN_CANDLES}",
    ]
    if positions:
        for s,p in positions.items():
            lines.append(f"• {s}: qty={p['qty']}, avg={p['avg']:.8f}")
    return "\n".join(lines)

def send_daily_report_if_time():
    global _last_daily_report_date
    hhmm = datetime.now(timezone.utc).strftime("%H:%M")
    if hhmm == DAILY_REPORT_HHMM:
        today = datetime.now(timezone.utc).date()
        if _last_daily_report_date != today:
            tg("🗓 Ежедневный отчёт:\n" + format_profit_report())
            _last_daily_report_date = today

def handle_telegram_update(upd: dict):
    msg = upd.get("message") or upd.get("edited_message") or {}
    chat_id = str((msg.get("chat") or {}).get("id") or TELEGRAM_CHAT_ID)
    text = (msg.get("text") or "").strip().lower()
    if text.startswith("/profit"):
        tg(format_profit_report(), chat_id=chat_id)
    elif text.startswith("/status"):
        try: bal = get_usdt_balance()
        except Exception: bal = 0.0
        tg(format_status(bal), chat_id=chat_id)

# ====== Telegram long-polling ======
def telegram_polling_loop():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    offset = None
    while True:
        try:
            params = {"timeout": 25}
            if offset: params["offset"] = offset
            r = requests.get(url, params=params, timeout=30)
            data = r.json()
            if not data.get("ok"): time.sleep(2); continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                handle_telegram_update(upd)
        except Exception:
            time.sleep(2)
        try: send_daily_report_if_time()
        except Exception: pass

# ====== ЦИКЛЫ + WATCHDOG ======
def trading_loop():
    while True:
        try:
            manage_positions()
            maybe_buy_signal()
        except Exception as e:
            log.exception(f"loop error: {e}")
        time.sleep(CHECK_INTERVAL)

def start_trading_thread():
    global trading_thread
    trading_thread = threading.Thread(target=trading_loop, daemon=True)
    trading_thread.start()

def start_telegram_thread():
    global telegram_thread
    telegram_thread = threading.Thread(target=telegram_polling_loop, daemon=True)
    telegram_thread.start()

def watchdog():
    while True:
        if not trading_thread.is_alive():
            log.warning("Trading loop died — restarting")
            start_trading_thread()
        if not telegram_thread.is_alive():
            log.warning("Telegram loop died — restarting")
            start_telegram_thread()
        time.sleep(5)

# ====== START ======
if __name__ == "__main__":
    start_trading_thread()
    start_telegram_thread()
    threading.Thread(target=watchdog, daemon=True).start()
    tg("🤖 Бот запущен! EMA 7/14, TP 1.0%, SL 0.7%. MIN_CANDLES=5. Self-heal включён. Сообщения — только по факту сделок.")
    port = int(os.environ.get("PORT","5000"))
    app.run(host="0.0.0.0", port=port)
