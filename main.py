import os
import time
import json
import math
import threading
import traceback
from datetime import datetime, timezone

import requests
import ccxt
from flask import Flask, jsonify

# =========================
# ─── ПАРАМЕТРЫ БОТА ───────────────────────────────────────────────────────────
# Все можно переопределить переменными окружения на Render
# =========================
PAIR_LIST = os.getenv("PAIR_LIST", "BTC/USDT,ETH/USDT,XRP/USDT,SOL/USDT,PEPE/USDT").split(",")
TF = os.getenv("TIMEFRAME", "1m")              # таймфрейм для сигналов
EMA_FAST = int(os.getenv("EMA_FAST", "7"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "14"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "1.0"))   # 1.0% по умолчанию
STOP_LOSS_PCT   = float(os.getenv("STOP_LOSS_PCT",   "0.7"))   # 0.7% по умолчанию
MIN_CANDLES = int(os.getenv("MIN_CANDLES", "5"))               # min history warmup
QUOTE_PER_TRADE_USDT = float(os.getenv("QUOTE_PER_TRADE", "10"))  # целевая сумма сделки в USDT
ONLY_DEAL_MESSAGES = os.getenv("ONLY_DEAL_MESSAGES", "1") == "1"  # присылать в TG только сделки/ошибки

# анти-спам: сколько секунд минимум между ошибочными уведомлениями по одной паре/типу
ERROR_COOLDOWN = int(os.getenv("ERROR_COOLDOWN", "90"))

# Telegram
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")

# Bitget API через CCXT
BITGET_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_PASSPHRASE = os.getenv("BITGET_API_PASS", "")  # Bitget требует passphrase

# Render health port
PORT = int(os.getenv("PORT", "10000"))

# =========================
# ─── ВСПОМОГАТЕЛЬНОЕ ─────────────────────────────────────────────────────────
# =========================

app = Flask(__name__)
last_error_push = {}      # {(symbol, code): ts}
open_trades = {}          # {symbol: {"side":"buy","entry":price,"tp":..,"sl":..,"amount":..,"id":..}}

def utcnow_iso():
    return datetime.now(timezone.utc).isoformat()

def tg_send(text: str):
    if not TG_TOKEN or not TG_CHAT:
        print(f"[TG skipped] {text}")
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {"chat_id": TG_CHAT, "text": text}
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print("TG ERROR:", e)

def ema(series, period):
    """Простая EMA без сторонних библиотек."""
    if len(series) < period:
        return [None] * len(series)
    k = 2 / (period + 1)
    out = [None] * len(series)
    # старт — SMA
    sma = sum(series[:period]) / period
    out[period - 1] = sma
    prev = sma
    for i in range(period, len(series)):
        prev = series[i] * k + prev * (1 - k)
        out[i] = prev
    return out

def throttle_error(symbol: str, code: str) -> bool:
    """Возвращает True если можно слать ошибку (не в охлаждении)."""
    key = (symbol, code)
    ts = time.time()
    last = last_error_push.get(key, 0)
    if ts - last >= ERROR_COOLDOWN:
        last_error_push[key] = ts
        return True
    return False

# =========================
# ─── БИРЖА (CCXT / BITGET) ───────────────────────────────────────────────────
# =========================

def build_exchange():
    # enable rate limit, spot only
    params = {
        "apiKey": BITGET_KEY,
        "secret": BITGET_SECRET,
        "password": BITGET_PASSPHRASE,
        "enableRateLimit": True,
        "options": {
            "defaultType": "spot"
        }
    }
    ex = ccxt.bitget(params)
    ex.load_markets()
    return ex

exchange = None

def fetch_candles(symbol: str, timeframe: str, limit: int = 200):
    """OHLCV -> [[ts, open, high, low, close, vol], ...]"""
    return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

def get_market(symbol: str):
    return exchange.market(symbol)

def get_balance_free(asset: str):
    bal = exchange.fetch_balance()
    wallets = bal.get(asset, {}) or {}
    return float(wallets.get("free", 0.0))

def round_amount(symbol: str, amount: float):
    m = get_market(symbol)
    precision = m["precision"]["amount"]
    # CCXT round
    return float(exchange.amount_to_precision(symbol, amount))

def round_price(symbol: str, price: float):
    return float(exchange.price_to_precision(symbol, price))

def min_cost_usdt(symbol: str) -> float:
    m = get_market(symbol)
    # попытка прочитать лимит стоимости (минимальный нотионал)
    limits = m.get("limits", {})
    cost = limits.get("cost", {})
    mn = cost.get("min")
    if mn:
        return float(mn)
    # если биржа не вернула, примем 10 USDT как дефолт
    return 10.0

# =========================
# ─── ТОРГОВЫЕ ФУНКЦИИ ───────────────────────────────────────────────────────
# =========================

