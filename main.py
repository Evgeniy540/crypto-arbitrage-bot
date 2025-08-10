# === main.py (SPOT, history-candles fix) — EMA 9/21, TP +1.5%, SL -1.0% ===
import os, time, hmac, hashlib, base64, json, threading, logging
from flask import Flask
import requests

# -------- KEYS (hard-coded per user request) --------
BITGET_API_KEY = "bg_7bd202760f36727cedf11a481dbca611"
BITGET_API_SECRET = "b6bd206dfbe827ee5b290604f6097d781ce5adabc3f215bba2380fb39c0e9711"
BITGET_API_PASSPHRASE = "Evgeniy84"

TELEGRAM_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID = "5723086631"

# -------- SETTINGS --------
SYMBOLS = ["BTCUSDT_SPBL","ETHUSDT_SPBL","SOLUSDT_SPBL","XRPUSDT_SPBL","TRXUSDT_SPBL"]
TIMEFRAME_SEC = 300   # 5m
EMA_FAST = 9
EMA_SLOW = 21
TP_PCT = 0.015
SL_PCT = 0.010
CHECK_INTERVAL = 30
NO_SIGNAL_INTERVAL = 3600
TRADE_AMOUNT_USDT = 10.0
MIN_BALANCE_BUFFER = 0.5

POSITIONS_FILE = "positions.json"
PROFIT_FILE = "profit.json"
LOG_LEVEL = "INFO"

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s | %(levelname)s | %(message)s")

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
    params = params or {}
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

# ---- Telegram ----
def tg(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        logging.error("Telegram error: %s", e)

# ---- Files ----
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

# ---- Helpers ----
def ema(series, period):
    k = 2/(period+1)
    e = None
    out = []
    for v in series:
        e = v if e is None else v*k + e*(1-k)
        out.append(e)
    return out

# ---- Market data (SPOT with history-candles) ----
def get_candles(symbol_spbl, limit=120):
    resp = _get("/api/spot/v1/market/history-candles",
                params={"symbol": symbol_spbl, "granularity": TIMEFRAME_SEC, "limit": str(max(limit, EMA_SLOW+1))})
    if resp.get("code") != "00000":
        raise Exception(resp.get("msg", resp))
    rows = resp.get("data", [])
    rows.reverse()
    return [float(r[4]) for r in rows]

def get_price(symbol_spbl):
    resp = _get("/api/spot/v1/market/ticker", params={"symbol": symbol_spbl})
    if resp.get("code") != "00000":
        r2 = _get("/api/spot/v1/market/tickers", params={"symbol": symbol_spbl})
        if r2.get("code") != "00000" or not r2.get("data"):
            raise Exception(f"{resp} | {r2}")
        d = r2["data"][0]
        return float(d.get("lastPr") or d.get("last"))
    d = resp.get("data", {})
    return float(d.get("lastPr") or d.get("last"))

def get_balance(coin="USDT"):
    resp = _get("/api/spot/v1/account/assets", params={"coin": coin}, auth=True)
    if resp.get("code") != "00000":
        raise Exception(resp.get("msg","unknown"))
    arr = resp.get("data", [])
    if not arr:
        return 0.0
    return float(arr[0].get("available", 0.0))

# ---- Trading (SPBL) ----
def market_buy(symbol_spbl, quote_usdt):
    payload = {
        "symbol": symbol_spbl,
        "side": "buy",
        "orderType": "market",
        "force": "normal",
        "quoteOrderQty": f"{quote_usdt:.6f}"
    }
    resp = _post("/api/spot/v1/trade/orders", payload)
    if resp.get("code") != "00000":
        raise Exception(resp.get("msg", "order buy failed"))
    return resp.get("data", {})

def market_sell(symbol_spbl, size):
    payload = {
        "symbol": symbol_spbl,
        "side": "sell",
        "orderType": "market",
        "force": "normal",
        "size": f"{size:.8f}"
    }
    resp = _post("/api/spot/v1/trade/orders", payload)
    if resp.get("code") != "00000":
        raise Exception(resp.get("msg", "order sell failed"))
    return resp.get("data", {})

# ---- Strategy ----
last_no_signal = {s: 0 for s in SYMBOLS}
TP_PCT_F = TP_PCT
SL_PCT_F = SL_PCT

def ema_signal(symbol_spbl):
    closes = get_candles(symbol_spbl, limit=max(EMA_SLOW+10, 60))
    if len(closes) < EMA_SLOW+1:
        return {"signal": None, "reason": "Недостаточно данных"}
    ef = ema(closes, EMA_FAST)
    es = ema(closes, EMA_SLOW)
    if ef[-1] > es[-1] and ef[-2] <= es[-2]:
        return {"signal": "LONG", "price": closes[-1], "ema": (ef[-1], es[-1])}
    return {"signal": None, "reason": "Нет сигнала", "ema": (ef[-1], es[-1])}

# ---- Core loops ----
def monitor_positions():
    changed = False
    for symbol, pos in list(positions.items()):
        try:
            price = get_price(symbol)
        except Exception as e:
            logging.warning("price check failed %s: %s", symbol, e)
            continue
        buy = pos["buy_price"]; qty = pos["qty"]
        pnl = (price - buy) / buy
        if pnl >= TP_PCT_F or pnl <= -SL_PCT_F:
            side = "TP" if pnl >= TP_PCT_F else "SL"
            try:
                market_sell(symbol, qty)
                pnl_usdt = price*qty - pos["spent_usdt"]
                profit["total_usdt"] += pnl_usdt
                profit["trades"].append({
                    "symbol": symbol, "side": side,
                    "buy_price": buy, "sell_price": price,
                    "qty": qty, "pnl_pct": round(pnl*100,4),
                    "pnl_usdt": round(pnl_usdt,6), "ts_close": int(time.time()*1000)
                })
                save_json(PROFIT_FILE, profit)
                tg(f"✅ {side} по {symbol}\nПродажа ~{price:.6f}\nP/L: {pnl*100:.3f}% ({pnl_usdt:.4f} USDT)\nСумм. прибыль: {profit['total_usdt']:.4f} USDT")
                positions.pop(symbol, None); changed = True
            except Exception as e:
                tg(f"❗ Ошибка продажи {symbol}: {e}")
                logging.error("sell failed %s: %s", symbol, e)
    if changed:
        save_json(POSITIONS_FILE, positions)

def trade_loop():
    tg("🤖 Бот запущен (Bitget SPOT history-candles, EMA 9/21, TP +1.5%, SL -1.0%).")
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
                sig = ema_signal(symbol)
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
                        est_qty = (need * (1 - 0.001)) / price
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

        time.sleep(max(1, CHECK_INTERVAL - int(time.time()-start)))

# ---- Flask ----
app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "Bitget SPOT bot (history-candles) is running", 200

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
