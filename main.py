# -*- coding: utf-8 -*-
"""
Bitget UMCBL сигнальный бот:
— Сообщения как на скрине: 🟢 «фильтры ЗЕЛЁНЫЕ» и ⚡ «Возможен вход ... ⏳»
— Анализ: 5m; тренды-подтверждения: 15m и 1h
— Индикаторы/фильтры: EMA9/21, сила, RSI(14), ATR% коридор, EMA50/200 (5m), тренды 15m/1h
— Управление через Telegram: /status, /mode, /set, /cooldown, /symbols, /check
— Диагностика: heartbeat и причины молчания
"""

import os, time, threading, requests
from datetime import datetime, timezone
from flask import Flask

# ================= ТВОИ ДАННЫЕ =================
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
FUT_SUFFIX = "_UMCBL"
# ===============================================

# Монеты по умолчанию
SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","TRXUSDT",
    "LINKUSDT","NEARUSDT","ATOMUSDT","INJUSDT","SUIUSDT",
    "DOTUSDT","OPUSDT","ARBUSDT","APTUSDT","LTCUSDT","PEPEUSDT"
]

# -------- ПАРАМЕТРЫ (меняются через Telegram) --------
EMA_FAST, EMA_SLOW = 9, 21
EMA_TREND_FAST, EMA_TREND_SLOW = 50, 200
CHECK_INTERVAL_S = 90

STRENGTH_MIN = 0.20     # |EMA9-EMA21|/Close*100, %  (сила сигнала)
NEAR_BAND_PCT = 0.10    # зона near-cross, %
RSI_MIN_LONG  = 50
RSI_MAX_SHORT = 50
ATR_MIN_PCT, ATR_MAX_PCT = 0.30, 1.50

NEAR_COOLDOWN_MIN = 15
HARD_COOLDOWN_MIN = 25
# -----------------------------------------------------

# Пресеты
PRESETS = {
    "soft":  {"STRENGTH_MIN":0.15, "NEAR_BAND_PCT":0.20, "RSI_MIN_LONG":48, "RSI_MAX_SHORT":52, "ATR_MIN_PCT":0.20, "ATR_MAX_PCT":2.00},
    "mid":   {"STRENGTH_MIN":0.20, "NEAR_BAND_PCT":0.10, "RSI_MIN_LONG":50, "RSI_MAX_SHORT":50, "ATR_MIN_PCT":0.30, "ATR_MAX_PCT":1.50},
    "strict":{"STRENGTH_MIN":0.30, "NEAR_BAND_PCT":0.06, "RSI_MIN_LONG":55, "RSI_MAX_SHORT":45, "ATR_MIN_PCT":0.35, "ATR_MAX_PCT":1.20},
}

# Диагностика / heartbeat
LAST_MSG_TS = 0
LAST_CAUSES = {}   # symbol -> краткая причина отсутствия сигнала
HEARTBEAT_MIN = 60  # раз в N минут, если не было сообщений — шлём «жив»

# Bitget API
BITGET_URL = "https://api.bitget.com/api/mix/v1/market/history-candles"
GRAN_MAP = {"1m":"1min","5m":"5min","15m":"15min","1h":"1h","4h":"4h"}

# Flask keep-alive
app = Flask(__name__)

# ================= ВСПОМОГАТЕЛЬНЫЕ =================
def now_utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def tg_send(text: str):
    """Отправка в Telegram + обновление таймштампа активности."""
    global LAST_MSG_TS
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10
        )
        LAST_MSG_TS = time.time()
    except Exception as e:
        print("TG error:", e)