def maybe_buy_signal(symbol: str):
    """EMA(7/14): пересечение снизу вверх => покупка."""
    candles = fetch_candles(symbol, TF, limit=max(EMA_SLOW + MIN_CANDLES, 50))
    closes = [c[4] for c in candles]
    if len(closes) < EMA_SLOW + MIN_CANDLES:
        return None  # мало истории

    efast = ema(closes, EMA_FAST)
    eslow = ema(closes, EMA_SLOW)

    # сигнал: предыдущая свеча ниже/равно, текущая выше
    if efast[-2] is None or eslow[-2] is None:
        return None

    crossed_up = (efast[-2] <= eslow[-2]) and (efast[-1] > eslow[-1])
    if not crossed_up:
        return None

    last_price = closes[-1]
    return {
        "price": last_price,
        "efast": efast[-1],
        "eslow": eslow[-1],
    }

def compute_amount_for_quote(symbol: str, quote_usdt: float, price: float) -> float:
    """Сколько монет купить на сумму quote_usdt с учетом precision."""
    if price <= 0:
        return 0.0
    raw_amount = quote_usdt / price
    amount = round_amount(symbol, raw_amount)
    return amount

def place_market_buy(symbol: str, quote_budget: float):
    """Маркет покупка с учетом минимального нотиона. Возвращает словарь ордера или бросает исключение."""
    market = get_market(symbol)
    base = market["base"]    # например BTC
    quote = market["quote"]  # должно быть USDT

    # учесть минимальный нотионал
    min_cost = min_cost_usdt(symbol)
    budget = max(quote_budget, min_cost)

    # проверить баланс USDT
    usdt_free = get_balance_free(quote)
    if usdt_free < budget:
        raise RuntimeError(f"Недостаточно {quote}: нужно ~{budget:.2f}, доступно {usdt_free:.2f}")

    ticker = exchange.fetch_ticker(symbol)
    last = float(ticker["last"])

    amount = compute_amount_for_quote(symbol, budget, last)
    if amount <= 0:
        raise RuntimeError("amount<=0 после округления")

    # повторно проконтролировать нотионал после округления
    notion = amount * last
    if notion < min_cost - 1e-8:
        # увеличим amount на минимально возможный шаг
        step_up = (min_cost / last) * 1.001
        amount = compute_amount_for_quote(symbol, step_up * last, last)
        notion = amount * last
        if notion < min_cost - 1e-8:
            raise RuntimeError(f"После округления нотионал {notion:.4f} < minCost {min_cost:.4f}")

    # разместить ордер
    order = exchange.create_order(symbol, type="market", side="buy", amount=amount)
    return order, last, amount

def place_take_profit_and_sl(symbol: str, entry_price: float, amount: float):
    """Пробуем создать TP и SL. Если биржа не поддерживает стоп-ордеры — ставим лимит TP, SL оставим на self-heal."""
    tp_price = round_price(symbol, entry_price * (1 + TAKE_PROFIT_PCT / 100.0))
    sl_price = round_price(symbol, entry_price * (1 - STOP_LOSS_PCT   / 100.0))

    created = {"tp": None, "sl": None}

    # Лимит на TP
    try:
        created["tp"] = exchange.create_order(symbol, type="limit", side="sell", amount=amount, price=tp_price)
    except Exception as e:
        if throttle_error(symbol, "tp"):
            tg_send(f"❗️Не удалось выставить TP {symbol} @ {tp_price}: {e}")

    # SL как стоп-маркет, если поддерживается
    try:
        params = {}
        # У разных бирж CCXT параметр стопа отличается. Для Bitget:
        # можно попробовать через params={"stopLossPrice": sl_price} или через create_order("market","sell",..., {"stopLossPrice":...})
        # Если не поддержит — словим исключение и отдадим на self-heal-мониторинг.
        params["stopLossPrice"] = sl_price
        created["sl"] = exchange.create_order(symbol, type="market", side="sell", amount=amount, params=params)
    except Exception as e:
        if throttle_error(symbol, "sl"):
            tg_send(f"⚠️ SL не выставлен на бирже {symbol}. Будет сопровождаться self‑heal. Детали: {e}")

    return created, tp_price, sl_price

# =========================
# ─── ОСНОВНОЙ ЦИКЛ ───────────────────────────────────────────────────────────
# =========================

