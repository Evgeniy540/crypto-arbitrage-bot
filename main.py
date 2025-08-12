# === main.py (Bitget SPOT + EMA 9/21 + TP/SL + Telegram + Flask keep-alive + /profit + daily report) ===
import os, time, hmac, hashlib, base64, json, threading, math, logging, requests
from datetime import datetime, timedelta
from flask import Flask

# ---------- КЛЮЧИ (BITGET SPOT) ----------
API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
API_PASSPHRASE = "Evgeniy84"

# ---------- TELEGRAM ----------
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"  # основной чат для уведомлений
# время ежедневного отчёта (по времени сервера); можно задать env DAILY_REPORT_HHMM="20:47"
DAILY_REPORT_HHMM = os.environ.get("DAILY_REPORT_HHMM", "20:47").strip()

# ---------- НАСТРОЙКИ ----------
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT", "PEPEUSDT", "BGBUSDT"]  # базовые имена
TRADE_AMOUNT = 10.0           # базовая сумма покупки в USDT
TP_PCT = 0.015                # +1.5%
SL_PCT = 0.010                # -1.0%
CHECK_INTERVAL = 30           # сек
NO_SIGNAL_COOLDOWN_MIN = 60   # «нет сигнала» не чаще 1/ч
CANDLE_PERIOD = "1min"        # Bitget спот ожидает строковый период
CANDLES_LIMIT = 100           # нужно >= 21

STATE_FILE = "positions.json"
PROFIT_FILE = "profit.json"

BITGET = "https://api.bitget.com"

# ---------- ЛОГГЕР ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bot")

# ---------- FLASK KEEP-ALIVE ----------
app = Flask(__name__)
@app.route("/")
def health():
    return "OK", 200

# ---------- УТИЛИТЫ ----------
def api_symbol(sym: str) -> str:
    return sym if sym.endswith("_SPBL") else f"{sym}_SPBL"

def tg(text: str, chat_id: str = None):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": chat_id or TELEGRAM_CHAT_ID, "text": text},
            timeout=10
        )
    except Exception as e:
        log.warning(f"TG send error: {e}")

def now_ms():
    return str(int(time.time() * 1000))

def sign_payload(ts: str, method: str, path: str, body: str = "") -> str:
    prehash = ts + method.upper() + path + body
    digest = hmac.new(API_SECRET.encode(), prehash.encode(), hashlib.sha256).digest()
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
        data = resp.json()
    except Exception:
        raise RuntimeError(f"HTTP {resp.status_code}: {txt}")
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {txt}")
    return data

# ---------- ХРАНИЛКИ ----------
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

positions = load_json(STATE_FILE, {})   # { "BTCUSDT": {"qty":..., "avg":..., "amount":..., "opened":...} }
profits   = load_json(PROFIT_FILE, {"total": 0.0, "trades": []})
last_no_signal_sent = datetime.utcnow() - timedelta(minutes=NO_SIGNAL_COOLDOWN_MIN+1)
_last_daily_report_date = None

# ---------- BITGET: MARKET DATA ----------
def get_products():
    url = BITGET + "/api/spot/v1/public/products"
    r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    data = get_json_or_raise(r)
    if data.get("code") != "00000":
        raise RuntimeError(f"products error: {data}")
    return data["data"]

_RULES_CACHE = {}
def get_symbol_rules(sym_no_sfx):
    sym = api_symbol(sym_no_sfx)
    if sym in _RULES_CACHE:
        return _RULES_CACHE[sym]
    prods = get_products()
    for p in prods:
        if p.get("symbol") == sym:
            rules = {
                "priceScale": int(p.get("priceScale", 4)),
                "quantityScale": int(p.get("quantityScale", 4)),
                "minTradeUSDT": float(p.get("minTradeUSDT", 1.0))
            }
            _RULES_CACHE[sym] = rules
            return rules
    log.warning(f"Rules not found for {sym}, using defaults.")
    rules = {"priceScale": 4, "quantityScale": 4, "minTradeUSDT": 1.0}
    _RULES_CACHE[sym] = rules
    return rules

def get_ticker_price(sym_no_sfx) -> float:
    sym = api_symbol(sym_no_sfx)
    url = BITGET + f"/api/spot/v1/market/tickers?symbol={sym}"
    r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    data = get_json_or_raise(r)
    if data.get("code") != "00000":
        raise RuntimeError(f"ticker error: {data}")
    arr = data.get("data", [])
    if not arr:
        raise RuntimeError("empty ticker")
    return float(arr[0]["lastPr"])

