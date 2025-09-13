# -*- coding: utf-8 -*-
"""
EMA(9/21) сигнальный бот • KuCoin SPOT • STRONG/WEAK
- STRONG: подтверждённый кросс (EMA9/EMA21) + наклон + (опц.) ATR
- WEAK: near-cross (EPS-зона) и ретест после кросса
- Режимы: /mode strongonly | both
- Таймфрейм: 5m по умолчанию, fallback 1m (оба автоматически переводятся в формат KuCoin: 5min, 1min)
- Антиспам "нет сигнала", cooldown по символу, отчёты, задержка между запросами к API
- Команды: /help для списка
"""

import os
import time
import math
import threading
from datetime import datetime, timezone
from collections import defaultdict
from typing import Tuple, Optional, List

import requests
from flask import Flask

# ========== ТВОИ ДАННЫЕ ==========
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
# =================================

# ========== НАСТРОЙКИ ПО УМОЛЧАНИЮ ==========
DEFAULT_SYMBOLS = [
    "BTC-USDT","ETH-USDT","BNB-USDT","SOL-USDT","XRP-USDT","ADA-USDT","DOGE-USDT","TRX-USDT",
    "TON-USDT","LINK-USDT","LTC-USDT","DOT-USDT","ARB-USDT","OP-USDT","PEPE-USDT","SHIB-USDT"
]

# Таймфреймы вводим «по-человечески», а в запросе они будут автоматом переведены в формат KuCoin.
BASE_TF_HUMAN     = "5m"    # можно: 1m,3m,5m,15m,30m,1h,2h,4h,6h,8h,12h,1d,1w
FALLBACK_TF_HUMAN = "1m"

EMA_FAST, EMA_SLOW = 9, 21
CANDLES_NEED       = 100        # сколько свечей держим для расчёта
CHECK_INTERVAL_S   = 180        # пауза между циклами проверок
COOLDOWN_S         = 180        # минимум между сигналами по одному символу
SEND_NOSIG_EVERY   = 3600       # «нет сигнала» по конкретному символу не чаще, чем раз в час
THROTTLE_PER_SYMBOL_S = 0.25    # троттлинг между монетами, чтобы KuCoin не резал лимиты

MODE          = "both"          # "strongonly" | "both"
USE_ATR       = False
ATR_MIN_PCT   = 0.20/100        # для STRONG, если USE_ATR=True
SLOPE_MIN     = 0.00/100        # мин. наклон (%/бар) для STRONG
EPS_PCT       = 0.10/100        # «почти-кросс» зона для WEAK (чем больше — мягче)

REPORT_SUMMARY_EVERY = 30*60    # 30 минут
KUCOIN_BASE = "https://api.kucoin.com"

# ========== ВНУТРЕННИЕ ГЛОБАЛЫ ==========
app = Flask(__name__)

last_signal_ts = defaultdict(lambda: 0)
last_nosig_ts  = defaultdict(lambda: 0)
last_cross_dir = defaultdict(lambda: None)   # 'up'/'down' (последний реальный кросс)
last_summary_ts = 0

SETTINGS = {"symbols": sorted(DEFAULT_SYMBOLS)}

# ========== УТИЛИТЫ ==========
def now_ts() -> int:
    return int(time.time())

