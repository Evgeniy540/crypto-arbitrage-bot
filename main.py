# -*- coding: utf-8 -*-
"""
Bitget SPOT (SPBL) — EMA9/21, покупка по quantity с
- пред-проверкой минимума ордера (notional и minBaseQty)
- пред-проверкой баланса (USDT)
- автоповторами на 40808 (scale) и 45110 (min notional в ответе)
- мягким сигналом (EMA9 > EMA21 или недавний кросс)
- TP/SL + кулдаун

ENV:
  BITGET_API_KEY, BITGET_API_SECRET, BITGET_PASSPHRASE
  TG_TOKEN, TG_CHAT (необязательно)
  TRADE_USDT (дефолт 1.50), TP_PCT (0.015), SL_PCT (0.01)
"""

import os, time, hmac, json, math, re, hashlib, base64
from decimal import Decimal, ROUND_DOWN
import requests

# ----------------- настройки -----------------
PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "TRXUSDT", "XRPUSDT"]
TIMEFRAME = "1min"
CANDLES = 60
EMA_FAST, EMA_SLOW = 9, 21
SOFT_MARGIN = 0.0005
COOLDOWN_SEC = 15 * 60
TRADE_USDT = float(os.getenv("TRADE_USDT", "1.5"))
TP_PCT = float(os.getenv("TP_PCT", "0.015"))
SL_PCT = float(os.getenv("SL_PCT", "0.010"))

TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT  = os.getenv("TG_CHAT", "")

BG_KEY = os.getenv("BITGET_API_KEY", "")
BG_SECRET = os.getenv("BITGET_API_SECRET", "")
BG_PASS = os.getenv("BITGET_PASSPHRASE", "")
BASE = "https://api.bitget.com"

session = requests.Session()
session.headers.update({"Content-Type": "application/json", "locale": "en-US"})
session.timeout = 20

def now_ms(): return int(time.time()*1000)

def send_tg(text: str):
    if not TG_TOKEN or not TG_CHAT: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": text})
    except Exception:
        pass

