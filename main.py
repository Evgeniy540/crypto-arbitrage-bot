# main.py
# -*- coding: utf-8 -*-
import os, time, hmac, hashlib, base64, json, threading, math, random
from datetime import datetime, timezone
from typing import Dict, Any, List, Tuple
import requests
from flask import Flask, request, jsonify

# =========  CONFIG  =========
EMA_FAST = 7
EMA_SLOW = 14
TAKE_PROFIT = 0.010   # 1.0%
STOP_LOSS   = 0.007   # 0.7%
MIN_CANDLES = 5       # минимум «полных» свечей до анализа
POLL_SEC    = 12      # частота опроса рынка
CANDLE_SEC  = 60      # гранулярность свечей, 60s
QUOTE_PER_TRADE_USDT = float(os.getenv("QUOTE_PER_TRADE_USDT", "10"))  # >= 1
ONLY_FACT_MSGS = True

# Telegram
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.getenv("TG_CHAT_ID", "")

# Bitget keys
BG_KEY   = os.getenv("BITGET_API_KEY", "")
BG_SEC   = os.getenv("BITGET_API_SECRET", "")
BG_PASS  = os.getenv("BITGET_PASSPHRASE", "")

# Universe
def _env_symbols() -> List[str]:
    raw = os.getenv("SYMBOLS", "")
    if not raw.strip():
        return ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","TRXUSDT","PEPEUSDT","BGBUSDT"]
    return [s.strip().upper() for s in raw.split(",") if s.strip()]

SYMBOLS = _env_symbols()

# =========  HELPERS  =========
SESSION = requests.Session()
SESSION.headers.update({"Content-Type": "application/json"})

def now_iso() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()