def get_candles(sym_no_sfx, period=CANDLE_PERIOD, limit=CANDLES_LIMIT):
    sym = api_symbol(sym_no_sfx)
    url = BITGET + f"/api/spot/v1/market/candles?symbol={sym}&period={period}&limit={limit}"
    r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    data = get_json_or_raise(r)
    if data.get("code") != "00000":
        raise RuntimeError(f"candles error: {data}")
    rows = data.get("data", [])
    if not rows:
        return []
    rows = list(reversed(rows))  # от старых к новым
    closes = [float(x[4]) for x in rows]
    return closes

# ---------- BITGET: ACCOUNT ----------
def get_usdt_balance() -> float:
    ts = now_ms()
    path = "/api/spot/v1/account/assets"
    q = "coin=USDT"
    sign = sign_payload(ts, "GET", path + "?" + q, "")
    r = requests.get(BITGET + path, params={"coin": "USDT"}, headers=headers(ts, sign), timeout=15)
    data = get_json_or_raise(r)
    if data.get("code") != "00000": raise RuntimeError(f"balance error: {data}")
    arr = data.get("data", [])
    if not arr: return 0.0
    return float(arr[0].get("available", "0"))

# ---------- BITGET: ORDERS ----------
def place_market_order(sym_no_sfx, side, size):
    ts = now_ms()
    path = "/api/spot/v1/trade/orders"
    body = {
        "symbol": api_symbol(sym_no_sfx),
        "side": side.lower(),      # "buy" | "sell"
        "orderType": "market",
        "force": "gtc",
        "size": str(size)
    }
    payload = json.dumps(body, separators=(",", ":"))
    sign = sign_payload(ts, "POST", path, payload)
    r = requests.post(BITGET + path, headers=headers(ts, sign), data=payload, timeout=20)
    data = get_json_or_raise(r)
    if data.get("code") != "00000":
        raise RuntimeError(f"order error: {data}")
    return data["data"]

# ---------- ТЕХНИКА: EMA ----------
def ema(values, period):
    if len(values) < period: return []
    k = 2 / (period + 1)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def ema_9_21_signal(closes):
    if len(closes) < 21: return None
    e9  = ema(closes, 9)
    e21 = ema(closes, 21)
    if len(e9) > len(e21):
        e9 = e9[-len(e21):]
    elif len(e21) > len(e9):
        e21 = e21[-len(e9):]
    if len(e9) < 2: return None
    if e9[-2] <= e21[-2] and e9[-1] > e21[-1]:
        return "long"
    if e9[-2] >= e21[-2] and e9[-1] < e21[-1]:
        return "short"
    return None

# ---------- МАТЕМАТИКА ОКРУГЛЕНИЙ ----------
def floor_to_scale(x, scale):
    if scale < 0: return x
    m = 10 ** scale
    return math.floor(x * m) / m

# ---------- ПОКУПКА ----------
def maybe_buy_first_signal():
    global positions, last_no_signal_sent
    if any(sym in positions for sym in SYMBOLS):
        return

    chosen = None
    for sym in SYMBOLS:
        try:
            closes = get_candles(sym, CANDLE_PERIOD, CANDLES_LIMIT)
            if len(closes) < 21:
                log.info(f"{sym}: недостаточно свечей ({len(closes)})")
                continue
            if ema_9_21_signal(closes) == "long":
                chosen = sym
                break
        except Exception as e:
            log.warning(f"{sym}: candles error: {e}")

    if not chosen:
        if datetime.utcnow() - last_no_signal_sent >= timedelta(minutes=NO_SIGNAL_COOLDOWN_MIN):
            tg("По рынку сейчас нет сигнала на вход (EMA 9/21).")
            last_no_signal_sent = datetime.utcnow()
        return

    sym = chosen
    try:
        rules = get_symbol_rules(sym)
        price = get_ticker_price(sym)
        usdt_avail = get_usdt_balance()

        amount = min(TRADE_AMOUNT, usdt_avail)
        min_usdt = max(1.0, rules["minTradeUSDT"])
        if amount < min_usdt:
            tg(f"Недостаточно USDT для {sym}. Баланс: {usdt_avail:.4f} USDT, минимум: {min_usdt:.4f} USDT.")
            return

        raw_qty = amount / price
        qty = floor_to_scale(raw_qty, rules["quantityScale"])

        if qty * price < min_usdt:
            need_qty = (min_usdt / price) * 1.0001
            qty = floor_to_scale(need_qty, rules["quantityScale"])

        notional = qty * price
        if qty <= 0 or notional < min_usdt:
            tg(f"❗ {sym}: сумма после округления {notional:.6f} USDT < минимума {min_usdt:.6f}. Увеличь TRADE_AMOUNT.")
            return

        place_market_order(sym, "buy", f"{qty:.{rules['quantityScale']}f}")
        positions[sym] = {
            "qty": qty,
            "avg": price,
            "amount": notional,
            "opened": datetime.utcnow().isoformat()
        }
        save_json(STATE_FILE, positions)
        tg(f"✅ Покупка {sym}: qty={qty}, цена≈{price:.8f}, сумма≈{notional:.4f} USDT.\n(qScale={rules['quantityScale']}, minTradeUSDT={rules['minTradeUSDT']})")
    except Exception as e:
        tg(f"❗ Ошибка покупки {sym}: {e}")
        log.exception(f"buy error {sym}: {e}")

