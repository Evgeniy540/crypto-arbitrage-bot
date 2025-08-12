# === main.py (Bitget SPOT + EMA 9/21 + TP/SL + Telegram + Flask keep-alive) ===
import os, time, hmac, hashlib, base64, json, threading, math, logging, requests
from datetime import datetime, timedelta
from flask import Flask

# ---------- КЛЮЧИ (BITGET SPOT) ----------
API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
API_PASSPHRASE = "Evgeniy84"

# ---------- TELEGRAM ----------
TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

# ---------- НАСТРОЙКИ ----------
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT", "PEPEUSDT", "BGBUSDT"]
TRADE_AMOUNT = 10.0           # базовая сумма покупки в USDT
TP_PCT = 0.015                # +1.5%
SL_PCT = 0.010                # -1.0%
CHECK_INTERVAL = 30           # сек
NO_SIGNAL_COOLDOWN_MIN = 60   # не чаще одного раза в час
CANDLE_SEC = 60               # 1m
CANDLES_LIMIT = 100           # нужно >= 21

STATE_FILE = "positions.json"
PROFIT_FILE = "profit.json"

BITGET_SPOT = "https://api.bitget.com"

# ---------- ЛОГГЕР ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bot")

# ---------- FLASK KEEP-ALIVE ----------
app = Flask(__name__)

@app.route("/")
def health():
    return "OK", 200

# ---------- УТИЛИТЫ ----------
def tg(text: str):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": text})
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

positions = load_json(STATE_FILE, {})   # { "BTCUSDT": {"qty": ..., "avg": ..., "amount": ..., "opened": "iso"} }
profits   = load_json(PROFIT_FILE, {"total": 0, "trades": []})
last_no_signal_sent = datetime.utcnow() - timedelta(minutes=NO_SIGNAL_COOLDOWN_MIN+1)

# ---------- BITGET: MARKET DATA ----------
def get_products():
    # публичный список продуктов со скейлами и minTradeUSDT
    url = BITGET_SPOT + "/api/spot/v1/public/products"
    r = requests.get(url, timeout=15)
    data = get_json_or_raise(r)
    if data.get("code") != "00000":
        raise RuntimeError(f"products error: {data}")
    return data["data"]

def get_symbol_rules(symbol):
    # вернёт словарь: {priceScale, quantityScale, minTradeUSDT}
    prods = get_products()
    for p in prods:
        if p.get("symbol") == symbol:
            return {
                "priceScale": int(p.get("priceScale", 4)),
                "quantityScale": int(p.get("quantityScale", 4)),
                "minTradeUSDT": float(p.get("minTradeUSDT", 1.0))
            }
    # дефолты на всякий
    return {"priceScale": 4, "quantityScale": 4, "minTradeUSDT": 1.0}

def get_ticker_price(symbol) -> float:
    url = BITGET_SPOT + f"/api/spot/v1/market/tickers?symbol={symbol}"
    r = requests.get(url, timeout=15)
    data = get_json_or_raise(r)
    if data.get("code") != "00000":
        raise RuntimeError(f"ticker error: {data}")
    arr = data.get("data", [])
    if not arr:
        raise RuntimeError("empty ticker")
    return float(arr[0]["lastPr"])

def get_candles(symbol, granularity=CANDLE_SEC, limit=CANDLES_LIMIT):
    url = BITGET_SPOT + f"/api/spot/v1/market/candles?symbol={symbol}&period={granularity}&limit={limit}"
    r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    data = get_json_or_raise(r)
    if data.get("code") != "00000":
        raise RuntimeError(f"candles error: {data}")
    # формат: [[ts, open, high, low, close, vol], ...] в строках, newest first
    rows = data.get("data", [])
    if not rows:
        return []
    # перевернём от старых к новым
    rows = list(reversed(rows))
    closes = [float(x[4]) for x in rows]
    return closes

# ---------- BITGET: ACCOUNT ----------
def get_usdt_balance() -> float:
    ts = now_ms()
    path = "/api/spot/v1/account/assets"
    q = "coin=USDT"
    sign = sign_payload(ts, "GET", path + "?" + q, "")
    r = requests.get(BITGET_SPOT + path, params={"coin": "USDT"}, headers=headers(ts, sign), timeout=15)
    data = get_json_or_raise(r)
    if data.get("code") != "00000": raise RuntimeError(f"balance error: {data}")
    arr = data.get("data", [])
    if not arr: return 0.0
    return float(arr[0].get("available", "0"))

# ---------- BITGET: ORDERS ----------
def place_market_order(symbol, side, size, clientOid=None):
    ts = now_ms()
    path = "/api/spot/v1/trade/orders"
    body = {
        "symbol": symbol,
        "side": side.lower(),    # "buy" | "sell"
        "orderType": "market",
        "force": "gtc",
        "size": f"{size}"
    }
    payload = json.dumps(body, separators=(",", ":"))
    sign = sign_payload(ts, "POST", path, payload)
    r = requests.post(BITGET_SPOT + path, headers=headers(ts, sign), data=payload, timeout=20)
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
    if len(closes) < 21: return None  # недостаточно данных
    e9  = ema(closes, 9)
    e21 = ema(closes, 21)
    # выровняем по хвосту
    shift = len(e21) - len(e9)
    if shift > 0: e21 = e21[shift:]
    if len(e9) == 0 or len(e21) == 0: return None
    # последний бар
    if e9[-2] <= e21[-2] and e9[-1] > e21[-1]:
        return "long"
    if e9[-2] >= e21[-2] and e9[-1] < e21[-1]:
        return "short"  # для спота «short» не используем — просто нет сигнала
    return None

