# === main.py (HARD-CODED KEYS) — Bitget SPOT, EMA 9/21, TP +1.5%, SL -1.0% ===
# ВНИМАНИЕ: В этом файле ключи зашиты в коде по просьбе пользователя.
# Не выкладывайте его публично. Рекомендуется хранить в приватном репозитории.

import os, time, hmac, hashlib, base64, json, threading, logging
from datetime import datetime, timedelta
from flask import Flask, request
import requests

# -------- USER KEYS (HARD-CODED) --------
BITGET_API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"

TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

# -------- SETTINGS --------
SYMBOLS = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","TRXUSDT","PEPEUSDT","BGBUSDT"]
TIMEFRAME_SEC = 300   # 5m candles
EMA_FAST = 9
EMA_SLOW = 21
TP_PCT = 0.015     # +1.5%
SL_PCT = 0.010     # -1.0%
CHECK_INTERVAL = 30  # seconds between checks
NO_SIGNAL_INTERVAL = 3600  # once per hour per symbol

TRADE_AMOUNT_USDT = 10.0   # сумма сделки в USDT
MIN_BALANCE_BUFFER = 0.5   # запас, чтобы не утыкаться в пыль

POSITIONS_FILE = "positions.json"
PROFIT_FILE = "profit.json"
LOG_LEVEL = "INFO"

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s | %(levelname)s | %(message)s")

# -------- HTTP helpers --------
BASE_URL = "https://api.bitget.com"

def _ts():
    return str(int(time.time() * 1000))

def _sign(timestamp, method, path, body=""):
    message = f"{timestamp}{method}{path}{body}"
    mac = hmac.new(BITGET_API_SECRET.encode(), message.encode(), hashlib.sha256).digest()
    return base64.b64encode(mac).decode()

def _headers(method, path, body=""):
    ts = _ts()
    sign = _sign(ts, method, path, body)
    return {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": BITGET_API_PASSPHRASE,
        "Content-Type": "application/json",
        "locale": "en-US"
    }

def _get(path, params=None, auth=False):
    url = BASE_URL + path
    if not params:
        params = {}
    if auth:
        from urllib.parse import urlencode
        qs = "?" + urlencode(params) if params else ""
        headers = _headers("GET", path + qs, "")
        r = requests.get(url + qs, headers=headers, timeout=15)
    else:
        r = requests.get(url, params=params, timeout=15)
    if r.status_code != 200:
        raise Exception(f"HTTP {r.status_code}: {r.text}")
    return r.json()

def _post(path, payload):
    url = BASE_URL + path
    body = json.dumps(payload, separators=(',',':'))
    headers = _headers("POST", path, body)
    r = requests.post(url, headers=headers, data=body, timeout=15)
    if r.status_code != 200:
        raise Exception(f"HTTP {r.status_code}: {r.text}")
    return r.json()

