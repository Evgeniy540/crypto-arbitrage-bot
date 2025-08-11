# -*- coding: utf-8 -*-
"""
Bitget SPOT (SPBL) EMA9/21 бот с безопасной торговлей:
- автоподбор точного символа *_SPBL
- самотест (цена, кол-во свечей)
- расчёт EMA9/21 и «мягкий» сигнал (не спамит)
- проверка баланса и мин. ноты (minTradeUSDT)
- маркет-покупка по quoteAmount (USDT), маркет-продажа по quantity
- корректное округление (quantityPrecision / pricePrecision)
- понятные Telegram-уведомления

Требуемые переменные окружения:
BITGET_API_KEY, BITGET_API_SECRET, BITGET_PASSPHRASE
TG_TOKEN (необязательно), TG_CHAT_ID (необязательно)
PAIRS="BTCUSDT,ETHUSDT,SOLUSDT,TRXUSDT,XRPUSDT"  (необязательно)
EMA_FAST=9  EMA_SLOW=21  INTERVAL=60  (сек, 60=1m, 300=5m)
BUY_USDT=1.20  MIN_USDT=1.10
COOL_SEC=30   (анти-спам по каждому символу)
"""

import os, time, hmac, hashlib, base64, json, math, threading
from datetime import datetime, timezone
from typing import Dict, Tuple, List
import requests

# ---------- ENV ----------
API_KEY = os.getenv("BITGET_API_KEY", "")
API_SECRET = os.getenv("BITGET_API_SECRET", "")
API_PASS = os.getenv("BITGET_PASSPHRASE", "")
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT_ID", "")
PAIRS = os.getenv("PAIRS", "BTCUSDT,ETHUSDT,SOLUSDT,TRXUSDT,XRPUSDT").split(",")
EMA_FAST = int(os.getenv("EMA_FAST", "9"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "21"))
INTERVAL = int(os.getenv("INTERVAL", "60"))  # seconds
BUY_USDT = float(os.getenv("BUY_USDT", "1.20"))
MIN_USDT = float(os.getenv("MIN_USDT", "1.10"))
COOL_SEC = int(os.getenv("COOL_SEC", "30"))

BASE = "https://api.bitget.com"

session = requests.Session()
session.headers.update({"Content-Type": "application/json"})

# ---------- UTILS ----------
def ts_ms() -> str:
    # Bitget ждёт ISO8601 с миллисекундами
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

def sign(method: str, path: str, body: str) -> Dict[str, str]:
    msg = f"{ts_ms()}{method}{path}{body}".encode()
    mac = hmac.new(API_SECRET.encode(), msg, hashlib.sha256).digest()
    sig = base64.b64encode(mac).decode()
    return {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": sig,
        "ACCESS-TIMESTAMP": ts_ms(),
        "ACCESS-PASSPHRASE": API_PASS,
        "Locale": "en-US",
    }

def tg(msg: str):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "disable_web_page_preview": True},
            timeout=8,
        )
    except Exception:
        pass

def get(path: str, params: dict = None, auth: bool = False):
    url = BASE + path
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    hdr = {}
    if auth:
        hdr = sign("GET", path if not params else f"{path}?"+ "&".join(f"{k}={v}" for k,v in params.items()), "")
    r = session.get(url, headers=hdr, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text}")
    return r.json()

def post(path: str, data: dict, auth: bool = True):
    body = json.dumps(data, separators=(",", ":"))
    hdr = sign("POST", path, body) if auth else {}
    r = session.post(BASE + path, data=body, headers=hdr, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text}")
    return r.json()

# ---------- MARKET META ----------
class SymMeta:
    def __init__(self, symbol, base, quote, min_usdt, q_prec, p_prec):
        self.symbol = symbol            # BTCUSDT_SPBL
        self.base = base                # BTC
        self.quote = quote              # USDT
        self.min_usdt = float(min_usdt) if min_usdt else 1.0
        self.q_prec = int(q_prec)
        self.p_prec = int(p_prec)

def load_products() -> Dict[str, SymMeta]:
    data = get("/api/spot/v1/public/products")
    book = {}
    for it in data.get("data", []):
        # интересуют *_SPBL (смесь нескольких ликовых пулов)
        if not it.get("symbol", "").endswith("_SPBL"):
            continue
        # приводим BTCUSDT
        plain = it["symbol"].replace("_SPBL", "")
        book[plain] = SymMeta(
            symbol=it["symbol"],
            base=it.get("baseCoin"),
            quote=it.get("quoteCoin"),
            min_usdt=it.get("minTradeUSDT") or it.get("minTradeUSDTForStrategy") or "1",
            q_prec=it.get("quantityPrecision", 8),
            p_prec=it.get("pricePrecision", 8)
        )
    return book

PRODUCTS = load_products()

# ---------- DATA ----------
def candles_spot(symbol_spbl: str, limit=60, granularity=INTERVAL) -> List[float]:
    # Bitget: /api/spot/v1/market/candles?symbol=BTCUSDT_SPBL&granularity=60&limit=60
    res = get("/api/spot/v1/market/candles", {
        "symbol": symbol_spbl, "granularity": granularity, "limit": limit
    })
    arr = res.get("data", [])
    # формат: [[ts, open, high, low, close, volume], ...] (строки)
    closes = [float(x[4]) for x in sorted(arr, key=lambda z: int(z[0]))]
    return closes