# --------------- подпись/вызовы ---------------
def _sign(ts, method, path, body=""):
    pre = f"{ts}{method}{path}{body}"
    digest = hmac.new(BG_SECRET.encode(), pre.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()

def _auth(method, path, body=None):
    if body is None: body = ""
    elif not isinstance(body, str): body = json.dumps(body, separators=(",", ":"))
    ts = str(now_ms())
    headers = {
        "ACCESS-KEY": BG_KEY,
        "ACCESS-SIGN": _sign(ts, method.upper(), path, body),
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": BG_PASS,
        "Content-Type": "application/json",
        "locale": "en-US",
    }
    url = BASE + path
    r = session.request(method.upper(), url, data=body if method!="get" else None, headers=headers)
    r.raise_for_status()
    try: return r.json()
    except Exception: return {"code":"HTTP", "raw": r.text}

def jget(url, params=None):
    r = session.get(url, params=params); r.raise_for_status(); return r.json()

def quantize(val: float, scale: int) -> str:
    q = Decimal(str(val)).quantize(Decimal('1.' + '0'*scale), rounding=ROUND_DOWN)
    return f"{q:.{scale}f}"

# --------------- справочники биржи ---------------
# meta[plain] = {symbol, qtyPrec, minBaseQty, minNotionalUSDT}
meta = {}

def load_products():
    """Тянем продукты + попытка определить минимумы."""
    global meta
    items = jget(BASE + "/api/spot/v1/public/products").get("data", [])
    m = {}
    for it in items:
        base = it.get("baseCoin") or it.get("base")
        quote = it.get("quoteCoin") or it.get("quote")
        if not base or not quote: continue
        plain = f"{base}{quote}"
        if plain not in [p for p in PAIRS]:  # plain без _SPBL
            continue
        symbol = it.get("symbol") or f"{plain}_SPBL"
        qtyPrec = int(it.get("quantityScale") or it.get("quantityPrecision") or 4)

        # минималки — что удастся извлечь из разных схем полей
        minBaseQty = float(it.get("minTradeAmount") or it.get("minOrderAmt") or 0.0)
        # часть инстансов отдают minNotional (в USDT) — если нет, посчитаем по цене
        minNotionalUSDT = float(it.get("minTradeUSDT") or it.get("minTradeUsd") or 0.0)

        m[plain] = {
            "symbol": symbol,
            "qtyPrec": qtyPrec,
            "minBaseQty": minBaseQty,
            "minNotionalUSDT": minNotionalUSDT
        }
    meta = m
    send_tg("🔎 Bitget products loaded: " + ", ".join([meta[p]["symbol"] for p in meta]))

def last_price(plain):
    sym = meta[plain]["symbol"]
    r = jget(BASE + "/api/spot/v1/market/ticker", {"symbol": sym}).get("data", {})
    return float(r.get("close") or r.get("lastPr") or r.get("last") or 0.0)

def candles_close(plain, limit):
    sym = meta[plain]["symbol"]
    data = jget(BASE + "/api/spot/v1/market/candles", {"symbol": sym, "period": TIMEFRAME}).get("data", [])
    closes = []
    for row in data[:limit][::-1]:
        if isinstance(row, dict): closes.append(float(row.get("close")))
        else: closes.append(float(row[4]))
    return closes[-limit:]

def ema(vals, n):
    if len(vals) < n: return None
    k = 2/(n+1); e = sum(vals[:n])/n
    for v in vals[n:]: e = v*k + e*(1-k)
    return e

# --------------- баланс и минимумы ---------------
def get_usdt_balance() -> float:
    r = _auth("get", "/api/spot/v1/account/assets")
    if r.get("code") != "00000": return 0.0
    bal = 0.0
    for a in r.get("data", []):
        if (a.get("coin") or a.get("asset")) == "USDT":
            # в разных ответах поле может называться по-разному
            free = a.get("available") or a.get("availableAmt") or a.get("free") or "0"
            try: bal = float(free)
            except: bal = 0.0
            break
    return bal

def resolve_min_notional_usdt(plain) -> float:
    """Пытаемся знать минимальный notional в USDT для пары."""
    info = meta[plain]
    notional = float(info.get("minNotionalUSDT") or 0.0)
    if notional > 0: return notional
    # fallback: цена * minBaseQty (если есть)
    minBaseQty = float(info.get("minBaseQty") or 0.0)
    if minBaseQty > 0:
        p = max(1e-12, last_price(plain))
        return minBaseQty * p
    # крайнюю меру ставим 1 USDT (как часто пишет Bitget)
    return 1.0

# --------------- отправка ордеров ---------------
def _retry_scale(resp: dict, payload: dict):
    if resp.get("code") != "40808": return resp
    m = re.search(r"checkScale\s*=\s*(\d+)", str(resp))
    if not m: return resp
    scale = int(m.group(1))
    q = float(payload.get("quantity", 0))
    step = 10 ** (-scale)
    q = math.floor(q / step) * step
    if q <= 0: return resp
    payload["quantity"] = quantize(q, scale)
    send_tg(f"↩️ Retry 40808 scale={scale} qty={payload['quantity']} for {payload.get('symbol')}")
    return _auth("post", "/api/spot/v1/trade/orders", payload)

def market_buy_quantity(plain: str, want_usdt: float):
    # пред-проверки
    min_usdt = resolve_min_notional_usdt(plain)
    target_usdt = max(want_usdt, min_usdt)
    bal = get_usdt_balance()
    if bal < target_usdt:
        raise Exception(f"Insufficient balance: need ~{target_usdt:.4f} USDT, have {bal:.4f}")

    sym = meta[plain]["symbol"]
    qprec = int(meta[plain]["qtyPrec"])
    price = last_price(plain)
    if price <= 0: raise Exception(f"No price for {plain}")

    qty = float(Decimal(str(target_usdt / price)).quantize(Decimal('1.' + '0'*qprec), rounding=ROUND_DOWN))
    if qty <= 0: raise Exception(f"Computed qty <= 0 (target_usdt={target_usdt}, price={price})")

    payload = {
        "symbol": sym,
        "side": "buy",
        "orderType": "market",
        "force": "normal",
        "clientOid": f"buyq-{sym}-{now_ms()}",
        "quantity": quantize(qty, qprec),
    }
    r = _auth("post", "/api/spot/v1/trade/orders", payload)

    # 45110 — биржа вернула «минимум N USDT»: пробуем подтянуться к нему и повторить один раз
    if r.get("code") == "45110":
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*USDT", str(r))
        need = float(m.group(1)) if m else min_usdt
        target2 = max(target_usdt, need)
        qty2 = float(Decimal(str(target2 / price)).quantize(Decimal('1.' + '0'*qprec), rounding=ROUND_DOWN))
        if qty2 > 0 and target2 <= bal:
            payload["quantity"] = quantize(qty2, qprec)
            send_tg(f"↩️ Retry 45110 -> qty={payload['quantity']} (~{target2:.4f} USDT)")
            r = _auth("post", "/api/spot/v1/trade/orders", payload)
        else:
            raise Exception(f"retry failed: min {need} USDT, balance {bal:.4f}")

    if r.get("code") == "40808":
        r = _retry_scale(r, payload)

    if r.get("code") != "00000":
        raise Exception(f"{r}")

    return r.get("data", {}), float(payload["quantity"]), price

def market_sell_all(plain: str, qty: float):
    sym = meta[plain]["symbol"]; qprec = int(meta[plain]["qtyPrec"])
    qty = float(Decimal(str(qty)).quantize(Decimal('1.' + '0'*qprec), rounding=ROUND_DOWN))
    if qty <= 0: raise Exception("Sell qty <= 0")
    payload = {
        "symbol": sym, "side": "sell", "orderType": "market", "force": "normal",
        "clientOid": f"sellq-{sym}-{now_ms()}",
        "quantity": quantize(qty, qprec),
    }
    r = _auth("post", "/api/spot/v1/trade/orders", payload)
    if r.get("code") == "40808": r = _retry_scale(r, payload)
    if r.get("code") != "00000": raise Exception(f"{r}")
    return r.get("data", {}), float(payload["quantity"])

# --------------- стратегия ---------------
def want_enter(closes):
    if len(closes) < EMA_SLOW + 2: return False
    e9, e21 = ema(closes, EMA_FAST), ema(closes, EMA_SLOW)
    if e9 is None or e21 is None: return False
    cond_now = e9 > e21 * (1 + SOFT_MARGIN)
    cross = False
    if len(closes) >= EMA_SLOW + 3:
        e9p, e21p = ema(closes[:-1], EMA_FAST), ema(closes[:-1], EMA_SLOW)
        e9pp, e21pp = ema(closes[:-2], EMA_FAST), ema(closes[:-2], EMA_SLOW)
        cross = (e9pp is not None and e21pp is not None and e9p is not None and e21p is not None
                 and e9pp <= e21pp and e9p > e21p)
    return cond_now or cross

positions = {}  # plain -> {qty, entry}
cooldown  = {}  # plain -> ts

def want_exit(plain, last):
    pos = positions.get(plain); if not pos: return False, ""
    entry = pos["entry"]
    if last >= entry * (1 + TP_PCT): return True, "TP"
    if last <= entry * (1 - SL_PCT): return True, "SL"
    return False, ""

def cycle(plain):
    try:
        if plain not in meta: return
        closes = candles_close(plain, CANDLES)
        if not closes: send_tg(f"ℹ️ Пропуск {plain}: нет свечей"); return
        e9, e21, last = ema(closes, EMA_FAST), ema(closes, EMA_SLOW), closes[-1]
        send_tg(f"ℹ️ {plain}: EMA9/21 {e9:.6f} / {e21:.6f}")

        # выход
        pos = positions.get(plain)
        if pos:
            ok, why = want_exit(plain, last)
            if ok:
                try:
                    _, sold = market_sell_all(plain, pos["qty"])
                    send_tg(f"✅ SELL {plain} ({why}) qty={sold}")
                except Exception as ex:
                    send_tg(f"❗ Sell error {plain}: {ex}")
                finally:
                    positions.pop(plain, None)
                    cooldown[plain] = time.time()
            return  # одна позиция на пару одновременно

        # вход
        if time.time() - cooldown.get(plain, 0) < COOLDOWN_SEC: return
        if want_enter(closes):
            try:
                data, qty, price = market_buy_quantity(plain, TRADE_USDT)
                positions[plain] = {"qty": qty, "entry": price}
                send_tg(f"🟢 BUY {plain}: qty={qty}, price={price}")
            except Exception as ex:
                send_tg(f"❗ Buy error {plain}: {ex}")

    except Exception as e:
        send_tg(f"❗ Cycle error {plain}: {e}")

def main():
    if not (BG_KEY and BG_SECRET and BG_PASS):
        print("⚠️ BITGET_API_* envs missing"); return
    load_products()
    enabled = [p for p in PAIRS if p in meta]
    send_tg("🤖 Bitget SPOT (soft EMA, pre-checks) пары: " + ", ".join(meta[p]["symbol"] for p in enabled))
    while True:
        for p in enabled:
            cycle(p)
            time.sleep(1)
        time.sleep(5)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("bye")