def ts_utc_str(ts: Optional[int] = None) -> str:
    ts = ts if ts is not None else now_ts()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def tg_send(text: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception:
        pass

def ema(series: List[float], period: int) -> List[Optional[float]]:
    if len(series) < period:
        return []
    k = 2.0 / (period + 1)
    out: List[Optional[float]] = [None] * (period - 1)
    ema_val = sum(series[:period]) / period
    out.append(ema_val)
    for x in series[period:]:
        ema_val = x * k + ema_val * (1 - k)
        out.append(ema_val)
    return out

def pct(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a - b) / b

# ----- Нормализация ТФ -----
_TF_MAP = {
    "1m":"1min","3m":"3min","5m":"5min","15m":"15min","30m":"30min",
    "1h":"1hour","2h":"2hour","4h":"4hour","6h":"6hour","8h":"8hour","12h":"12hour",
    "1d":"1day","1w":"1week"
}
def tf_human_to_kucoin(tf: str) -> str:
    tf = tf.strip().lower()
    # разрешаем сразу kucoin-формат, если вдруг ввели его
    if tf in _TF_MAP.values():
        return tf
    return _TF_MAP.get(tf, "5min")  # по умолчанию 5min

# ----- Получение свечей с ретраями -----
def kucoin_candles(symbol: str, tf_kucoin: str, need: int, max_retries: int = 3) -> Tuple[List[float], List[float], List[float]]:
    """
    KuCoin /api/v1/market/candles
    Возвращает списки closes, highs, lows в хронологическом порядке.
    """
    url = f"{KUCOIN_BASE}/api/v1/market/candles"
    params = {"type": tf_kucoin, "symbol": symbol}

    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, params=params, timeout=12)
            # Если перебор лимита — подождём и повторим
            if r.status_code in (429, 503):
                time.sleep(0.5 * attempt)
                continue
            r.raise_for_status()
            data = r.json().get("data", [])
            if not isinstance(data, list) or len(data) == 0:
                # пусто — попробуем ещё раз через маленькую паузу
                time.sleep(0.25 * attempt)
                continue
            # формат: [[ts, open, close, high, low, volume], ...] — новее сначала
            arr = list(reversed(data))[-max(need, EMA_SLOW + 3):]
            closes = [float(x[2]) for x in arr]
            highs  = [float(x[3]) for x in arr]
            lows   = [float(x[4]) for x in arr]
            return closes, highs, lows
        except Exception:
            time.sleep(0.4 * attempt)

    return [], [], []

def atr_percent(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    trs = []
    prev_close = closes[0]
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - prev_close),
            abs(lows[i] - prev_close),
        )
        trs.append(tr)
        prev_close = closes[i]
    atr = sum(trs[-period:]) / period
    price = closes[-1]
    return (atr / price) if price else None

# ========== АНАЛИТИКА ==========
def analyze_symbol(symbol: str, tf_human: str, need: int) -> Tuple[Optional[str], Optional[str], str]:
    """
    Возвращает: kind('STRONG'|'WEAK'|None), direction('up'|'down'|None), reason(str)
    """
    tf_kucoin = tf_human_to_kucoin(tf_human)
    closes, highs, lows = kucoin_candles(symbol, tf_kucoin, need)
    tf_used = tf_kucoin

    if len(closes) < max(need, EMA_SLOW + 2):
        # fallback 1m
        fb_kucoin = tf_human_to_kucoin(FALLBACK_TF_HUMAN)
        closes, highs, lows = kucoin_candles(symbol, fb_kucoin, need)
        tf_used = fb_kucoin

    if len(closes) < max(EMA_SLOW + 2, 30):
        return None, None, f"недостаточно данных ({len(closes)})"

    ema_fast = ema(closes, EMA_FAST)
    ema_slow = ema(closes, EMA_SLOW)
    if not ema_fast or not ema_slow:
        return None, None, "EMA not ready"

    c  = closes[-1]
    f1, f2 = ema_fast[-2], ema_fast[-1]
    s1, s2 = ema_slow[-2], ema_slow[-1]

    slope = pct(f2, f1)

    crossed_up   = (f1 is not None and s1 is not None and f1 <= s1 and f2 > s2)
    crossed_down = (f1 is not None and s1 is not None and f1 >= s1 and f2 < s2)

    dist_pct = abs(pct(f2, s2))
    near_cross = dist_pct <= EPS_PCT

    atrp = atr_percent(highs, lows, closes, period=14)

    # ---- STRONG ----
    strong_dir = None
    reasons = []
    if crossed_up and slope >= SLOPE_MIN:
        strong_dir = "up"; reasons.append(f"cross↑ & slope≥{SLOPE_MIN*100:.2f}%/бар")
    elif crossed_down and -slope >= SLOPE_MIN:
        strong_dir = "down"; reasons.append(f"cross↓ & |slope|≥{SLOPE_MIN*100:.2f}%/бар")

    if strong_dir and USE_ATR:
        if atrp is None or atrp < ATR_MIN_PCT:
            strong_dir = None
            reasons.append(f"ATR{(atrp or 0)*100:.2f}% < {ATR_MIN_PCT*100:.2f}%")

    if strong_dir:
        last_cross_dir[symbol] = strong_dir
        return "STRONG", strong_dir, "; ".join(reasons) + f", tf={tf_used}"

    # ---- WEAK ----
    if MODE == "both":
        if near_cross:
            direction = "up" if f2 >= s2 else "down"
            return "WEAK", direction, f"near-cross Δ≈{dist_pct*100:.3f}%, tf={tf_used}"

        if last_cross_dir[symbol] in ("up", "down"):
            dir_ = last_cross_dir[symbol]
            # ретест: fast возле slow после кросса, не пересекая
            if dir_ == "up" and f2 > s2 and dist_pct <= (EPS_PCT * 1.2):
                return "WEAK", "up", f"retest↑ Δ≈{dist_pct*100:.3f}%, tf={tf_used}"
            if dir_ == "down" and f2 < s2 and dist_pct <= (EPS_PCT * 1.2):
                return "WEAK", "down", f"retest↓ Δ≈{dist_pct*100:.3f}%, tf={tf_used}"

    return None, None, f"нет сигнала (tf={tf_used}, свечей={len(closes)})"

