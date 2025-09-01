# -*- coding: utf-8 -*-
"""
main.py — EMA-сигнальный бот для Bitget UMCBL (фьючерсы)
- Свечи: GET /api/mix/v1/market/candles
- ВАЖНО:
  * granularity = секунды (60, 300, 900, 1800, 3600, 14400, 43200, 86400, 604800)
  * endTime (мс) обязателен
  * limit фиксированно 200 (макс у Bitget)
- EMA(9/21), ATR-фильтр, сила сигнала
- fallback 5m -> 15m, ретраи, антиспам "нет сигналов"
- cooldown по символу, Telegram команды/пресеты, хранение config.json
- Flask keep-alive для Render
"""

import os
import time
import json
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify

# ===================== ДАННЫЕ =====================
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = 5723086631

# ===================== НАСТРОЙКИ =====================
DEFAULT_SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","TRXUSDT",
    "LINKUSDT","NEARUSDT","ATOMUSDT","INJUSDT","SUIUSDT",
    "DOTUSDT","OPUSDT","ARBUSDT","APTUSDT","LTCUSDT","PEPEUSDT"
]
FUT_SUFFIX = "_UMCBL"
BASE_TF    = "5m"

# TF -> seconds (для /market/candles)
TF_SEC = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "12h": 43200, "1d": 86400, "1w": 604800
}
def tf_to_seconds(x):
    if isinstance(x, (int, float)):
        return int(x)
    s = str(x).strip().lower()
    if s.isdigit():
        return int(s)
    if s in TF_SEC:
        return TF_SEC[s]
    raise ValueError(f"Unsupported timeframe: {x}")

CONFIG_PATH = "config.json"
cfg_lock = threading.Lock()
cfg = {
    "MIN_CANDLES": 18,
    "STRENGTH_MAIN": 0.0015,
    "ATR_MIN": 0.0025,
    "ATR_MAX": 0.0180,
    "CHECK_INTERVAL_S": 300,
    "HARD_COOLDOWN_S": 25*60,
    "NO_SIGNAL_INTERVAL_S": 60*60,
    "SYMBOLS": DEFAULT_SYMBOLS
}

_env_symbols = os.getenv("SYMBOLS", "").strip()
if _env_symbols:
    cfg["SYMBOLS"] = [s.strip().upper() for s in _env_symbols.split(",") if s.strip()]

def load_config():
    global cfg
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            with cfg_lock:
                for k, v in data.items():
                    if k in cfg:
                        cfg[k] = v
        except Exception:
            pass

def save_config():
    with cfg_lock:
        tmp = dict(cfg)
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(tmp, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

load_config()

def get_cfg(key):
    with cfg_lock:
        return cfg[key]

def set_cfg(key, value):
    with cfg_lock:
        cfg[key] = value
    save_config()

# ===================== УТИЛИТЫ =====================
def now_ts() -> float:
    return time.time()

def pct(a, b):
    return (a - b) / b if b != 0 else 0.0

def ema(series, length):
    if len(series) < length:
        return None
    k = 2.0 / (length + 1.0)
    e = series[0]
    for v in series[1:]:
        e = v * k + e * (1 - k)
    return e

def atr_like(series_closes, period=14):
    if len(series_closes) < period + 1:
        return None
    diffs = [abs(series_closes[i] - series_closes[i-1]) for i in range(1, len(series_closes))]
    return sum(diffs[-period:]) / float(period)

def ts_iso(ts=None):
    if ts is None:
        ts = now_ts()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def parse_number(s: str):
    s = s.strip().replace(",", ".")
    is_percent = s.endswith("%")
    if is_percent:
        s = s[:-1].strip()
    val = float(s)
    return val / 100.0 if is_percent else val

def parse_duration_seconds(s: str) -> int:
    s = s.strip().lower()
    if s.endswith("s"):
        return int(float(s[:-1]))
    if s.endswith("m"):
        return int(float(s[:-1]) * 60)
    if s.endswith("h"):
        return int(float(s[:-1]) * 3600)
    return int(float(s))

# ===================== Telegram =====================
def send_tele(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text}
        )
    except Exception:
        pass

