# -*- coding: utf-8 -*-
"""
EMA(9/21) сигнальный бот • KuCoin SPOT • STRONG/WEAK сигналы
- STRONG: подтверждённый кросс + наклон + (опц.) ATR-порог
- WEAK: "почти-кросс" (EPS-зона) или ретест после кросса
- Режимы: /mode strongonly | both
- Таймфрейм: базово 5m, fallback 1m при нехватке данных
- Антиспам "нет сигнала", cooldown по символу
- Команды: /help (см. список)
"""

import os, time, math, threading, requests
from datetime import datetime, timezone
from collections import defaultdict, deque
from flask import Flask

# === ТВОИ ДАННЫЕ ===
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
# ===================

# -------- Настройки по умолчанию --------
DEFAULT_SYMBOLS = [
    "BTC-USDT","ETH-USDT","BNB-USDT","SOL-USDT","XRP-USDT","ADA-USDT","DOGE-USDT","TRX-USDT",
    "TON-USDT","LINK-USDT","LTC-USDT","DOT-USDT","ARB-USDT","OP-USDT","PEPE-USDT","SHIB-USDT"
]

BASE_TF          = "5m"    # базовый ТФ: 1m | 3m | 5m | 15m ...
FALLBACK_TF      = "1m"
CANDLES_NEED     = 100     # сколько свечей грузим (EMA сглаживается)
CHECK_INTERVAL_S = 180     # пауза между раундами проверок
COOLDOWN_S       = 180     # минимум между сигналами по одной монете
SEND_NOSIG_EVERY = 3600    # раз в час "нет сигнала" по символу

EMA_FAST, EMA_SLOW = 9, 21

# --- Фильтры сигналов ---
MODE = "both"              # "strongonly" | "both"
USE_ATR = False            # включить ATR-фильтр для STRONG
ATR_MIN_PCT = 0.20/100     # мин. дневной ATR% для STRONG (если USE_ATR=True)

SLOPE_MIN = 0.00/100       # мин. наклон (в % от цены/бар) для STRONG
EPS_PCT   = 0.10/100       # ширина "почти-кросс" зоны для WEAK (чем больше — мягче)

# --- Анти-спам и статус ---
REPORT_SUMMARY_EVERY = 30*60   # каждые 30 мин прислать краткий отчёт
KUCOIN_BASE = "https://api.kucoin.com"

app = Flask(__name__)

# -------- Глобальные состояния --------
last_signal_ts = defaultdict(lambda: 0)     # по монете
last_nosig_ts  = defaultdict(lambda: 0)
last_cross_dir = defaultdict(lambda: None)  # 'up'/'down' — последняя направлённость кросса (для ретестов)
last_summary_ts = 0

# ========== УТИЛИТЫ ==========
def now_ts() -> int:
    return int(time.time())