def ema(values: List[float], period: int) -> float:
    if len(values) < period:
        return values[-1]
    k = 2.0 / (period + 1.0)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e

def ticker_close(symbol_spbl: str) -> float:
    # /api/spot/v1/market/tickers?symbol=BTCUSDT_SPBL
    r = get("/api/spot/v1/market/tickers", {"symbol": symbol_spbl})
    d = r.get("data", [])
    if d and "close" in d[0]:
        return float(d[0]["close"])
    if d and "lastPr" in d[0]:
        return float(d[0]["lastPr"])
    raise RuntimeError("No close price in ticker")

# ---------- ACCOUNT ----------
def balance(coin: str) -> float:
    r = get("/api/spot/v1/account/assets", {"coin": coin}, auth=True)
    for it in r.get("data", []):
        if it["coin"].upper() == coin.upper():
            return float(it.get("available", "0"))
    return 0.0

def round_qty(q: float, prec: int) -> float:
    if prec < 0: prec = 0
    step = 10 ** prec
    return math.floor(q * step) / step

# ---------- ORDERS ----------
def market_buy_by_quote(symbol_spbl: str, meta: SymMeta, quote_usdt: float) -> dict:
    # Bitget принимает quoteAmount для market BUY
    body = {
        "symbol": symbol_spbl,
        "side": "buy",
        "orderType": "market",
        "force": "normal",
        "quoteAmount": f"{quote_usdt:.2f}"
    }
    return post("/api/spot/v1/trade/orders", body)

def market_sell_by_qty(symbol_spbl: str, qty: float) -> dict:
    body = {
        "symbol": symbol_spbl,
        "side": "sell",
        "orderType": "market",
        "force": "normal",
        "quantity": f"{qty}"
    }
    return post("/api/spot/v1/trade/orders", body)

# ---------- STRATEGY ----------
last_action_at: Dict[str, float] = {}   # анти-спам

def process_pair(plain: str):
    meta = PRODUCTS.get(plain)
    if not meta:
        tg(f"⚠️ Пара {plain} не найдена среди SPBL. Пропуск.")
        return

    symbol = meta.symbol
    # Самотест
    try:
        closes = candles_spot(symbol, limit=EMA_SLOW+30)
    except Exception as e:
        tg(f"❗ Ошибка свечей {plain}: {e}")
        return
    if len(closes) < EMA_SLOW + 1:
        tg(f"ℹ️ По {plain} мало свечей: {len(closes)}")
        return

    fast = ema(closes[-120:], EMA_FAST)
    slow = ema(closes[-120:], EMA_SLOW)
    last = closes[-1]

    tg(f"ℹ️ {plain}: EMA{EMA_FAST}/{EMA_SLOW}: {fast:.6f} / {slow:.6f} (last={last:.6f})")

    now = time.time()
    if now - last_action_at.get(plain, 0) < COOL_SEC:
        return

    # Условие входа (пример: мягкий «перекрест»)
    want_buy = last > fast > slow
    want_sell = last < slow < fast  # простой обратный

    # BUY
    if want_buy:
        usdt_avail = balance("USDT")
        lot = max(BUY_USDT, meta.min_usdt, MIN_USDT)
        if usdt_avail < lot:
            tg(f"ℹ️ Недостаточно USDT для {plain}. Нужно ≥ {lot:.2f}, есть {usdt_avail:.2f}")
            last_action_at[plain] = now
            return
        try:
            r = market_buy_by_quote(symbol, meta, lot)
            if r.get("code") == "00000":
                tg(f"✅ Покупка {plain} на {lot:.2f} USDT: OK")
            else:
                tg(f"❗ Ошибка покупки {plain}: {r}")
        except Exception as e:
            tg(f"❗ Ошибка покупки {plain}: {e}")
        last_action_at[plain] = now
        return

    # SELL: если есть монета и сигнал «слабый выход»
    if want_sell:
        coin_bal = balance(meta.base)
        if coin_bal <= 0:
            last_action_at[plain] = now
            return
        # продавать только если нота ≥ minTradeUSDT
        if coin_bal * last < max(meta.min_usdt, MIN_USDT) * 0.98:
            last_action_at[plain] = now
            return
        qty = round_qty(coin_bal, meta.q_prec)
        if qty <= 0:
            last_action_at[plain] = now
            return
        try:
            r = market_sell_by_qty(symbol, qty)
            if r.get("code") == "00000":
                tg(f"✅ Продажа {plain}: qty={qty}")
            else:
                tg(f"❗ Ошибка продажи {plain}: {r}")
        except Exception as e:
            tg(f"❗ Ошибка продажи {plain}: {e}")
        last_action_at[plain] = now

# ---------- MAIN LOOP ----------
def worker():
    tg("🤖 Bitget SPOT запущен (soft EMA, quantity-safe). Пары: " + ", ".join(sorted(PAIRS)))
    while True:
        try:
            for p in PAIRS:
                plain = p.strip().upper()
                # обеспечить наличие *_SPBL в кеше (на случай рестарта рынка)
                if plain not in PRODUCTS:
                    PRODUCTS.update(load_products())
                threading.Thread(target=process_pair, args=(plain,), daemon=True).start()
            time.sleep(INTERVAL)
        except Exception as e:
            tg(f"❗ Loop error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    # Короткая проверка ключей
    if not (API_KEY and API_SECRET and API_PASS):
        tg("⚠️ Нет API-ключей Bitget в переменных окружения.")
    # Запуск
    worker()