def get_updates(offset=None, timeout=10):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(url, params=params, timeout=timeout+5)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {"ok": False, "result": []}

# ===================== Bitget API =====================
BITGET_MIX_CANDLES_URL = "https://api.bitget.com/api/mix/v1/market/candles"
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0"}
BITGET_LIMIT = 200  # фиксированно

def fetch_candles(symbol: str, granularity="5m"):
    """
    Возвращает (closes:list[float], n:int, err:str|None)
    Делает 1 запрос + 2 ретрая, сортирует бары по времени (старые -> новые),
    аккуратно отбрасывает возможный дубль последней свечи.
    """
    try:
        gran_sec = tf_to_seconds(granularity)
    except Exception as conv_err:
        return None, 0, f"granularity convert error: {conv_err}"

    end_ts_ms = int(time.time() * 1000)  # ОБЯЗАТЕЛЬНО: конец окна (мс)
    params = {
        "symbol": f"{symbol}{FUT_SUFFIX}",
        "granularity": gran_sec,
        "limit": BITGET_LIMIT,     # Bitget max 200
        "endTime": end_ts_ms
    }

    last_err = None
    for _ in range(3):
        try:
            r = requests.get(BITGET_MIX_CANDLES_URL, params=params, headers=HTTP_HEADERS, timeout=10)
            r.raise_for_status()
            data = r.json()
            if "data" not in data or not isinstance(data["data"], list):
                last_err = "bad payload"
                time.sleep(0.4)
                continue
            candles = sorted(data["data"], key=lambda x: int(x[0]))  # старые -> новые
            closes = [float(c[4]) for c in candles]
            if len(candles) >= 2 and candles[-1][0] == candles[-2][0]:
                closes = closes[:-1]
            return closes, len(closes), None
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(0.5)
    return None, 0, last_err or "unknown error"

def get_closes_with_fallback(symbol: str, min_candles: int):
    """
    Пробуем 5m; если < min_candles — переходим на 15m.
    Возвращает (closes, tf_used, n_bars, fb_flag|None, err|None)
    """
    closes, n, err = fetch_candles(symbol, "5m")
    if closes and n >= min_candles:
        return closes, "5m", n, None, None
    closes15, n15, err15 = fetch_candles(symbol, "15m")
    if closes15 and n15 >= min_candles:
        return closes15, "15m", n15, "fallback", None
    best_n = max(n, n15 if closes15 else 0)
    return None, None, best_n, None, (err15 if not closes else err)

# ===================== ЛОГИКА СИГНАЛОВ =====================
last_signal_ts_by_symbol = {}
last_no_signal_ts = 0.0

def symbol_on_cooldown(symbol: str, hard_cooldown_s: int) -> bool:
    ts = last_signal_ts_by_symbol.get(symbol, 0.0)
    return (now_ts() - ts) < hard_cooldown_s

def mark_signal(symbol: str):
    last_signal_ts_by_symbol[symbol] = now_ts()