def format_signal(symbol: str, kind: str, direction: str, reason: str) -> str:
    arrow = "🟢LONG" if direction == "up" else "🔴SHORT"
    tag = "STRONG" if kind == "STRONG" else "weak"
    return (
        f"⚡ {symbol}: {arrow} <b>{tag}</b>\n"
        f"• EMA9/21: {reason}\n"
        f"• UTC: {ts_utc_str()}"
    )

# ========== TELEGRAM ==========
def tg_get_updates(offset=None):
    try:
        params = {"timeout": 0}
        if offset: params["offset"] = offset
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                         params=params, timeout=10)
        return r.json().get("result", [])
    except Exception:
        return []

def parse_cmd(text: str):
    parts = text.strip().split()
    if not parts:
        return None, []
    return parts[0].lower(), parts[1:]

def process_updates():
    global MODE, BASE_TF_HUMAN, COOLDOWN_S, CHECK_INTERVAL_S, EPS_PCT, SLOPE_MIN, USE_ATR, ATR_MIN_PCT
    last_update_id = None
    symbols = set(DEFAULT_SYMBOLS)

    tg_send(f"🤖 Бот запущен! Режим: <b>{MODE}</b>, tf={BASE_TF_HUMAN}, symbols={len(symbols)}")

    while True:
        for upd in tg_get_updates(last_update_id + 1 if last_update_id else None):
            last_update_id = upd["update_id"]
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            chat_id = str(msg.get("chat", {}).get("id"))
            if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
                continue

            text = (msg.get("text") or "").strip()
            if not text:
                continue

            cmd, args = parse_cmd(text)

            if cmd == "/help":
                tg_send(
                    "Команды:\n"
                    "/mode strongonly|both\n"
                    "/seteps 0.12    — EPS% для WEAK (0.12 = 0.12%)\n"
                    "/setslope 0.02  — мин. наклон %/бар для STRONG\n"
                    "/useatr on|off  — включить/выключить ATR для STRONG\n"
                    "/setatr 0.25    — мин. ATR% (0.25 = 0.25%)\n"
                    "/settf 1m|3m|5m|15m|30m|1h|4h|1d (вводишь по-человечески)\n"
                    "/setcooldown 180\n"
                    "/setcheck 120   — пауза между циклами\n"
                    "/setsymbols BTC-USDT,ETH-USDT,...\n"
                    "/status"
                )

            elif cmd == "/mode" and args and args[0].lower() in ("strongonly", "both"):
                MODE = args[0].lower()
                tg_send(f"✅ MODE: {MODE}")

            elif cmd == "/seteps" and args:
                try:
                    val = float(args[0]) / 100.0
                    if val <= 0: raise ValueError
                    EPS_PCT = val
                    tg_send(f"✅ EPS_PCT: {EPS_PCT*100:.3f}%")
                except Exception:
                    tg_send("❌ Пример: /seteps 0.10")

            elif cmd == "/setslope" and args:
                try:
                    SLOPE_MIN = float(args[0]) / 100.0
                    tg_send(f"✅ SLOPE_MIN: {SLOPE_MIN*100:.3f}%/бар")
                except Exception:
                    tg_send("❌ Пример: /setslope 0.02")

            elif cmd == "/useatr" and args:
                USE_ATR = (args[0].lower() == "on")
                tg_send(f"✅ USE_ATR: {USE_ATR}")

            elif cmd == "/setatr" and args:
                try:
                    ATR_MIN_PCT = float(args[0]) / 100.0
                    tg_send(f"✅ ATR_MIN_PCT: {ATR_MIN_PCT*100:.2f}%")
                except Exception:
                    tg_send("❌ Пример: /setatr 0.25")

            elif cmd == "/settf" and args:
                BASE_TF_HUMAN = args[0].lower()
                tg_send(f"✅ TF: {BASE_TF_HUMAN} (KuCoin type={tf_human_to_kucoin(BASE_TF_HUMAN)}, fallback={tf_human_to_kucoin(FALLBACK_TF_HUMAN)})")

            elif cmd == "/setcooldown" and args:
                try:
                    COOLDOWN_S = int(args[0]); tg_send(f"✅ COOLDOWN: {COOLDOWN_S}s")
                except Exception:
                    tg_send("❌ Пример: /setcooldown 180")

            elif cmd == "/setcheck" and args:
                try:
                    CHECK_INTERVAL_S = int(args[0]); tg_send(f"✅ CHECK: {CHECK_INTERVAL_S}s")
                except Exception:
                    tg_send("❌ Пример: /setcheck 120")

            elif cmd == "/setsymbols" and args:
                try:
                    arr = [x.strip().upper() for x in " ".join(args).replace(",", " ").split()]
                    if arr:
                        symbols = set(arr)
                        tg_send(f"✅ SYMBOLS: {len(symbols)}\n" + ", ".join(sorted(symbols))[:1000])
                except Exception:
                    tg_send("❌ Пример: /setsymbols BTC-USDT,ETH-USDT,TRX-USDT")

            elif cmd == "/status":
                tg_send(
                    f"Символов={len(symbols)}, tf={BASE_TF_HUMAN}→{FALLBACK_TF_HUMAN}, cooldown={COOLDOWN_S}s, режим={MODE}\n"
                    f"EPS={EPS_PCT*100:.2f}%, slope≥{SLOPE_MIN*100:.2f}%/бар, ATR{' ON' if USE_ATR else ' OFF'} ≥ {ATR_MIN_PCT*100:.2f}%"
                )

            SETTINGS["symbols"] = sorted(list(symbols))

        time.sleep(1)