# ---------- TP/SL ПРОДАЖА ----------
def check_tp_sl():
    global positions, profits
    to_close = []
    for sym, pos in list(positions.items()):
        try:
            price = get_ticker_price(sym)
            avg = pos["avg"]
            change = (price - avg) / avg
            if change >= TP_PCT or change <= -SL_PCT:
                rules = get_symbol_rules(sym)
                qty = floor_to_scale(float(pos["qty"]), rules["quantityScale"])
                if qty <= 0:
                    to_close.append(sym); continue
                min_usdt = max(1.0, rules["minTradeUSDT"])
                if qty * price < min_usdt:
                    tg(f"❗ Продажа {sym} отклонена: сумма {qty*price:.6f} < {min_usdt:.6f} USDT (слишком мало).")
                    to_close.append(sym); continue
                place_market_order(sym, "sell", f"{qty:.{rules['quantityScale']}f}")
                pnl = (price - avg) * qty
                profits["total"] += pnl
                profits["trades"].append({
                    "symbol": sym, "qty": qty, "buy": avg, "sell": price,
                    "pnl": pnl, "closed": datetime.utcnow().isoformat()
                })
                save_json(PROFIT_FILE, profits)
                tg(f"💰 Продажа {sym}: qty={qty}, buy={avg:.8f}, sell={price:.8f}, PnL={pnl:.4f} USDT.\nИтого: {profits['total']:.4f} USDT.")
                to_close.append(sym)
        except Exception as e:
            log.warning(f"tp/sl error {sym}: {e}")

    for sym in to_close:
        positions.pop(sym, None)
    if to_close:
        save_json(STATE_FILE, positions)

# ---------- /profit и ежедневный отчёт ----------
def format_profit_report():
    total = profits.get("total", 0.0)
    trades = profits.get("trades", [])
    lines = [f"📊 Отчёт по прибыли", f"Итоговая прибыль: {total:.4f} USDT"]
    if positions:
        lines.append("Открытые позиции:")
        for s, p in positions.items():
            lines.append(f"• {s}: qty={p['qty']}, avg={p['avg']:.8f}")
    if trades:
        lines.append("Последние сделки:")
        for t in trades[-5:]:
            lines.append(f"• {t['symbol']}: qty={t['qty']}, buy={t['buy']:.6f} → sell={t['sell']:.6f}, PnL={t['pnl']:.4f}")
    else:
        lines.append("Сделок ещё не было.")
    return "\n".join(lines)

def send_daily_report_if_time():
    global _last_daily_report_date
    hhmm = datetime.now().strftime("%H:%M")
    if hhmm == DAILY_REPORT_HHMM:
        today = datetime.now().date()
        if _last_daily_report_date != today:
            tg("🗓 Ежедневный отчёт:\n" + format_profit_report())
            _last_daily_report_date = today

# --- Telegram long-polling для команд (/profit) ---
def telegram_polling_loop():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    offset = None
    tg("🤖 Бот запущен на Render! EMA 9/21, TP 1.5%, SL 1.0%.")
    while True:
        try:
            params = {"timeout": 25}
            if offset: params["offset"] = offset
            r = requests.get(url, params=params, timeout=30)
            data = r.json()
            if not data.get("ok"):
                time.sleep(2); continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg: continue
                chat_id = str(msg["chat"]["id"])
                text = (msg.get("text") or "").strip()
                if text.lower().startswith("/profit"):
                    tg(format_profit_report(), chat_id=chat_id)
        except Exception:
            time.sleep(2)  # на всякий пожарный
        # ежедневный отчёт проверяем тут же
        try:
            send_daily_report_if_time()
        except Exception:
            pass

# ---------- ОСНОВНОЙ ЦИКЛ ТРЕЙДА ----------
def trading_loop():
    while True:
        try:
            check_tp_sl()
            maybe_buy_first_signal()
        except Exception as e:
            log.exception(f"loop error: {e}")
        time.sleep(CHECK_INTERVAL)

# ---------- СТАРТ ----------
if __name__ == "__main__":
    threading.Thread(target=trading_loop, daemon=True).start()
    threading.Thread(target=telegram_polling_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