def check_signals_once():
    global last_no_signal_ts
    had_any_signal = False

    MIN_CANDLES        = get_cfg("MIN_CANDLES")
    STRENGTH_MAIN      = get_cfg("STRENGTH_MAIN")
    ATR_MIN            = get_cfg("ATR_MIN")
    ATR_MAX            = get_cfg("ATR_MAX")
    HARD_COOLDOWN_S    = get_cfg("HARD_COOLDOWN_S")
    NO_SIGNAL_INTERVAL_S = get_cfg("NO_SIGNAL_INTERVAL_S")
    SYMBOLS            = get_cfg("SYMBOLS")

    for symbol in SYMBOLS:
        if symbol_on_cooldown(symbol, HARD_COOLDOWN_S):
            continue

        closes, tf_used, n_bars, fb, err = get_closes_with_fallback(symbol, MIN_CANDLES)
        if not closes:
            send_tele(f"{symbol}: свечей={n_bars} — недостаточно данных{'' if not err else f' (err: {err})'}")
            continue

        ema9  = ema(closes[-200:], 9)
        ema21 = ema(closes[-200:], 21)
        if ema9 is None or ema21 is None:
            send_tele(f"{symbol}: свечей={len(closes)} — EMA не посчитать")
            continue

        strength = abs(pct(ema9, ema21))
        atrv     = atr_like(closes, 14)
        atr_rel  = pct(atrv, closes[-1]) if atrv else None

        if atr_rel is None or not (ATR_MIN <= abs(atr_rel) <= ATR_MAX):
            continue

        note = "" if not fb else " (fallback 15m)"
        long_cross  = (ema9 > ema21) and (strength >= STRENGTH_MAIN)
        short_cross = (ema9 < ema21) and (strength >= STRENGTH_MAIN)

        if long_cross:
            had_any_signal = True
            mark_signal(symbol)
            send_tele(
                f"✅ LONG {symbol}{note}\n"
                f"TF: {tf_used} | strength: {strength*100:.2f}% | ATR: {abs(atr_rel)*100:.2f}%\n"
                f"{ts_iso()}"
            )
            continue

        if short_cross:
            had_any_signal = True
            mark_signal(symbol)
            send_tele(
                f"✅ SHORT {symbol}{note}\n"
                f"TF: {tf_used} | strength: {strength*100:.2f}% | ATR: {abs(atr_rel)*100:.2f}%\n"
                f"{ts_iso()}"
            )
            continue

    if not had_any_signal and (now_ts() - last_no_signal_ts) >= NO_SIGNAL_INTERVAL_S:
        last_no_signal_ts = now_ts()
        send_tele("⏰ Жив. За последний час сигналов не было.")

# ===================== Основной цикл =====================
def signals_loop():
    send_tele("🤖 Бот сигналов запущен! (UMCBL)")
    while True:
        try:
            check_signals_once()
        except Exception as e:
            try:
                send_tele(f"⚠️ Ошибка цикла сигналов: {e}")
            except:
                pass
        time.sleep(get_cfg("CHECK_INTERVAL_S"))

# ===================== Команды Telegram =====================
def status_text():
    MIN_CANDLES        = get_cfg("MIN_CANDLES")
    STRENGTH_MAIN      = get_cfg("STRENGTH_MAIN")
    ATR_MIN            = get_cfg("ATR_MIN")
    ATR_MAX            = get_cfg("ATR_MAX")
    HARD_COOLDOWN_S    = get_cfg("HARD_COOLDOWN_S")
    NO_SIGNAL_INTERVAL_S = get_cfg("NO_SIGNAL_INTERVAL_S")
    SYMBOLS            = get_cfg("SYMBOLS")

    active_cooldowns = []
    now = now_ts()
    for s in SYMBOLS:
        ts = last_signal_ts_by_symbol.get(s, 0.0)
        left = max(0, int(HARD_COOLDOWN_S - (now - ts))) if ts else 0
        if left > 0:
            active_cooldowns.append(f"{s}:{left//60}m")

    parts = [
        "⚙️ Статус бота:",
        f"TF base: {BASE_TF} (fallback→15m при малом числе свечей)",
        f"Параметры: strength≥{STRENGTH_MAIN*100:.2f}% | ATR∈[{ATR_MIN*100:.2f}%; {ATR_MAX*100:.2f}%]",
        f"MIN_CANDLES: {MIN_CANDLES}",
        f"Cooldown (hard): {HARD_COOLDOWN_S//60}m",
        f"Антиспам 'нет сигналов': раз в {NO_SIGNAL_INTERVAL_S//3600}ч",
        f"Интервал проверки: {get_cfg('CHECK_INTERVAL_S')}s",
        f"Монеты: {', '.join(SYMBOLS)}",
        f"Активные cooldown: {', '.join(active_cooldowns) if active_cooldowns else 'нет'}",
        f"Время: {ts_iso()}",
    ]
    return "\n".join(parts)