# ========== РАБОЧИЙ ПОТОК ==========
def worker():
    global last_summary_ts
    while True:
        round_started = now_ts()
        for sym in SETTINGS["symbols"]:
            kind, direction, reason = analyze_symbol(sym, BASE_TF_HUMAN, CANDLES_NEED)

            if kind in ("STRONG", "WEAK"):
                if now_ts() - last_signal_ts[sym] >= COOLDOWN_S:
                    last_signal_ts[sym] = now_ts()
                    tg_send(format_signal(sym, kind, direction, reason))
            else:
                if now_ts() - last_nosig_ts[sym] >= SEND_NOSIG_EVERY:
                    last_nosig_ts[sym] = now_ts()
                    tg_send(f"ℹ️ {sym}: {reason}\nUTC: {ts_utc_str()}")

            # Лёгкая задержка между монетами (анти-лимиты KuCoin)
            time.sleep(THROTTLE_PER_SYMBOL_S)

        if now_ts() - last_summary_ts >= REPORT_SUMMARY_EVERY:
            last_summary_ts = now_ts()
            tg_send(
                f"✂️ Отчёт: символов={len(SETTINGS['symbols'])}, "
                f"tf={BASE_TF_HUMAN}→{FALLBACK_TF_HUMAN}, cooldown={COOLDOWN_S}s, режим={MODE}\n"
                f"UTC: {ts_utc_str()}"
            )

        # пауза до следующего круга
        elapsed = now_ts() - round_started
        sleep_left = max(1, CHECK_INTERVAL_S - elapsed)
        time.sleep(sleep_left)

# ========== FLASK KEEP-ALIVE ==========
app = Flask(__name__)

@app.route("/")
def root():
    return "OK"

def main():
    threading.Thread(target=process_updates, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    port = int(os.environ.get("PORT", "7860"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