def trader_loop():
    global exchange
    tg_send(f"🤖 Бот запущен! EMA {EMA_FAST}/{EMA_SLOW}, TP {TAKE_PROFIT_PCT}%, SL {STOP_LOSS_PCT}%. MIN_CANDLES={MIN_CANDLES}. Сообщения — только по факту сделок.")
    while True:
        try:
            for symbol in PAIR_LIST:
                symbol = symbol.strip()
                if not symbol:
                    continue

                # Если уже открыта сделка и нет TP/SL — сопровождаем (self-heal)
                if symbol in open_trades:
                    try:
                        monitor_trade(symbol)
                    except Exception as e:
                        if throttle_error(symbol, "monitor"):
                            tg_send(f"⚠️ Ошибка сопровождения {symbol}: {e}")
                    continue

                sig = maybe_buy_signal(symbol)
                if not sig:
                    # молчим чтобы не спамить
                    continue

                try:
                    order, last, amount = place_market_buy(symbol, QUOTE_PER_TRADE_USDT)
                except Exception as e:
                    # Нормализуем frequent ошибки
                    msg = str(e)
                    code = "order_error"
                    if "minCost" in msg or "минималь" in msg:
                        code = "min_cost"
                    elif "Недостаточно" in msg:
                        code = "insufficient"
                    elif "amount<=0" in msg:
                        code = "qty_zero"

                    if throttle_error(symbol, code):
                        tg_send(f"❗️Покупка {symbol} не выполнена: {msg}")
                    continue

                entry = float(order.get("price") or last)  # по маркету price может быть None
                created, tp, sl = place_take_profit_and_sl(symbol, entry, amount)

                open_trades[symbol] = {
                    "side": "buy",
                    "entry": entry,
                    "amount": amount,
                    "tp": tp,
                    "sl": sl,
                    "ts": time.time(),
                }

                tg_send(f"✅ Куплено {symbol}: amount≈{amount}, entry≈{entry:.6f}. TP≈{tp:.6f}, SL≈{sl:.6f}")

            time.sleep(5)   # частота обхода списка
        except Exception as loop_err:
            # глобальная защита: не падаем
            traceback.print_exc()
            if throttle_error("GLOBAL", "loop"):
                tg_send(f"⚠️ Цикл: {loop_err}")
            # пробуем пересоздать соединение с биржей при системной ошибке
            try:
                time.sleep(3)
                recreate_exchange()
            except Exception:
                pass
            time.sleep(2)

def recreate_exchange():
    global exchange
    try:
        ex = build_exchange()
        exchange = ex
    except Exception as e:
        raise RuntimeError(f"Не удалось инициализировать Bitget: {e}")

def monitor_trade(symbol: str):
    """Self‑heal сопровождение позиции: если цена достигла TP — позиция закрыта лимитом,
    если провалилась ниже SL — закроем маркетом (если SL не смогли поставить)."""
    data = open_trades.get(symbol)
    if not data:
        return
    amount = data["amount"]
    entry = data["entry"]
    tp = data["tp"]
    sl = data["sl"]

    ticker = exchange.fetch_ticker(symbol)
    last = float(ticker["last"])

    # если SL не удалось выставить на бирже — контролируем вручную
    if sl and last <= sl:
        # закрыть маркетом
        try:
            exchange.create_order(symbol, type="market", side="sell", amount=amount)
            tg_send(f"🛑 SL сработал {symbol}: close @ {last:.6f}")
        except Exception as e:
            if throttle_error(symbol, "heal_sl"):
                tg_send(f"❗️Не удалось закрыть по SL {symbol}: {e}")
        finally:
            open_trades.pop(symbol, None)
        return

    # TP может исполниться на бирже без нашего участия. Проверим остаток баланса base.
    base = get_market(symbol)["base"]
    bal = get_balance_free(base)
    # Если базовой монеты стало ≈0 (ниже 5% от купленного) — считаем, что позиция закрыта.
    if bal <= amount * 0.05:
        tg_send(f"🏁 {symbol}: позиция закрыта (вероятно TP).")
        open_trades.pop(symbol, None)

# =========================
# ─── FLASK (ХЕЛСЧЕК / СТАТУС) ────────────────────────────────────────────────
# =========================

@app.route("/")
def root():
    return jsonify({
        "ok": True,
        "ts": utcnow_iso(),
        "running": True,
        "pairs": PAIR_LIST,
        "ema": f"{EMA_FAST}/{EMA_SLOW}",
        "tp_pct": TAKE_PROFIT_PCT,
        "sl_pct": STOP_LOSS_PCT,
        "min_candles": MIN_CANDLES,
        "open_trades": open_trades,
    })

# =========================
# ─── ЗАПУСК ──────────────────────────────────────────────────────────────────
# =========================

def main():
    recreate_exchange()
    # прогрев: один запрос свечей на каждую пару, чтобы словить возможные ошибки фильтров
    for s in PAIR_LIST:
        try:
            fetch_candles(s.strip(), TF, limit=max(EMA_SLOW + MIN_CANDLES, 50))
        except Exception as e:
            if throttle_error(s, "candles"):
                tg_send(f"⚠️ {s}: candles_error {e}")

    th = threading.Thread(target=trader_loop, daemon=True)
    th.start()

    # Flask keep-alive сервер для Render
    app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