HELP_TEXT = (
    "🛠 Команды:\n"
    "/status — показать текущие параметры\n"
    "/ping — проверка связи\n"
    "/symbols — текущие монеты\n"
    "/setsymbols BTCUSDT,ETHUSDT,SOLUSDT — задать монеты\n"
    "/setstrength 0.2 | 0.2% — порог силы\n"
    "/setatr 0.25 1.8 — ATR-диапазон (в % или долях)\n"
    "/setmincandles 21 — требуемое число свечей\n"
    "/setcooldown 25 — cooldown в минутах\n"
    "/setcheck 5m — интервал проверки (например: 300, 300s, 5m, 1h)\n"
    "/preset aggressive|neutral|conservative — пресеты порогов\n"
)

def apply_preset(name: str):
    name = name.lower().strip()
    if name == "aggressive":
        set_cfg("STRENGTH_MAIN", 0.0010)
        set_cfg("ATR_MIN", 0.0020)
        set_cfg("ATR_MAX", 0.0250)
        set_cfg("MIN_CANDLES", 15)
        set_cfg("HARD_COOLDOWN_S", 15*60)
        return "✅ Пресет AGGRESSIVE применён: strength 0.10%, ATR 0.20–2.50%, min_candles 15, cooldown 15m"
    elif name == "neutral":
        set_cfg("STRENGTH_MAIN", 0.0015)
        set_cfg("ATR_MIN", 0.0025)
        set_cfg("ATR_MAX", 0.0180)
        set_cfg("MIN_CANDLES", 18)
        set_cfg("HARD_COOLDOWN_S", 25*60)
        return "✅ Пресет NEUTRAL применён: strength 0.15%, ATR 0.25–1.80%, min_candles 18, cooldown 25m"
    elif name == "conservative":
        set_cfg("STRENGTH_MAIN", 0.0025)
        set_cfg("ATR_MIN", 0.0030)
        set_cfg("ATR_MAX", 0.0120)
        set_cfg("MIN_CANDLES", 21)
        set_cfg("HARD_COOLDOWN_S", 35*60)
        return "✅ Пресет CONSERVATIVE применён: strength 0.25%, ATR 0.30–1.20%, min_candles 21, cooldown 35m"
    else:
        return "❌ Неизвестный пресет. Доступны: aggressive, neutral, conservative."