def tg_send(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        SESSION.post(url, json=payload, timeout=10)
    except Exception:
        pass

def clamp_quote(q: float) -> float:
    # Bitget: минимальный «квот» 1 USDT
    return 0.0 if q < 1.0 else q

def ema(series: List[float], period: int) -> List[float]:
    if not series or period <= 0:
        return []
    k = 2.0 / (period + 1.0)
    out = []
    ema_val = None
    for x in series:
        if ema_val is None:
            ema_val = x
        else:
            ema_val = x * k + ema_val * (1.0 - k)
        out.append(ema_val)
    return out

def xspbl(sym: str) -> str:
    s = sym.strip().upper()
    # Под Bitget SPOT формат TICKER_SPBL
    return s if s.endswith("_SPBL") else f"{s}_SPBL"

def ts_ms() -> str:
    # Bitget/OKX-совместимый таймштамп в секундах с мс как строка
    return str(int(time.time() * 1000))

def sign_bitget(timestamp: str, method: str, path: str, body: str) -> str:
    # Документация Bitget: prehash = timestamp + method + requestPath + body
    prehash = f"{timestamp}{method.upper()}{path}{body}"
    h = hmac.new(BG_SEC.encode(), prehash.encode(), hashlib.sha256).digest()
    return base64.b64encode(h).decode()

def bg_headers(ts: str, sign: str) -> Dict[str, str]:
    # В Bitget заголовки семейства ACCESS-* (аналогично OKX)
    return {
        "ACCESS-KEY": BG_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": BG_PASS,
        "Content-Type": "application/json",
        "X-CHANNEL-API-CODE": "bitget-python"
    }

def http_get(url: str, params: Dict[str, Any] = None, timeout: int = 15) -> Dict[str, Any]:
    r = SESSION.get(url, params=params, timeout=timeout)
    # Bitget на 4xx отдаёт json {"code":"400xxx", "msg": "..."}
    try:
        j = r.json()
    except Exception:
        j = {"http": r.status_code, "text": r.text}
    if r.status_code >= 400:
        j["http"] = r.status_code
    return j

def http_signed(method: str, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    base = "https://api.bitget.com"
    body = json.dumps(payload, separators=(",", ":")) if payload else ""
    ts = ts_ms()
    sig = sign_bitget(ts, method, path, body)
    url = base + path
    h = bg_headers(ts, sig)
    if method.upper() == "POST":
        r = SESSION.post(url, headers=h, data=body, timeout=15)
    else:
        r = SESSION.get(url, headers=h, params=payload, timeout=15)
    try:
        j = r.json()
    except Exception:
        j = {"http": r.status_code, "text": r.text}
    if r.status_code >= 400:
        j["http"] = r.status_code
    return j

# =========  BITGET MARKET  =========
def fetch_candles_spot(symbol_spbl: str, granularity_sec: int = 60, limit: int = 120) -> List[Tuple[int, float]]:
    """
    Возвращает [(ts_ms, close), ...] по SPOT символу.
    Bitget spot v1: /api/spot/v1/market/candles
    params: symbol, granularity (секунды), limit
    """
    url = "https://api.bitget.com/api/spot/v1/market/candles"
    params = {"symbol": symbol_spbl, "granularity": str(granularity_sec), "limit": str(limit)}
    j = http_get(url, params)
    # Успешный ответ: {"code":"00000","msg":"success","requestTime":..., "data":[[ts, open, high, low, close, vol], ...]}
    if not isinstance(j, dict) or j.get("code") != "00000":
        raise RuntimeError(f"candles_error for {symbol_spbl}: {j}")
    data = j.get("data", [])
    out = []
    for row in data:
        try:
            # Bitget ts как миллисекунды строкой
            ts = int(row[0])
            close = float(row[4])
            out.append((ts, close))
        except Exception:
            continue
    # По спецификации данные идут от свежего к старому — развернём
    out.sort(key=lambda x: x[0])
    return out

def fetch_ticker_price(symbol_spbl: str) -> float:
    url = "https://api.bitget.com/api/spot/v1/market/ticker"
    j = http_get(url, {"symbol": symbol_spbl})
    if not isinstance(j, dict) or j.get("code") != "00000":
        raise RuntimeError(f"ticker_error for {symbol_spbl}: {j}")
    data = j.get("data") or {}
    return float(data.get("close", "0"))

# =========  ORDERS (SPOT)  =========
def place_market_buy(symbol_spbl: str, quote_usdt: float) -> Dict[str, Any]:
    """
    Маркет-покупка: используем quoteOrderQty (сумма в USDT).
    """
    q = clamp_quote(float(quote_usdt))
    if q <= 0:
        # Ничего не шлём — чтобы не ловить 40019/45110
        return {"skipped": True, "reason": "qty_zero_fallback", "need": round(max(1.0, quote_usdt), 4)}
    path = "/api/spot/v1/trade/orders"
    payload = {
        "symbol": symbol_spbl,
        "side": "buy",
        "orderType": "market",
        "force": "normal",
        "quoteOrderQty": f"{q:.4f}"
    }
    j = http_signed("POST", path, payload)
    # Успешно: {"code":"00000","msg":"success","data":{"orderId":"..."}}
    return j

def place_market_sell(symbol_spbl: str, base_size: float) -> Dict[str, Any]:
    """
    Маркет-продажа: используем size (кол-во базовой монеты).
    """
    size = float(base_size)
    if size <= 0:
        return {"skipped": True, "reason": "size_zero"}
    path = "/api/spot/v1/trade/orders"
    payload = {
        "symbol": symbol_spbl,
        "side": "sell",
        "orderType": "market",
        "force": "normal",
        "size": f"{size:.8f}"
    }
    j = http_signed("POST", path, payload)
    return j

# =========  STRATEGY / STATE  =========
class Position:
    __slots__ = ("entry", "size")
    def __init__(self, entry: float, size: float):
        self.entry = float(entry)
        self.size  = float(size)

positions: Dict[str, Position] = {}

def ema_signal(closes: List[float]) -> str:
    if len(closes) < max(EMA_FAST, EMA_SLOW) + 2:
        return "none"
    e_fast = ema(closes, EMA_FAST)
    e_slow = ema(closes, EMA_SLOW)
    # Кросс последней полной свечи (берём -2 как «закрытую»)
    f_prev, s_prev = e_fast[-3], e_slow[-3]
    f_last, s_last = e_fast[-2], e_slow[-2]
    if f_prev <= s_prev and f_last > s_last:
        return "buy"
    if f_prev >= s_prev and f_last < s_last:
        return "sell"
    return "none"

def maybe_trade_symbol(symbol: str):
    spbl = xspbl(symbol)
    # 1) Свечи
    try:
        candles = fetch_candles_spot(spbl, granularity_sec=CANDLE_SEC, limit=200)
    except Exception as e:
        tg_send(f"⚠️ {symbol}: ошибка свечей: {e}")
        return
    if len(candles) < (max(EMA_FAST, EMA_SLOW) + MIN_CANDLES):
        return
    closes = [c for _, c in candles]
    signal = ema_signal(closes)

    # 2) Проверка TP/SL, если позиция есть
    pos = positions.get(spbl)
    try:
        price = fetch_ticker_price(spbl)
    except Exception as e:
        tg_send(f"⚠️ {symbol}: ошибка цены: {e}")
        return

    if pos:
        tp = pos.entry * (1.0 + TAKE_PROFIT)
        sl = pos.entry * (1.0 - STOP_LOSS)
        if price >= tp:
            # Продаём всю позицию
            sell = place_market_sell(spbl, pos.size)
            if sell.get("code") == "00000":
                tg_send(f"✅ TP {symbol}: {price:.6f} (вход {pos.entry:.6f})")
                positions.pop(spbl, None)
            else:
                # Мягкая обработка 4xx
                err = json.dumps(sell, ensure_ascii=False)
                tg_send(f"❗ Ошибка продажи {symbol}: {err}")
        elif price <= sl:
            sell = place_market_sell(spbl, pos.size)
            if sell.get("code") == "00000":
                tg_send(f"🛑 SL {symbol}: {price:.6f} (вход {pos.entry:.6f})")
                positions.pop(spbl, None)
            else:
                err = json.dumps(sell, ensure_ascii=False)
                tg_send(f"❗ Ошибка продажи {symbol}: {err}")

    # 3) Вход по сигналу (если позиции нет)
    if signal == "buy" and not positions.get(spbl):
        # Рассчитываем примерный размер базовой монеты (для данных и логов)
        base_est = QUOTE_PER_TRADE_USDT / max(1e-9, price)
        # MARKET BUY по quoteOrderQty — главное правило: >= 1 USDT
        resp = place_market_buy(spbl, QUOTE_PER_TRADE_USDT)
        if resp.get("code") == "00000":
            # Сохраняем позицию с примерным размером (для SL/TP)
            positions[spbl] = Position(entry=price, size=base_est)
            tg_send(f"🟢 Покупка {symbol}: ~{base_est:.8f} по ~{price:.6f} USDT")
        else:
            # Ловим типовые ошибки аккуратно
            if resp.get("skipped"):
                need = resp.get("need", 1.0)
                tg_send(f"❕ {symbol}: покупка пропущена (qty_zero_fallback). Баланс/QUOTE должен быть ≥ {need:.4f} USDT.")
            else:
                code = str(resp.get("code"))
                if code == "45110":
                    tg_send(f"❗ Ошибка покупки {symbol}: сумма меньше минимума 1 USDT.")
                elif code == "40019":
                    tg_send(f"❗ Ошибка покупки {symbol}: параметр quantity/quoteOrderQty пуст — защита сработала.")
                else:
                    tg_send(f"❗ Ошибка покупки {symbol}: {json.dumps(resp, ensure_ascii=False)}")

# =========  LOOP  =========
def boot_message():
    conf = f"EMA {EMA_FAST}/{EMA_SLOW}, TP {TAKE_PROFIT*100:.1f}%, SL {STOP_LOSS*100:.1f}%. MIN_CANDLES={MIN_CANDLES}."
    tg_send(f"🤖 Бот запущен! {conf} Сообщения — только по факту сделок.")

def worker():
    # Мягкий запуск
    boot_message()
    last_no_signal = 0.0
    while True:
        any_action = False
        for sym in SYMBOLS:
            try:
                maybe_trade_symbol(sym)
            except Exception as e:
                tg_send(f"❗ Ошибка символа {sym}: {e}")
            time.sleep(0.25)  # не долбим API
        # Информационное сообщение «нет сигнала» — не чаще раза в 20 минут
        if not ONLY_FACT_MSGS:
            now = time.time()
            if now - last_no_signal > 1200:
                tg_send(f"По рынку нет сигнала (EMA {EMA_FAST}/{EMA_SLOW}).")
                last_no_signal = now
        time.sleep(POLL_SEC)

# =========  FLASK (для Render)  =========
app = Flask(__name__)

@app.get("/")
def health():
    return jsonify(ok=True, time=now_iso(), running=True)

@app.post("/telegram")
def telegram_endpoint():
    # Запасной крючок под будущие команды
    try:
        data = request.json or {}
        text = (data.get("message") or {}).get("text","").strip()
        if text == "/status":
            open_pos = ", ".join([f"{k}:{v.size:.6f}@{v.entry:.6f}" for k,v in positions.items()]) or "нет"
            tg_send(f"ℹ️ Статус: позиций {open_pos}. QUOTE_PER_TRADE={QUOTE_PER_TRADE_USDT} USDT.")
        elif text == "/profit":
            tg_send("Пока считаем профит по факту TP/SL (учёт упрощённый).")
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 200
    return jsonify(ok=True)

def main():
    # Проверка ключей (торг возможен только при наличии ключей)
    if not (BG_KEY and BG_SEC and BG_PASS):
        tg_send("⚠️ Внимание: ключи Bitget не заданы — торговые ордера отключены.")
    # Стартуем фонового рабочего
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    # Flask-сервис
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