# -------- Telegram --------
def tg(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        logging.error("Telegram error: %s", e)

# -------- Persistence --------
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

positions = load_json(POSITIONS_FILE, {})
profit = load_json(PROFIT_FILE, {"total_usdt": 0.0, "trades": []})

# -------- Indicators --------
def ema(series, period):
    k = 2/(period+1)
    ema_val = None
    out = []
    for v in series:
        if ema_val is None:
            ema_val = v
        else:
            ema_val = v*k + ema_val*(1-k)
        out.append(ema_val)
    return out

# -------- Market data --------
def get_candles(symbol, limit=100):
    try:
        resp = _get("/api/spot/v1/market/candles", params={"symbol": symbol, "granularity": TIMEFRAME_SEC, "limit": str(max(limit, EMA_SLOW+1))})
        if resp.get("code") != "00000":
            raise Exception(resp.get("msg", "unknown"))
        rows = resp.get("data", [])
        rows.reverse()  # oldest -> newest
        closes = [float(r[4]) for r in rows]
        return closes
    except Exception as e:
        raise Exception(f"candles error {symbol}: {e}")

def get_price(symbol):
    try:
        resp = _get("/api/spot/v1/market/ticker", params={"symbol": symbol})
        if resp.get("code") != "00000":
            raise Exception(resp.get("msg", "unknown"))
        data = resp.get("data", {})
        return float(data.get("lastPr") or data.get("last", 0.0))
    except Exception as e:
        raise Exception(f"price error {symbol}: {e}")

def get_balance(coin="USDT"):
    try:
        resp = _get("/api/spot/v1/account/assets", params={"coin": coin}, auth=True)
        if resp.get("code") != "00000":
            raise Exception(resp.get("msg", "unknown"))
        data = resp.get("data", [])
        if not data:
            return 0.0
        return float(data[0].get("available", 0.0))
    except Exception as e:
        raise Exception(f"balance error {coin}: {e}")

# -------- Trading --------
def market_buy(symbol, quote_usdt):
    payload = {
        "symbol": symbol,
        "side": "buy",
        "orderType": "market",
        "force": "normal",
        "quoteOrderQty": f"{quote_usdt:.6f}"
    }
    resp = _post("/api/spot/v1/trade/orders", payload)
    if resp.get("code") != "00000":
        raise Exception(resp.get("msg", "order buy failed"))
    return resp.get("data", {})

def market_sell(symbol, size):
    payload = {
        "symbol": symbol,
        "side": "sell",
        "orderType": "market",
        "force": "normal",
        "size": f"{size:.8f}"
    }
    resp = _post("/api/spot/v1/trade/orders", payload)
    if resp.get("code") != "00000":
        raise Exception(resp.get("msg", "order sell failed"))
    return resp.get("data", {})

def get_base_coin(symbol):
    return symbol.replace("USDT","")

def get_base_balance(symbol):
    coin = get_base_coin(symbol)
    return get_balance(coin)

# -------- Strategy --------
last_no_signal = {s: 0 for s in SYMBOLS}

def check_signal(symbol):
    closes = get_candles(symbol, limit=max(EMA_SLOW+10, 60))
    if len(closes) < EMA_SLOW+1:
        return {"signal": None, "reason": "Недостаточно данных"}
    ef = ema(closes, EMA_FAST)
    es = ema(closes, EMA_SLOW)
    if ef[-1] > es[-1] and ef[-2] <= es[-2]:
        return {"signal": "LONG", "price": closes[-1], "ema": (ef[-1], es[-1])}
    else:
        return {"signal": None, "reason": "Нет сигнала", "ema": (ef[-1], es[-1])}

def monitor_positions():
    to_close = []
    for symbol, pos in positions.items():
        try:
            price = get_price(symbol)
        except Exception as e:
            logging.warning("price check failed %s: %s", symbol, e)
            continue
        buy_price = pos["buy_price"]
        qty = pos["qty"]
        pnl_pct = (price - buy_price) / buy_price
        if pnl_pct >= TP_PCT or pnl_pct <= -SL_PCT:
            side = "TP" if pnl_pct >= TP_PCT else "SL"
            try:
                market_sell(symbol, qty)
                usdt_value = price * qty
                pnl_usdt = usdt_value - pos["spent_usdt"]
                profit["total_usdt"] += pnl_usdt
                profit["trades"].append({
                    "symbol": symbol,
                    "side": side,
                    "buy_price": buy_price, "sell_price": price,
                    "qty": qty,
                    "pnl_pct": round(pnl_pct*100, 4),
                    "pnl_usdt": round(pnl_usdt, 6),
                    "ts_close": int(time.time()*1000)
                })
                save_json(PROFIT_FILE, profit)
                tg(f"✅ {side} по {symbol}\nПродажа по ~{price:.6f}\nP/L: {pnl_pct*100:.3f}% ({pnl_usdt:.4f} USDT)\nСумм. прибыль: {profit['total_usdt']:.4f} USDT")
            except Exception as e:
                tg(f"❗ Ошибка продажи {symbol}: {e}")
                logging.error("sell failed %s: %s", symbol, e)
                continue
            to_close.append(symbol)
    for s in to_close:
        positions.pop(s, None)
    if to_close:
        save_json(POSITIONS_FILE, positions)

def trade_loop():
    tg("🤖 Бот запущен (Bitget SPOT, EMA 9/21, TP +1.5%, SL -1.0%).")
    while True:
        start = time.time()
        try:
            monitor_positions()
        except Exception as e:
            logging.error("monitor error: %s", e)

        for symbol in SYMBOLS:
            try:
                if symbol in positions:
                    continue
                sig = check_signal(symbol)
                if sig["signal"] == "LONG":
                    try:
                        usdt = get_balance("USDT")
                    except Exception as e:
                        tg(f"❗ Ошибка баланса USDT: {e}")
                        continue
                    need = TRADE_AMOUNT_USDT
                    if usdt < need + MIN_BALANCE_BUFFER:
                        tg(f"ℹ️ Недостаточно USDT для {symbol}. Баланс: {usdt:.4f}, нужно: {need:.2f}.")
                        continue
                    try:
                        market_buy(symbol, need)
                        time.sleep(0.5)
                        price = get_price(symbol)
                        est_qty = (need * (1 - 0.001)) / price  # приблизительно после комиссии
                        positions[symbol] = {
                            "qty": float(f"{est_qty:.8f}"),
                            "buy_price": price,
                            "spent_usdt": need,
                            "ts": int(time.time()*1000)
                        }
                        save_json(POSITIONS_FILE, positions)
                        tg(f"🟢 Покупка {symbol}\nСумма: {need:.2f} USDT\nЦена ~ {price:.6f}\nEMA9/21: {sig['ema'][0]:.6f} / {sig['ema'][1]:.6f}")
                    except Exception as e:
                        tg(f"❗ Ошибка покупки {symbol}: {e}")
                        logging.error("buy failed %s: %s", symbol, e)
                else:
                    now = time.time()
                    if now - last_no_signal.get(symbol, 0) > NO_SIGNAL_INTERVAL:
                        last_no_signal[symbol] = now
                        tg(f"ℹ️ По {symbol} сейчас нет сигнала. EMA9/21: {sig['ema'][0]:.6f} / {sig['ema'][1]:.6f}")
            except Exception as e:
                logging.error("loop symbol %s error: %s", symbol, e)

        elapsed = time.time() - start
        to_sleep = max(1, CHECK_INTERVAL - int(elapsed))
        time.sleep(to_sleep)

# -------- Flask (Render keep-alive & profit endpoint) --------
app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "Bitget SPOT bot is running", 200

@app.route("/profit", methods=["GET"])
def profit_status():
    p = load_json(PROFIT_FILE, {"total_usdt": 0.0, "trades": []})
    return {"total_usdt": p.get("total_usdt", 0.0), "trades": p.get("trades", [])}, 200

def run_flask():
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    t = threading.Thread(target=trade_loop, daemon=True)
    t.start()
    run_flask()
