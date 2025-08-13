# =========================
# main.py — Bitget SPOT EMA 7/14 (устойчивые market-ордера)
# =========================
import os, time, json, hmac, base64, hashlib, logging, threading, requests
from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext, ROUND_DOWN
from flask import Flask, request as _flask_request

# ---------- Decimal ----------
getcontext().prec = 28

# ---------- Конфиг (ЗАПОЛНИ при необходимости) ----------
API_KEY        = "bg_7bd202760f36727cedf11a481dbca611"
API_SECRET     = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
API_PASSPHRASE = "Evgeniy84"

TELEGRAM_TOKEN   = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

BITGET = "https://api.bitget.com"  # v2

SYMBOLS = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","TRXUSDT","PEPEUSDT","BGBUSDT"]

# Торговые параметры
MIN_QUOTE_USDT = Decimal("10")   # желаемая сумма покупки (в USDT) на сделку
TP_PCT = Decimal("0.010")        # 1.0% take profit
SL_PCT = Decimal("0.007")        # 0.7% stop loss
EMA_FAST = 7
EMA_SLOW = 14
MIN_CANDLES = 5
CHECK_INTERVAL = 30              # сек
MAX_OPEN_POS = 2
NO_SIGNAL_COOLDOWN_MIN = 60      # напоминание «сигнала нет» раз в N мин
DAILY_REPORT_UTC = "20:47"

# ---------- Логи ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bot")

# ---------- Flask keep-alive ----------
app = Flask(__name__)
@app.get("/")
def health(): return "OK", 200

# ---------- Telegram ----------
def notify(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=8
        )
    except Exception as e:
        log.warning(f"TG send error: {e}")

# ---------- Подпись Bitget ----------
def _now_ms() -> str: return str(int(time.time()*1000))

def _sign(ts: str, method: str, path: str, body: str="") -> str:
    msg = ts + method.upper() + path + body
    digest = hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()