def ts_utc_str(ts=None):
    if ts is None: ts = now_ts()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def tg_send(text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode":"HTML"}, timeout=10)
    except Exception:
        pass

def ema(series, period):
    """Простая EMA без pandas."""
    if len(series) < period: return []
    k = 2/(period+1)
    out = []
    ema_val = sum(series[:period]) / period
    out.extend([None]*(period-1))
    out.append(ema_val)
    for x in series[period:]:
        ema_val = x * k + ema_val * (1-k)
        out.append(ema_val)
    return out

def pct(a, b):  # относит. разница (a-b)/b
    if b == 0: return 0.0
    return (a - b) / b

def kucoin_candles(symbol, tf, limit):
    # KuCoin: /api/v1/market/candles?type=5min&symbol=BTC-USDT
    # Ответ: [[time,open,close,high,low,volume], ...] в обратном порядке (новые сначала)
    url = f"{KUCOIN_BASE}/api/v1/market/candles"
    params = {"type": tf, "symbol": symbol}
    r = requests.get(url, params=params, timeout=12)
    r.raise_for_status()
    data = r.json().get("data", [])
    if not isinstance(data, list): return []
    # Разворачиваем в хронологический порядок и ограничиваем количеством
    arr = list(reversed(data))[-limit:]
    closes = [float(x[2]) for x in arr]  # close
    highs  = [float(x[3]) for x in arr]
    lows   = [float(x[4]) for x in arr]
    return closes, highs, lows

def atr_percent(highs, lows, closes, period=14):
    if len(closes) < period+1: return None
    trs = []
    prev_close = closes[0]
    for i in range(1, len(closes)):
        tr = max(highs[i]-lows[i], abs(highs[i]-prev_close), abs(lows[i]-prev_close))
        trs.append(tr)
        prev_close = closes[i]
    atr = sum(trs[-period:]) / period
    price = closes[-1]
    return (atr / price) if price else None

# ========== ЛОГИКА СИГНАЛОВ ==========
def analyze_symbol(symbol, tf, need):
    """Возвращает ('STRONG'|'WEAK'|None, direction 'up'|'down', reason:str)"""
    try:
        closes, highs, lows = kucoin_candles(symbol, tf, need)
        if len(closes) < need:  # fallback на 1m
            closes, highs, lows = kucoin_candles(symbol, FALLBACK_TF, need)
            tf_used = FALLBACK_TF
        else:
            tf_used = tf
        if len(closes) < max(EMA_SLOW+2, 30):
            return None, None, f"недостаточно данных ({len(closes)})"

        ema_fast = ema(closes, EMA_FAST)
        ema_slow = ema(closes, EMA_SLOW)
        if not ema_fast or not ema_slow: return None, None, "EMA not ready"

        # Берём последние точки
        c  = closes[-1]
        f1, f2 = ema_fast[-2], ema_fast[-1]
        s1, s2 = ema_slow[-2], ema_slow[-1]

        # Наклон быстр. EMA (в %/бар)
        slope = pct(f2, f1)

        # Детект кросса между предыдущей и текущей свечой
        crossed_up   = (f1 <= s1) and (f2 > s2)
        crossed_down = (f1 >= s1) and (f2 < s2)

        # «Почти-кросс»: расстояние fast/slow в пределах EPS_PCT от цены
        dist_pct = abs(pct(f2, s2))
        near_cross = dist_pct <= EPS_PCT

        # ATR-фильтр (по желанию)
        atrp = atr_percent(highs, lows, closes, period=14)

        # ====== Правила STRONG ======
        strong = None
        reason = []
        if crossed_up:
            strong = ("up" if slope >= SLOPE_MIN else None)
            if strong: reason.append(f"cross↑ & slope≥{SLOPE_MIN*100:.2f}%")
        elif crossed_down:
            strong = ("down" if -slope >= SLOPE_MIN else None)
            if strong: reason.append(f"cross↓ & |slope|≥{SLOPE_MIN*100:.2f}%")

        if strong and USE_ATR:
            if atrp is None or atrp < ATR_MIN_PCT:
                strong = None
                reason.append(f"ATR{(atrp or 0)*100:.2f}% < {ATR_MIN_PCT*100:.2f}%")

        if strong:
            last_cross_dir[symbol] = strong
            return "STRONG", strong, "; ".join(reason) + f", tf={tf_used}"

        # ====== Правила WEAK ======
        # 1) Почти-кросс в EPS-зоне
        if MODE == "both" and near_cross:
            direction = "up" if f2 >= s2 else "down"
            return "WEAK", direction, f"near-cross Δ≈{dist_pct*100:.3f}%, tf={tf_used}"

        # 2) Ретест после последнего кросса (fast вернулась к slow и оттолкнулась)
        if MODE == "both" and last_cross_dir[symbol] in ("up","down"):
            dir_ = last_cross_dir[symbol]
            # «ретест»: fast снаружи и снова сближается к slow, но не пересекает
            if dir_ == "up" and f2 > s2 and dist_pct <= (EPS_PCT*1.2):
                return "WEAK", "up", f"retest↑ Δ≈{dist_pct*100:.3f}%, tf={tf_used}"
            if dir_ == "down" and f2 < s2 and dist_pct <= (EPS_PCT*1.2):
                return "WEAK", "down", f"retest↓ Δ≈{dist_pct*100:.3f}%, tf={tf_used}"

        return None, None, f"нет сигнала (tf={tf_used}, свечей={len(closes)})"
    except Exception as e:
        return None, None, f"ошибка: {e}"

def format_signal(symbol, kind, direction, reason):
    arrow = "🟢LONG" if direction=="up" else "🔴SHORT"
    tag   = "STRONG" if kind=="STRONG" else "weak"
    return (f"⚡ {symbol}: {arrow} <b>{tag}</b>\n"
            f"• EMA9/21: {reason}\n"
            f"• Время (UTC): {ts_utc_str()}")

# ========== ТЕЛЕГРАМ КОМАНДЫ ==========
def parse_cmd(text):
    parts = text.strip().split()
    if not parts: return None, []
    return parts[0].lower(), parts[1:]

def tg_get_updates(offset=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 0}
    if offset: params["offset"] = offset
    try:
        r = requests.get(url, params=params, timeout=10).json()
        return r.get("result", [])
    except Exception:
        return []

def process_updates():
    last_update_id = None
    global MODE, BASE_TF, COOLDOWN_S, CHECK_INTERVAL_S, EPS_PCT, SLOPE_MIN, USE_ATR, ATR_MIN_PCT
    global REPORT_SUMMARY_EVERY
    symbols = set(DEFAULT_SYMBOLS)

    tg_send("🤖 Бот запущен! Режим: <b>%s</b>, tf=%s, symbols=%d" % (MODE, BASE_TF, len(symbols)))

    while True:
        for upd in tg_get_updates(last_update_id+1 if last_update_id else None):
            last_update_id = upd["update_id"]
            msg = upd.get("message") or upd.get("edited_message")
            if not msg: continue
            chat_id = str(msg.get("chat", {}).get("id"))
            if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
                continue  # игнор чужих чатов
            text = (msg.get("text") or "").strip()
            if not text: continue

            cmd, args = parse_cmd(text)

            if cmd == "/help":
                tg_send(
                    "Команды:\n"
                    "/mode strongonly|both\n"
                    "/seteps 0.12   — EPS% для WEAK (0.12 = 0.12%)\n"
                    "/setslope 0.02 — мин. наклон %/бар для STRONG\n"
                    "/useatr on|off — ATR-фильтр для STRONG\n"
                    "/setatr 0.25   — мин. ATR% (0.25 = 0.25%)\n"
                    "/settf 1m|3m|5m|15m\n"
                    "/setcooldown 180\n"
                    "/setcheck 120  — пауза между циклами\n"
                    "/setsymbols BTC-USDT,ETH-USDT,...\n"
                    "/status"
                )
            elif cmd == "/mode" and args:
                if args[0].lower() in ("strongonly","both"):
                    MODE = args[0].lower()
                    tg_send(f"✅ MODE: {MODE}")
            elif cmd == "/seteps" and args:
                try:
                    EPS_PCT = float(args[0])/100.0
                    tg_send(f"✅ EPS_PCT: {EPS_PCT*100:.3f}%")
                except: tg_send("❌ Пример: /seteps 0.10")
            elif cmd == "/setslope" and args:
                try:
                    SLOPE_MIN = float(args[0])/100.0
                    tg_send(f"✅ SLOPE_MIN: {SLOPE_MIN*100:.3f}%/бар")
                except: tg_send("❌ Пример: /setslope 0.02")
            elif cmd == "/useatr" and args:
                USE_ATR = (args[0].lower() == "on")
                tg_send(f"✅ USE_ATR: {USE_ATR}")
            elif cmd == "/setatr" and args:
                try:
                    ATR_MIN_PCT = float(args[0])/100.0
                    tg_send(f"✅ ATR_MIN_PCT: {ATR_MIN_PCT*100:.2f}%")
                except: tg_send("❌ Пример: /setatr 0.25")
            elif cmd == "/settf" and args:
                BASE_TF = args[0]
                tg_send(f"✅ TF: {BASE_TF} (fallback {FALLBACK_TF})")
            elif cmd == "/setcooldown" and args:
                try:
                    COOLDOWN_S = int(args[0]); tg_send(f"✅ COOLDOWN: {COOLDOWN_S}s")
                except: tg_send("❌ Пример: /setcooldown 180")
            elif cmd == "/setcheck" and args:
                try:
                    CHECK_INTERVAL_S = int(args[0]); tg_send(f"✅ CHECK: {CHECK_INTERVAL_S}s")
                except: tg_send("❌ Пример: /setcheck 120")
            elif cmd == "/setsymbols" and args:
                try:
                    arr = [x.strip().upper() for x in " ".join(args).replace(",", " ").split()]
                    if arr: 
                        symbols.clear()
                        symbols.update(arr)
                        tg_send(f"✅ SYMBOLS: {len(symbols)}\n" + ", ".join(sorted(symbols))[:1000])
                except: tg_send("❌ Пример: /setsymbols BTC-USDT,ETH-USDT,TRX-USDT")
            elif cmd == "/status":
                tg_send(
                    f"Символов={len(symbols)}, tf={BASE_TF}→{FALLBACK_TF}, cooldown={COOLDOWN_S}s, "
                    f"режим={MODE}, EPS={EPS_PCT*100:.2f}%, slope≥{SLOPE_MIN*100:.2f}%/бар, "
                    f"ATR{' ON' if USE_ATR else ' OFF'} ≥ {ATR_MIN_PCT*100:.2f}%"
                )
            # сохраняем актуальный список для worker-а
            SETTINGS["symbols"] = sorted(list(symbols))
        time.sleep(1)

# ========== ВОРКЕР ПРОВЕРОК ==========
SETTINGS = {"symbols": sorted(DEFAULT_SYMBOLS)}

def worker():
    global last_summary_ts
    while True:
        started = now_ts()
        syms = SETTINGS["symbols"]
        for sym in syms:
            kind, direction, reason = analyze_symbol(sym, BASE_TF, CANDLES_NEED)
            ts_prev = last_signal_ts[sym]
            allow_signal = (now_ts() - ts_prev) >= COOLDOWN_S

            if kind in ("STRONG","WEAK") and allow_signal:
                last_signal_ts[sym] = now_ts()
                msg = format_signal(sym, kind, direction, reason)
                tg_send(msg)
            else:
                # редкий "нет сигнала"
                if (now_ts() - last_nosig_ts[sym]) >= SEND_NOSIG_EVERY:
                    last_nosig_ts[sym] = now_ts()
                    tg_send(f"ℹ️ {sym}: {reason}\nUTC: {ts_utc_str()}")

        # периодический отчёт
        if (now_ts() - last_summary_ts) >= REPORT_SUMMARY_EVERY:
            last_summary_ts = now_ts()
            tg_send(f"✂️ Отчёт: символов={len(syms)}, tf={BASE_TF}→{FALLBACK_TF}, cooldown={COOLDOWN_S}s, режим={MODE}\nUTC: {ts_utc_str()}")

        # пауза до следующего круга
        dt = now_ts() - started
        sleep_left = max(1, CHECK_INTERVAL_S - dt)
        time.sleep(sleep_left)

# ========== FLASK KEEP-ALIVE ==========
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