def telegram_commands_loop():
    offset = None
    send_tele("📮 Команды активны: /status, /ping, /symbols, /setsymbols, /setstrength, /setatr, /setmincandles, /setcooldown, /setcheck, /preset\n\n" + HELP_TEXT)
    while True:
        try:
            data = get_updates(offset=offset, timeout=10)
            if not data.get("ok", False):
                time.sleep(2)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                chat_id = ((msg.get("chat") or {}).get("id"))
                if not text or chat_id != TELEGRAM_CHAT_ID:
                    continue

                t = text.lower()
                if t.startswith("/ping"):
                    send_tele(f"🏓 Pong! {ts_iso()}")
                elif t.startswith("/help"):
                    send_tele(HELP_TEXT)
                elif t.startswith("/status"):
                    send_tele(status_text())
                elif t.startswith("/symbols"):
                    send_tele("Монеты: " + ", ".join(get_cfg("SYMBOLS")))
                elif t.startswith("/setsymbols"):
                    try:
                        raw = text.split(" ", 1)[1]
                        arr = [s.strip().upper() for s in raw.split(",") if s.strip()]
                        if not arr:
                            raise ValueError("пустой список")
                        set_cfg("SYMBOLS", arr)
                        send_tele("✅ Обновлён список монет: " + ", ".join(arr))
                    except Exception as e:
                        send_tele(f"❌ Пример: /setsymbols BTCUSDT,ETHUSDT,SOLUSDT\nОшибка: {e}")
                elif t.startswith("/setstrength"):
                    try:
                        val = parse_number(text.split(" ", 1)[1])
                        if not (0.0001 <= val <= 0.05):
                            raise ValueError("0.01%..5%")
                        set_cfg("STRENGTH_MAIN", val)
                        send_tele(f"✅ strength порог установлен: {val*100:.3f}%")
                    except Exception as e:
                        send_tele(f"❌ Пример: /setstrength 0.2  (или 0.2%)\nОшибка: {e}")
                elif t.startswith("/setatr"):
                    try:
                        parts = text.split()
                        if len(parts) < 3:
                            raise ValueError("нужно два числа")
                        vmin = parse_number(parts[1]); vmax = parse_number(parts[2])
                        if not (0.0005 <= vmin < vmax <= 0.10):
                            raise ValueError("0.05%..10%, min<max")
                        set_cfg("ATR_MIN", vmin); set_cfg("ATR_MAX", vmax)
                        send_tele(f"✅ ATR-диапазон: {vmin*100:.2f}% — {vmax*100:.2f}%")
                    except Exception as e:
                        send_tele(f"❌ Пример: /setatr 0.25 1.8  (или с %)\nОшибка: {e}")
                elif t.startswith("/setmincandles"):
                    try:
                        n = int(text.split()[1])
                        if not (5 <= n <= 500):
                            raise ValueError("5..500")
                        set_cfg("MIN_CANDLES", n)
                        send_tele(f"✅ MIN_CANDLES = {n}")
                    except Exception as e:
                        send_tele(f"❌ Пример: /setmincandles 21\nОшибка: {e}")
                elif t.startswith("/setcooldown"):
                    try:
                        minutes = int(text.split()[1])
                        if not (1 <= minutes <= 240):
                            raise ValueError("1..240")
                        set_cfg("HARD_COOLDOWN_S", minutes * 60)
                        send_tele(f"✅ Cooldown = {minutes}m")
                    except Exception as e:
                        send_tele(f"❌ Пример: /setcooldown 25\nОшибка: {e}")
                elif t.startswith("/setcheck"):
                    try:
                        secs = parse_duration_seconds(text.split()[1])
                        if not (10 <= secs <= 3600):
                            raise ValueError("10..3600 сек")
                        set_cfg("CHECK_INTERVAL_S", secs)
                        send_tele(f"✅ Интервал проверки = {secs}s")
                    except Exception as e:
                        send_tele(f"❌ Пример: /setcheck 5m   (или 300, 300s, 1h)\nОшибка: {e}")
                elif t.startswith("/preset"):
                    try:
                        name = text.split()[1]
                        msg = apply_preset(name)
                        send_tele(msg + "\n" + status_text())
                    except Exception:
                        send_tele("❌ Пример: /preset aggressive\nДоступны: aggressive, neutral, conservative")
                else:
                    if text.startswith("/"):
                        send_tele("Команда не распознана. /help")
        except Exception:
            time.sleep(2)

# ===================== Flask keep-alive =====================
app = Flask(__name__)

@app.route("/")
def root():
    return jsonify({
        "ok": True,
        "service": "bitget-umcbl-ema-signals",
        "time": ts_iso(),
        "symbols": get_cfg("SYMBOLS"),
        "base_tf": BASE_TF,
        "min_candles": get_cfg("MIN_CANDLES"),
        "strength_main": get_cfg("STRENGTH_MAIN"),
        "atr_range": [get_cfg("ATR_MIN"), get_cfg("ATR_MAX")],
        "cooldown_s": get_cfg("HARD_COOLDOWN_S"),
        "check_interval_s": get_cfg("CHECK_INTERVAL_S")
    })

# ===================== Запуск =====================
def run_threads():
    t1 = threading.Thread(target=signals_loop, daemon=True)
    t2 = threading.Thread(target=telegram_commands_loop, daemon=True)
    t1.start()
    t2.start()

if __name__ == "__main__":
    run_threads()
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