def _headers(ts: str, sign: str):
    return {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": API_PASSPHRASE,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }

def get_json_or_raise(method: str, path: str, params: dict=None, json_body: dict=None):
    url = BITGET + path
    ts = _now_ms()
    body_str = json.dumps(json_body, separators=(",",":")) if json_body else ""
    # при GET Bitget ожидает подпись без query (у v2 ок), но с самим path
    sign = _sign(ts, method, path if not params else path, body_str)
    kwargs = {"headers": _headers(ts, sign), "timeout": 20}
    if params: kwargs["params"] = params
    if json_body: kwargs["data"] = body_str
    r = requests.request(method, url, **kwargs)
    txt = r.text
    try:
        d = r.json()
    except Exception:
        raise RuntimeError(f"HTTP {r.status_code}: {txt}")
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {txt}")
    if d.get("code") not in ("00000", "0", 0):
        # Bitget иногда возвращает "code":"00000"
        # Пробрасываем как исключение, чтобы наверх ушло в notify
        raise RuntimeError(f"bitget_error:{d}")
    return d

# ---------- Метаданные символов ----------
class SymbolMeta:
    __slots__ = ("symbol","pricePrecision","quantityPrecision","quotePrecision","minTradeUSDT")
    def __init__(self, d):
        self.symbol = d["symbol"]
        self.pricePrecision    = int(d.get("pricePrecision", d.get("priceScale", 6)))
        self.quantityPrecision = int(d.get("quantityPrecision", d.get("quantityScale", 6))) # base
        self.quotePrecision    = int(d.get("quotePrecision", 6))                            # quote(USDT)
        # minTradeUSDT бывает в v2 сразу, а в v1/других — иногда нет
        mt = d.get("minTradeUSDT") or d.get("minTradeAmount") or "1"
        self.minTradeUSDT      = Decimal(str(mt))

SYMBOL_META = {}
_META_TS = 0

def load_symbol_meta(force=False):
    global _META_TS
    if not force and (time.time() - _META_TS) < 600 and SYMBOL_META:
        return
    d = get_json_or_raise("GET", "/api/v2/spot/public/symbols")
    data = d.get("data", [])
    picked = 0
    for row in data:
        sym = row.get("symbol")
        if sym in SYMBOLS:
            SYMBOL_META[sym] = SymbolMeta(row)
            picked += 1
    _META_TS = time.time()
    if picked == 0:
        raise RuntimeError("no symbols meta loaded")

def meta(s: str) -> SymbolMeta:
    if s not in SYMBOL_META: load_symbol_meta()
    return SYMBOL_META[s]

# ---------- Рынок/баланс ----------
def dg(x) -> Decimal: return Decimal(str(x))

def last_price(symbol: str) -> Decimal:
    d = get_json_or_raise("GET", f"/api/v2/spot/market/tickers?symbol={symbol}")
    arr = d.get("data") or []
    if not arr: raise RuntimeError("ticker empty")
    row = arr[0]
    for k in ("lastPr","close","last","c"):
        v = row.get(k)
        if v not in (None,""):
            return dg(v)
    raise RuntimeError("ticker no price")

def candles_close(symbol: str, limit: int=120):
    d = get_json_or_raise("GET", "/api/v2/spot/market/candles",
                          params={"symbol": symbol, "period":"1min", "limit": limit})
    rows = list(reversed(d.get("data") or []))
    closes = []
    for r in rows:
        if isinstance(r, (list, tuple)) and len(r) >= 5:
            closes.append(dg(r[4]))
        elif isinstance(r, dict):
            for k in ("close","last","c"):
                if k in r:
                    closes.append(dg(r[k])); break
    return closes

def free_balance(coin: str) -> Decimal:
    d = get_json_or_raise("GET", "/api/v2/spot/account/assets")
    for a in d.get("data", []):
        if a.get("coin") == coin:
            return dg(a.get("available","0"))
    return Decimal("0")

# ---------- EMA / сигнал ----------
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

# ---------- Хранилища ----------
STATE_FILE  = "positions.json"
PROFIT_FILE = "profit.json"

def _load(path, default):
    try:
        with open(path,"r",encoding="utf-8") as f: return json.load(f)
    except Exception: return default

def _save(path, data):
    with open(path,"w",encoding="utf-8") as f: json.dump(data,f,ensure_ascii=False,indent=2)

positions = _load(STATE_FILE, {})
profits   = _load(PROFIT_FILE, {"total":0.0,"trades":[]})
_last_no_signal = datetime.now(timezone.utc) - timedelta(minutes=NO_SIGNAL_COOLDOWN_MIN+1)

# ---------- Округления ----------
def round_down(val: Decimal, precision: int) -> Decimal:
    quant = Decimal(1).scaleb(-precision)
    return val.quantize(quant, rounding=ROUND_DOWN)

# ---------- Ордеры ----------
def place_order(symbol: str, side: str, order_type: str, size_str: str):
    body = {
        "symbol": symbol,
        "side": side,                # "buy" / "sell"
        "orderType": order_type,     # "market"
        "force": "gtc",
        "size": size_str
    }
    r = get_json_or_raise("POST", "/api/v2/spot/trade/orders", json_body=body)
    return (r.get("data") or {}).get("orderId")

def market_buy_quote(symbol: str, quote_usdt: Decimal):
    """BUY: size = сумма в USDT (котируемая валюта)"""
    m = meta(symbol)
    need = max(quote_usdt, m.minTradeUSDT)
    size = round_down(need, m.quotePrecision)

    if size < m.minTradeUSDT or size <= 0:
        notify(f"❕ {symbol}: покупка пропущена — минимум {m.minTradeUSDT} USDT, после округления {size}.")
        return None

    usdt = free_balance("USDT")
    if usdt < size:
        notify(f"❕ {symbol}: мало USDT ({usdt}), нужно {size}.")
        return None

    oid = place_order(symbol, "buy", "market", f"{size}")
    return oid

def market_sell_base(symbol: str, qty: Decimal):
    """SELL: size = количество базовой монеты"""
    m = meta(symbol)
    px = last_price(symbol)
    notional = qty * px
    if notional < m.minTradeUSDT:
        notify(f"❕ {symbol}: продажа пропущена — {notional:.6f} < {m.minTradeUSDT} USDT.")
        return None

    size = round_down(qty, m.quantityPrecision)
    if size <= 0:
        notify(f"❕ {symbol}: продажа невозможна — после округления размер 0.")
        return None

    base = symbol.replace("USDT","")
    free = free_balance(base)
    if free <= 0:
        notify(f"❕ {symbol}: нет свободного {base} для продажи.")
        return None
    size = min(size, free)

    oid = place_order(symbol, "sell", "market", f"{size}")
    return oid

# ---------- Торговля ----------
def try_open_position():
    global _last_no_signal

    if len(positions) >= MAX_OPEN_POS:
        return

    chosen = None
    for s in SYMBOLS:
        if s in positions: continue
        try:
            cl = candles_close(s, max(EMA_SLOW+20, 120))
            if len(cl) < MIN_CANDLES: continue
            if ema_signal(cl) == "long":
                chosen = s; break
        except Exception as e:
            log.warning(f"{s} candles error: {e}")

    if not chosen:
        if datetime.now(timezone.utc) - _last_no_signal > timedelta(minutes=NO_SIGNAL_COOLDOWN_MIN):
            notify(f"По рынку нет сигнала (EMA {EMA_FAST}/{EMA_SLOW}).")
            _last_no_signal = datetime.now(timezone.utc)
        return

    s = chosen
    try:
        oid = market_buy_quote(s, MIN_QUOTE_USDT)
        if not oid: return

        px = last_price(s)
        qty_rough = Decimal(MIN_QUOTE_USDT) / px  # только для оценки, позицию ведём в qty≈
        positions[s] = {
            "qty": float(qty_rough),
            "avg": float(px),
            "amount": float(qty_rough * px),
            "opened": datetime.now(timezone.utc).isoformat()
        }
        _save(STATE_FILE, positions)
        notify(f"✅ Покупка {s}: ~{qty_rough:.8f} по {px:.8f} (≈{(qty_rough*px):.6f} USDT).")
    except Exception as e:
        notify(f"❗ Ошибка покупки {s}: {e}")

def manage_positions():
    global positions, profits
    to_close = []
    for s, pos in list(positions.items()):
        try:
            px  = last_price(s)
            avg = Decimal(str(pos["avg"]))
            qty = Decimal(str(pos["qty"]))
            chg = (px - avg)/avg
            reason = None
            if chg >= TP_PCT: reason = "TP"
            elif chg <= -SL_PCT: reason = "SL"
            if not reason: continue

            if market_sell_base(s, qty):
                pnl = (px - avg) * qty
                profits["total"] = float(Decimal(str(profits["total"])) + pnl)
                profits["trades"].append({
                    "symbol": s, "qty": float(qty), "buy": float(avg), "sell": float(px),
                    "pnl": float(pnl), "closed": datetime.now(timezone.utc).isoformat(), "reason": reason
                })
                _save(PROFIT_FILE, profits)
                notify(f"💰 {reason} {s}: {avg:.6f}→{px:.6f}, qty≈{qty:.8f}, PnL={pnl:.6f} USDT. "
                       f"Итого: {profits['total']:.6f} USDT.")
                to_close.append(s)
        except Exception as e:
            log.warning(f"manage {s} error: {e}")
    for s in to_close: positions.pop(s, None)
    if to_close: _save(STATE_FILE, positions)

# ---------- Отчёт / команды ----------
def profit_text():
    total = profits.get("total",0.0)
    rows  = profits.get("trades",[])
    lines = [f"📊 Итоговая прибыль: {total:.6f} USDT"]
    if positions:
        lines.append("Открытые позиции:")
        for s,p in positions.items():
            lines.append(f"• {s}: qty≈{p['qty']}, avg={p['avg']:.8f}")
    if rows:
        lines.append("Последние сделки:")
        for t in rows[-5:]:
            lines.append(f"• {t['symbol']} ({t['reason']}): {t['qty']} шт, "
                         f"{t['buy']:.6f}→{t['sell']:.6f}, PnL={t['pnl']:.6f}")
    else:
        lines.append("Сделок ещё не было.")
    return "\n".join(lines)

def status_text():
    try: usdt = free_balance("USDT")
    except Exception: usdt = Decimal("0")
    lines = [
        "🛠 Статус",
        f"Баланс USDT: {usdt}",
        f"Сделка (BUY size): {MIN_QUOTE_USDT} USDT",
        f"Открытых позиций: {len(positions)}/{MAX_OPEN_POS}",
        f"EMA {EMA_FAST}/{EMA_SLOW}, TP {TP_PCT*100:.1f}%, SL {SL_PCT*100:.1f}%, MIN_CANDLES={MIN_CANDLES}",
    ]
    if positions:
        for s,p in positions.items():
            lines.append(f"• {s}: qty≈{p['qty']}, avg={p['avg']:.8f}")
    return "\n".join(lines)

def telegram_loop():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    offset = None
    last_daily = None
    while True:
        try:
            params = {"timeout": 25}
            if offset is not None: params["offset"] = offset
            r = requests.get(url, params=params, timeout=30).json()
            if r.get("ok"):
                for upd in r.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or upd.get("edited_message") or {}
                    text = (msg.get("text") or "").strip().lower()
                    chat = str((msg.get("chat") or {}).get("id") or TELEGRAM_CHAT_ID)
                    if text.startswith("/profit"):
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                      data={"chat_id": chat, "text": profit_text()}, timeout=8)
                    elif text.startswith("/status"):
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                      data={"chat_id": chat, "text": status_text()}, timeout=8)
        except Exception:
            time.sleep(2)

        try:
            hhmm = datetime.now(timezone.utc).strftime("%H:%M")
            if hhmm == DAILY_REPORT_UTC and last_daily != hhmm:
                last_daily = hhmm
                notify("🗓 Ежедневный отчёт:\n" + profit_text())
        except Exception:
            pass

def trade_loop():
    while True:
        try:
            manage_positions()
            try_open_position()
        except Exception as e:
            log.exception(f"trade loop error: {e}")
        time.sleep(CHECK_INTERVAL)

# ---------- Старт ----------
if __name__ == "__main__":
    threading.Thread(target=trade_loop, daemon=True).start()
    threading.Thread(target=telegram_loop, daemon=True).start()
    notify(f"🤖 Бот запущен! EMA {EMA_FAST}/{EMA_SLOW}, TP {TP_PCT*100:.1f}%, SL {SL_PCT*100:.1f}%. "
           f"MIN_CANDLES={MIN_CANDLES}. Сообщения — только по факту сделок.")
    port = int(os.environ.get("PORT","5000"))
    app.run(host="0.0.0.0", port=port)