# ---------- ОКРУГЛЕНИЕ ПОД ПРАВИЛА ----------
def floor_to_scale(x, scale):
    m = 10 ** scale
    return math.floor(x * m) / m

# ---------- ЛОГИКА ПОКУПКИ ----------
def maybe_buy_first_signal():
    global positions, last_no_signal_sent
    # если уже есть открытая позиция по любому символу — не покупать всё подряд
    if any(sym in positions for sym in SYMBOLS):
        return

    chosen = None
    for sym in SYMBOLS:
        try:
            closes = get_candles(sym, CANDLE_SEC, CANDLES_LIMIT)
            if len(closes) < 21:
                log.info(f"{sym}: недостаточно свечей ({len(closes)})")
                continue
            sig = ema_9_21_signal(closes)
            if sig == "long":
                chosen = sym
                break
        except Exception as e:
            log.warning(f"{sym}: candles error: {e}")

    if not chosen:
        # антиспам «нет сигнала» раз в час
        if datetime.utcnow() - last_no_signal_sent >= timedelta(minutes=NO_SIGNAL_COOLDOWN_MIN):
            tg("По рынку сейчас нет сигнала на вход (EMA 9/21).")
            last_no_signal_sent = datetime.utcnow()
        return

    sym = chosen
    try:
        rules = get_symbol_rules(sym)  # priceScale, quantityScale, minTradeUSDT
        price = get_ticker_price(sym)
        usdt_avail = get_usdt_balance()

        amount = min(TRADE_AMOUNT, usdt_avail)
        if amount < max(1.0, rules["minTradeUSDT"]):
            tg(f"Недостаточно USDT для {sym}. Баланс: {usdt_avail:.4f} USDT, минимум для ордера: {max(1.0, rules['minTradeUSDT']):.4f} USDT.")
            return

        raw_qty = amount / price
        qty = floor_to_scale(raw_qty, rules["quantityScale"])

        # попытка поднять qty, чтобы пройти minTradeUSDT (если округлением сильно «съело»)
        if qty * price < max(1.0, rules["minTradeUSDT"]):
            need_qty = (max(1.0, rules["minTradeUSDT"]) / price) * 1.0001
            qty = floor_to_scale(need_qty, rules["quantityScale"])

        # финальная проверка
        notional = qty * price
        if qty <= 0 or notional < max(1.0, rules["minTradeUSDT"]):
            tg(f"❗ {sym}: сумма сделки после округления {notional:.6f} USDT < минимума {max(1.0, rules['minTradeUSDT']):.6f}. Увеличь TRADE_AMOUNT.")
            return

        # маркет-покупка
        od = place_market_order(sym, "buy", f"{qty:.{rules['quantityScale']}f}")
        positions[sym] = {
            "qty": qty,
            "avg": price,            # упрощённо берём текущий тикер как среднюю
            "amount": notional,
            "opened": datetime.utcnow().isoformat()
        }
        save_json(STATE_FILE, positions)
        tg(f"✅ Покупка {sym}: qty={qty}, цена≈{price:.8f}, сумма≈{notional:.4f} USDT.\n(Правила: qScale={rules['quantityScale']}, minTradeUSDT={rules['minTradeUSDT']})")
    except Exception as e:
        msg = str(e)
        tg(f"❗ Ошибка покупки {sym}: {msg}")
        log.exception(f"buy error {sym}: {e}")

# ---------- ЛОГИКА ПРОДАЖИ (TP/SL) ----------
def check_tp_sl():
    global positions, profits
    to_close = []
    for sym, pos in positions.items():
        try:
            price = get_ticker_price(sym)
            avg = pos["avg"]
            change = (price - avg) / avg
            if change >= TP_PCT or change <= -SL_PCT:
                # продаём всё количество
                rules = get_symbol_rules(sym)
                qty = floor_to_scale(float(pos["qty"]), rules["quantityScale"])
                if qty <= 0:
                    to_close.append(sym)
                    continue
                od = place_market_order(sym, "sell", f"{qty:.{rules['quantityScale']}f}")
                pnl = (price - avg) * qty
                profits["total"] += pnl
                profits["trades"].append({
                    "symbol": sym,
                    "qty": qty,
                    "buy": avg,
                    "sell": price,
                    "pnl": pnl,
                    "closed": datetime.utcnow().isoformat()
                })
                save_json(PROFIT_FILE, profits)
                tg(f"💰 Продажа {sym}: qty={qty}, buy={avg:.8f}, sell={price:.8f}, PnL={pnl:.4f} USDT.\nИтого накоплено: {profits['total']:.4f} USDT.")
                to_close.append(sym)
        except Exception as e:
            log.warning(f"tp/sl error {sym}: {e}")

    for sym in to_close:
        positions.pop(sym, None)
    if to_close:
        save_json(STATE_FILE, positions)

# ---------- ОСНОВНОЙ ЦИКЛ ----------
def run_bot():
    tg("🤖 Бот запущен на Render! EMA 9/21, TP 1.5%, SL 1.0%.")
    while True:
        try:
            check_tp_sl()
            maybe_buy_first_signal()
        except Exception as e:
            log.exception(f"loop error: {e}")
        time.sleep(CHECK_INTERVAL)

# ---------- СТАРТ ----------
if __name__ == "__main__":
    # поток бота
    t = threading.Thread(target=run_bot, daemon=True)
    t.start()

    # Flask для Render Web Service (иначе — тайм-аут портов)
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