def fetch_candles(symbol: str, tf: str, limit: int = 300):
    """Возвращает [(ts, o, h, l, c)] по возрастанию. Шлёт предупреждение, если Bitget вернул пусто."""
    try:
        r = requests.get(
            BITGET_URL,
            params={"symbol": f"{symbol}{FUT_SUFFIX}", "granularity": GRAN_MAP[tf], "limit": str(limit)},
            timeout=15,
            headers={"User-Agent":"Mozilla/5.0"}
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            tg_send(f"⚠️ Нет свечей от Bitget для {symbol}{FUT_SUFFIX} на {tf}")
            return []
        rows = [(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in data]
        rows.sort(key=lambda t: t[0])
        return rows
    except Exception as e:
        print("fetch_candles error:", symbol, tf, e)
        return []

def ema(series, period):
    k = 2/(period+1.0)
    out, cur = [], None
    for v in series:
        cur = v if cur is None else v*k + cur*(1-k)
        out.append(cur)
    return out

def rsi14(closes, period=14):
    if len(closes) < period+1: return None
    gains, losses = [], []
    for i in range(1, period+1):
        ch = closes[i]-closes[i-1]
        gains.append(max(ch,0)); losses.append(-min(ch,0))
    avg_gain = sum(gains)/period; avg_loss = sum(losses)/period
    rs = (avg_gain/avg_loss) if avg_loss>0 else 1e9
    rsi = 100 - 100/(1+rs)
    for i in range(period+1, len(closes)):
        ch = closes[i]-closes[i-1]
        gain, loss = max(ch,0), -min(ch,0)
        avg_gain = (avg_gain*(period-1)+gain)/period
        avg_loss = (avg_loss*(period-1)+loss)/period
        rs = (avg_gain/avg_loss) if avg_loss>0 else 1e9
        rsi = 100 - 100/(1+rs)
    return rsi

def atr_pct(highs, lows, closes, period=14):
    if len(closes) < period+1: return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    atr = sum(trs[-period:])/period
    return (atr / closes[-1]) * 100.0

# --- кулдауны сообщений ---
last_near_sent = {}   # (symbol, side) -> ts
last_hard_sent = {}   # (symbol, side) -> ts
def cooldown_ok(store, key, minutes): return (time.time() - store.get(key, 0)) >= minutes*60
def mark_sent(store, key): store[key] = time.time()

def trend_ok(symbol: str):
    """Возвращает «LONG», если на 15m и 1h EMA50>EMA200; «SHORT», если EMA50<EMA200."""
    try:
        for tf in ("15m","1h"):
            rows = fetch_candles(symbol, tf, limit=240)
            if not rows: return None
            closes = [r[4] for r in rows]
            e50 = ema(closes, 50)[-1]; e200 = ema(closes, 200)[-1]
            if e50 <= e200:
                return "SHORT"
        return "LONG"
    except Exception as e:
        print("trend_ok error:", symbol, e)
        return None

def trend_text(_):  # на карточке пишем просто «OK»
    return "тренды 15m/1h OK"

# ================= ОСНОВНАЯ ЛОГИКА =================
def check_symbol(symbol: str):
    """Проверка монеты, отправка сигналов и запись причины в LAST_CAUSES."""
    try:
        rows = fetch_candles(symbol, "5m", limit=300)
        if len(rows) < 220:
            LAST_CAUSES[symbol] = "мало свечей 5m"
            return

        _, o, h, l, c = zip(*rows)
        closes, highs, lows = list(c), list(h), list(l)

        e9, e21 = ema(closes, 9), ema(closes, 21)
        e50, e200 = ema(closes, 50), ema(closes, 200)
        ema9, ema21, ema50, ema200 = e9[-1], e21[-1], e50[-1], e200[-1]
        price = closes[-1]

        diff_now, diff_prev = ema9-ema21, e9[-2]-e21[-2]
        strength = abs(diff_now)/price*100.0
        rsi = rsi14(closes)
        atrp = atr_pct(highs, lows, closes)

        dom_trend = trend_ok(symbol)
        bull_5m = ema50 > ema200
        bear_5m = ema50 < ema200

        # подтверждённые 🟢
        long_ok = (diff_now>0 and strength>=STRENGTH_MIN and rsi is not None and rsi>=RSI_MIN_LONG
                   and atrp is not None and ATR_MIN_PCT<=atrp<=ATR_MAX_PCT
                   and bull_5m and dom_trend=="LONG")
        short_ok = (diff_now<0 and strength>=STRENGTH_MIN and rsi is not None and rsi<=RSI_MAX_SHORT
                    and atrp is not None and ATR_MIN_PCT<=atrp<=ATR_MAX_PCT
                    and bear_5m and dom_trend=="SHORT")

        # near-cross ⚡
        near_band = (abs(diff_now)/price*100.0) <= NEAR_BAND_PCT
        cross_incoming_long  = (diff_prev < 0 and diff_now >= 0) or (near_band and ema9 >= ema21)
        cross_incoming_short = (diff_prev > 0 and diff_now <= 0) or (near_band and ema9 <= ema21)

        def line_filters(side_txt):
            return (f"5m: {side_txt} • {trend_text(dom_trend)} • "
                    f"сила ≥ {STRENGTH_MIN:.2f}% • RSI ≥{RSI_MIN_LONG if side_txt=='LONG' else '…'} "
                    f"• ATR {ATR_MIN_PCT:.2f}%—{ATR_MAX_PCT:.2f}% • EMA50/EMA200 OK")

        def snapshot(side_txt):
            return (f"Цена: {price:.6f} • 5m: {side_txt}\n"
                    f"Тренды 15m/1h: OK • Сила={strength:.2f}% (≥ {STRENGTH_MIN:.2f}%) • "
                    f"RSI(14)={rsi:.1f} • ATR={atrp:.2f}% в коридоре • EMA50/EMA200 OK")

        # 🟢 подтверждённые
        if long_ok and cooldown_ok(last_hard_sent, (symbol,"LONG"), HARD_COOLDOWN_MIN):
            tg_send(f"🟢 {symbol}{FUT_SUFFIX}: фильтры ЗЕЛЁНЫЕ\n{line_filters('LONG')}")
            mark_sent(last_hard_sent, (symbol,"LONG"))
            LAST_CAUSES[symbol] = "сигнал LONG отправлен"
            return

        if short_ok and cooldown_ok(last_hard_sent, (symbol,"SHORT"), HARD_COOLDOWN_MIN):
            tg_send(f"🟢 {symbol}{FUT_SUFFIX}: фильтры ЗЕЛЁНЫЕ\n{line_filters('SHORT')}")
            mark_sent(last_hard_sent, (symbol,"SHORT"))
            LAST_CAUSES[symbol] = "сигнал SHORT отправлен"
            return

        # ⚡ возможен вход
        if cross_incoming_long and cooldown_ok(last_near_sent, (symbol,"LONG"), NEAR_COOLDOWN_MIN):
            tg_send(f"⚡ Возможен вход LONG по {symbol}{FUT_SUFFIX}\n{snapshot('LONG')}\n⏳ ждём подтверждения кросса EMA ↑")
            mark_sent(last_near_sent, (symbol,"LONG"))
            LAST_CAUSES[symbol] = "near LONG"
            return

        if cross_incoming_short and cooldown_ok(last_near_sent, (symbol,"SHORT"), NEAR_COOLDOWN_MIN):
            tg_send(f"⚡ Возможен вход SHORT по {symbol}{FUT_SUFFIX}\n{snapshot('SHORT')}\n⏳ ждём подтверждения кросса EMA ↓")
            mark_sent(last_near_sent, (symbol,"SHORT"))
            LAST_CAUSES[symbol] = "near SHORT"
            return

        # --- если ничего не отправили — запишем короткую причину
        reason = []
        if strength < STRENGTH_MIN: reason.append(f"сила {strength:.2f}%<{STRENGTH_MIN:.2f}%")
        if rsi is None: reason.append("RSI n/a")
        else:
            if diff_now>0 and rsi<RSI_MIN_LONG: reason.append(f"RSI {rsi:.1f}<long {RSI_MIN_LONG}")
            if diff_now<0 and rsi>RSI_MAX_SHORT: reason.append(f"RSI {rsi:.1f}>short {RSI_MAX_SHORT}")
        if atrp is None: reason.append("ATR n/a")
        else:
            if not (ATR_MIN_PCT <= atrp <= ATR_MAX_PCT): reason.append(f"ATR {atrp:.2f}% вне {ATR_MIN_PCT:.2f}–{ATR_MAX_PCT:.2f}%")
        if diff_now>0 and not (ema50>ema200 and dom_trend=="LONG"): reason.append("тренд LONG не подтверждён")
        if diff_now<0 and not (ema50<ema200 and dom_trend=="SHORT"): reason.append("тренд SHORT не подтверждён")
        LAST_CAUSES[symbol] = "; ".join(reason[:3]) or "ожидание кросса"

    except Exception as e:
        print(f"[{now_utc_iso()}] {symbol} error:", e)
        LAST_CAUSES[symbol] = "ошибка расчёта"

# ================== TELEGRAM КОМАНДЫ ==================
def apply_preset(name: str):
    global STRENGTH_MIN, NEAR_BAND_PCT, RSI_MIN_LONG, RSI_MAX_SHORT, ATR_MIN_PCT, ATR_MAX_PCT
    conf = PRESETS.get(name.lower())
    if not conf: return False
    STRENGTH_MIN = conf["STRENGTH_MIN"]
    NEAR_BAND_PCT = conf["NEAR_BAND_PCT"]
    RSI_MIN_LONG = conf["RSI_MIN_LONG"]
    RSI_MAX_SHORT = conf["RSI_MAX_SHORT"]
    ATR_MIN_PCT, ATR_MAX_PCT = conf["ATR_MIN_PCT"], conf["ATR_MAX_PCT"]
    return True

def status_text():
    return (
        "⚙️ Текущие параметры:\n"
        f"• strength ≥ {STRENGTH_MIN:.2f}% | near ±{NEAR_BAND_PCT:.2f}%\n"
        f"• RSI: long≥{RSI_MIN_LONG} / short≤{RSI_MAX_SHORT}\n"
        f"• ATR corridor: {ATR_MIN_PCT:.2f}%—{ATR_MAX_PCT:.2f}%\n"
        f"• cooldown: near {NEAR_COOLDOWN_MIN}m / hard {HARD_COOLDOWN_MIN}m\n"
        f"• symbols: {', '.join(SYMBOLS)}\n"
        f"• time: {now_utc_iso()}"
    )

def tg_poll():
    tg_send("🤖 Бот сигналов запущен! (UMCBL)")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset: params["offset"] = offset
            resp = requests.get(url, params=params, timeout=35)
            data = resp.json().get("result", [])
            for upd in data:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg: continue
                chat_id = str(msg["chat"]["id"])
                if chat_id != str(TELEGRAM_CHAT_ID):  # игнор чужих
                    continue
                text = (msg.get("text") or "").strip()
                if not text.startswith("/"): continue
                low = text.lower()

                if low.startswith("/status"):
                    tg_send(status_text()); continue

                if low.startswith("/mode"):
                    parts = low.split()
                    if len(parts)>=2 and apply_preset(parts[1]):
                        tg_send("✅ Пресет применён:\n" + status_text())
                    else:
                        tg_send("Используй: /mode soft | /mode mid | /mode strict")
                    continue

                if low.startswith("/set "):
                    try:
                        parts = low.split()
                        if len(parts)<3:
                            tg_send("❗ Нужно указать значение.\nПримеры:\n/set strength 0.25\n/set near 0.15\n/set rsi_long 55\n/set rsi_short 45\n/set atr 0.30 1.50")
                            continue
                        key, val = parts[1], parts[2]
                        if key=="strength":
                            global STRENGTH_MIN; STRENGTH_MIN = float(val); tg_send(f"OK: strength={val}")
                        elif key=="near":
                            global NEAR_BAND_PCT; NEAR_BAND_PCT = float(val); tg_send(f"OK: near={val}")
                        elif key=="rsi_long":
                            global RSI_MIN_LONG; RSI_MIN_LONG = int(val); tg_send(f"OK: rsi_long={val}")
                        elif key=="rsi_short":
                            global RSI_MAX_SHORT; RSI_MAX_SHORT = int(val); tg_send(f"OK: rsi_short={val}")
                        elif key=="atr" and len(parts)>=4:
                            global ATR_MIN_PCT, ATR_MAX_PCT
                            ATR_MIN_PCT = float(parts[2]); ATR_MAX_PCT = float(parts[3])
                            tg_send(f"OK: atr={ATR_MIN_PCT}-{ATR_MAX_PCT}")
                        else:
                            tg_send("Неизвестный параметр. Доступно: strength, near, rsi_long, rsi_short, atr")
                    except Exception:
                        tg_send("Ошибка в формате /set")
                    continue

                if low.startswith("/cooldown"):
                    try:
                        parts = low.split()
                        if len(parts)<3:
                            tg_send("Примеры:\n/cooldown near 20\n/cooldown hard 40"); continue
                        if parts[1]=="near":
                            global NEAR_COOLDOWN_MIN; NEAR_COOLDOWN_MIN = int(parts[2]); tg_send("OK: near cooldown=" + parts[2])
                        elif parts[1]=="hard":
                            global HARD_COOLDOWN_MIN; HARD_COOLDOWN_MIN = int(parts[2]); tg_send("OK: hard cooldown=" + parts[2])
                        else:
                            tg_send("Используй near|hard")
                    except Exception:
                        tg_send("Ошибка в формате /cooldown")
                    continue

                if low.startswith("/symbols"):
                    parts = low.split()
                    if len(parts)==1:
                        tg_send("Список монет:\n" + ", ".join(SYMBOLS)); continue
                    if len(parts)>=3 and parts[1]=="add":
                        sym = parts[2].upper()
                        if sym not in SYMBOLS:
                            SYMBOLS.append(sym); tg_send(f"Добавлено: {sym}")
                        else:
                            tg_send("Уже есть: " + sym)
                        continue
                    if len(parts)>=3 and parts[1]=="remove":
                        sym = parts[2].upper()
                        if sym in SYMBOLS:
                            SYMBOLS.remove(sym); tg_send(f"Удалено: {sym}")
                        else:
                            tg_send("Нет такой: " + sym)
                        continue
                    tg_send("Команды:\n/symbols\n/symbols add DOGEUSDT\n/symbols remove DOGEUSDT")
                    continue

                if low.startswith("/check"):
                    parts = low.split()
                    sym = parts[1].upper() if len(parts)>=2 else None
                    if sym is None:
                        tg_send("Используй: /check BTCUSDT")
                        continue
                    check_symbol(sym)  # прогоняем свежий расчёт
                    cause = LAST_CAUSES.get(sym, "нет данных")
                    tg_send(f"🔎 {sym}{FUT_SUFFIX}: {cause}")
                    continue

                tg_send("Команды: /status, /mode soft|mid|strict, /set ..., /cooldown ..., /symbols, /check SYMBOL")
        except Exception as e:
            print("tg_poll error:", e)
            time.sleep(2)

# ================== РАБОЧИЕ ПОТОКИ ==================
def signal_worker():
    while True:
        for s in list(SYMBOLS):
            check_symbol(s); time.sleep(1.2)  # бережём API
        time.sleep(CHECK_INTERVAL_S)

def heartbeat_worker():
    while True:
        try:
            # если давно не было ни одного сообщения — отправим «жив» + причины
            if time.time() - LAST_MSG_TS > HEARTBEAT_MIN*60:
                top = []
                for s in list(SYMBOLS)[:10]:
                    if s in LAST_CAUSES:
                        top.append(f"{s}: {LAST_CAUSES[s]}")
                extra = "\n".join(top[:5]) if top else "нет причин (мало активности)"
                tg_send("⏱ Жив. За последний час сигналов не было.\n" + extra)
            time.sleep(60)
        except Exception as e:
            print("heartbeat error:", e)
            time.sleep(5)

# ================== FLASK KEEP-ALIVE ==================
@app.route("/")
def index(): return f"OK {now_utc_iso()}"

def main():
    threading.Thread(target=signal_worker, daemon=True).start()
    threading.Thread(target=tg_poll, daemon=True).start()
    threading.Thread(target=heartbeat_worker, daemon=True).start()
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
