import os
import time
import threading
from datetime import datetime, timezone
from collections import defaultdict

import requests
from flask import Flask, jsonify, request

# ========= ТВОИ ДАННЫЕ =========
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"
# ===============================

# -------- Настройки --------
FUT_SUFFIX = "_UMCBL"                         # USDT-M perpetual на Bitget
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT"]

WORK_TF = "5min"                              # рабочий ТФ для входов
HTF_TF  = "15min"                             # фильтр тренда
EMA_FAST, EMA_SLOW = 9, 21
CANDLES_LIMIT = 600                           # ГЛУБИНА ИСТОРИИ (было 300)

STRENGTH_PCT = 0.0015                         # мин. «сила» кросса 0.15%
RSI_PERIOD = 14
ALERT_COOLDOWN_SEC = 15 * 60                  # не чаще 1/15 мин/символ
HEARTBEAT_SEC = 60 * 60                       # статус раз в час
REQUEST_TIMEOUT = 12
SLEEP_BETWEEN_SYMBOLS = 0.25
LOOP_SLEEP = 1.5

# Сколько держать пару отключенной перед повторной попыткой (сек)
RECHECK_FAIL_SEC = 15 * 60

BASE_URL = "https://api.bitget.com"
_REQ_HEADERS = {"User-Agent": "futures-signal-bot/2.0", "Accept": "application/json"}

# -------- Служебные --------
last_alert_time = defaultdict(lambda: 0.0)         # антиспам по символу
last_heartbeat_time = defaultdict(lambda: 0.0)
last_band_state = {}                                # LONG/SHORT/NEUTRAL (5m)

# Запоминаем удачные параметры и отключённые пары (по ключу (symbol, tf))
accepted_params = {}     # (sym_base, tf) -> dict(endpoint, symbol, gran, productType?)
disabled_symbols = {}    # (sym_base, tf) -> dict(reason, until_ts)

# Для контроля «сколько свечей пришло»
last_candles_count = defaultdict(lambda: {"5m": 0, "15m": 0})

app = Flask(__name__)

# ========= Утилиты =========
_GRAN_TO_SEC = {
    "1":60, "60":60, "1min":60,
    "3":180, "180":180, "3min":180,
    "5":300, "300":300, "5min":300,
    "15":900, "900":900, "15min":900,
    "30":1800, "1800":1800, "30min":1800,
    "60min":3600, "1h":3600, "3600":3600,
    "240":14400, "4h":14400, "14400":14400,
    "21600":21600, "6h":21600,
    "43200":43200, "12h":43200,
    "86400":86400, "1day":86400,
    "604800":604800, "1week":604800,
    "2592000":2592000, "1M":2592000,
}

V2_GRAN_CANDS = {
    "1min": ["1min", "60"], "3min": ["3min", "180"], "5min": ["5min", "300"],
    "15min": ["15min", "900"], "30min": ["30min", "1800"], "1h": ["1h", "3600"],
    "4h": ["4h", "14400"], "6h": ["6h", "21600"], "12h": ["12h", "43200"],
    "1day": ["1day", "86400"], "1week": ["1week", "604800"], "1M": ["1M", "2592000"],
}
V1_GRAN_CANDS = {
    "1min": ["1min", "60", "1"], "3min": ["3min", "180", "3"], "5min": ["5min", "300", "5"],
    "15min": ["15min", "900", "15"], "30min": ["30min", "1800", "30"],
    "1h": ["1h", "3600", "60"], "4h": ["4h", "14400", "240"], "6h": ["6h", "21600", "360"],
    "12h": ["12h", "43200", "720"], "1day": ["1day", "86400", "1D"], "1week": ["1week", "604800", "1W"], "1M": ["1M", "2592000", "1M"],
}

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def send_telegram(text: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[TG] send error: {e}")
        return False

def _safe_json(resp) -> dict:
    try:
        return resp.json()
    except Exception as e:
        print(f"[http] .json() parse error: {e}")
        return {}

# ========= Индикаторы =========
def ema_series(values, period):
    out = []
    k = 2.0 / (period + 1.0)
    ema = None
    for v in values:
        ema = v if ema is None else (v * k + ema * (1 - k))
        out.append(ema)
    return out

def rsi_series(close, period=14):
    if len(close) < period + 2:
        return [50.0] * len(close)
    gains = [max(0.0, close[i]-close[i-1]) for i in range(1, len(close))]
    losses = [max(0.0, close[i-1]-close[i]) for i in range(1, len(close))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsis = [50.0]*(period+1)
    rs = (avg_gain/avg_loss) if avg_loss != 0 else 9999
    rsis.append(100 - 100/(1+rs))
    for i in range(period+2, len(close)+1):
        g = gains[i-2]; l = losses[i-2]
        avg_gain = (avg_gain*(period-1)+g)/period
        avg_loss = (avg_loss*(period-1)+l)/period
        rs = (avg_gain/avg_loss) if avg_loss != 0 else 9999
        rsis.append(100 - 100/(1+rs))
    return rsis[:len(close)]

def atr_series(high, low, close, period=14):
    trs = []
    for i in range(len(close)):
        if i == 0:
            trs.append(high[i]-low[i])
        else:
            tr = max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
            trs.append(tr)
    out = []
    if len(trs) < period:
        return [None]*len(close)
    s = sum(trs[:period]) / period
    out = [None]*(period-1) + [s]
    for i in range(period, len(trs)):
        s = (out[-1]*(period-1) + trs[i]) / period
        out.append(s)
    return out

# ========= Bitget candles =========
def _parse_ohlcv_payload(data):
    rows = data.get("data", []) or []
    out = []
    for row in rows:
        try:
            ts = int(float(row[0]))
            o = float(row[1]); h = float(row[2]); l = float(row[3]); c = float(row[4])
            v = float(row[5]) if len(row) > 5 else 0.0
            out.append([ts, o, h, l, c, v])
        except Exception:
            continue
    out.sort(key=lambda x: x[0])
    return out

def _try_v2(symbol_str: str, gran: str, product_type, limit: int):
    url = f"{BASE_URL}/api/v2/mix/market/candles"
    params = {"symbol": symbol_str, "granularity": gran, "limit": limit}
    if product_type:
        params["productType"] = product_type
    try:
        r = requests.get(url, params=params, headers=_REQ_HEADERS, timeout=REQUEST_TIMEOUT)
        data = _safe_json(r)
        if str(data.get("code")) == "00000":
            return _parse_ohlcv_payload(data)
    except Exception as e:
        print(f"[v2] exception {symbol_str} {gran} {product_type}: {e}")
    return None

def _try_v1(symbol_str: str, gran: str, limit: int):
    url = f"{BASE_URL}/api/mix/v1/market/candles"
    params = {"symbol": symbol_str, "granularity": gran, "limit": limit}
    try:
        r = requests.get(url, params=params, headers=_REQ_HEADERS, timeout=REQUEST_TIMEOUT)
        data = _safe_json(r)
        if str(data.get("code")) == "00000":
            return _parse_ohlcv_payload(data)
    except Exception as e:
        print(f"[v1] exception {symbol_str} {gran}: {e}")
    return None

def fetch_candles_exact(sym_base: str, tf: str, limit: int):
    key = (sym_base, tf)

    # Если пара отключена — проверим, пора ли пробовать снова
    if key in disabled_symbols:
        if time.time() < disabled_symbols[key]["until_ts"]:
            raise RuntimeError(f"{sym_base}{FUT_SUFFIX}/{tf} disabled: {disabled_symbols[key]['reason']}")
        else:
            disabled_symbols.pop(key, None)
            send_telegram(f"✅ Повторная попытка включения {sym_base}{FUT_SUFFIX} [{tf}]")

    # Если уже есть рабочие параметры — используем их
    if key in accepted_params:
        cfg = accepted_params[key]
        data = _try_v2(cfg["symbol"], cfg["gran"], cfg.get("productType"), limit) if cfg["endpoint"]=="v2" \
               else _try_v1(cfg["symbol"], cfg["gran"], limit)
        if data:
            return data
        accepted_params.pop(key, None)  # сломалось — забудем

    # Полный перебор: v2, затем v1
    v2_grans = V2_GRAN_CANDS.get(tf, ["5min","300"])
    v1_grans = V1_GRAN_CANDS.get(tf, ["5min","300","5"])
    for sym in (sym_base + FUT_SUFFIX, sym_base):
        for prod in (None, "umcbl", "UMCBL"):
            for gran in v2_grans:
                data = _try_v2(sym, gran, prod, limit)
                if data:
                    accepted_params[key] = {"endpoint":"v2","symbol":sym,"gran":gran,"productType":prod}
                    print(f"[{sym_base} {tf}] ACCEPT v2: symbol={sym}, gran={gran}, productType={prod}")
                    return data
    for sym in (sym_base + FUT_SUFFIX, sym_base):
        for gran in v1_grans:
            data = _try_v1(sym, gran, limit)
            if data:
                accepted_params[key] = {"endpoint":"v1","symbol":sym,"gran":gran}
                print(f"[{sym_base} {tf}] ACCEPT v1: symbol={sym}, gran={gran}")
                return data

    # Если совсем не получилось — отключаем пару на время
    reason = f"свечи не отдаются для TF={tf}"
    until_ts = time.time() + RECHECK_FAIL_SEC
    disabled_symbols[key] = {"reason": reason, "until_ts": until_ts}
    send_telegram(f"⛔ Отключаю {sym_base}{FUT_SUFFIX} [{tf}] на {RECHECK_FAIL_SEC//60} мин: {reason}")
    raise RuntimeError(f"[{sym_base} {tf}] disabled: {reason}")

def get_closed_ohlcv(sym_base: str, tf: str, limit: int):
    data = fetch_candles_exact(sym_base, tf, limit)
    if not data:
        return [], [], []
    gran_sec = _GRAN_TO_SEC.get(tf, 300)
    now_ms = int(time.time() * 1000)
    closed = [r for r in data if (now_ms - int(r[0])) >= gran_sec * 1000]   # только ЗАКРЫТЫЕ
    if not closed:
        return [], [], []
    highs = [r[2] for r in closed]
    lows  = [r[3] for r in closed]
    closes= [r[4] for r in closed]
    return highs, lows, closes

# ========= Логика сигналов =========
def analyze_and_alert(sym_base: str):
    # 5m данные
    h5, l5, c5 = get_closed_ohlcv(sym_base, WORK_TF, CANDLES_LIMIT)
    # 15m тренд
    h15, l15, c15 = get_closed_ohlcv(sym_base, HTF_TF, CANDLES_LIMIT//2)

    # обновим счётчики для /status
    last_candles_count[sym_base] = {"5m": len(c5), "15m": len(c15)}

    if len(c5) < max(EMA_SLOW+5, 60) or len(c15) < max(EMA_SLOW+5, 40):
        # недост. данных — просто пропустим без шума
        return

    # индикаторы
    ema9_5  = ema_series(c5, EMA_FAST)
    ema21_5 = ema_series(c5, EMA_SLOW)
    ema9_15  = ema_series(c15, EMA_FAST)
    ema21_15 = ema_series(c15, EMA_SLOW)
    rsi5 = rsi_series(c5, RSI_PERIOD)
    atr5 = atr_series(h5, l5, c5, 14)

    i = len(c5) - 1
    j = len(c15) - 1
    if i < 2 or j < 1:
        return

    # Подтверждённый сигнал: кросс был НА предыдущей свече и сохраняется сейчас
    cross_up_prev   = ema9_5[i-2] <= ema21_5[i-2] and ema9_5[i-1] > ema21_5[i-1]
    cross_down_prev = ema9_5[i-2] >= ema21_5[i-2] and ema9_5[i-1] < ema21_5[i-1]
    hold_up   = ema9_5[i] > ema21_5[i]
    hold_down = ema9_5[i] < ema21_5[i]

    strength_now = abs(ema9_5[i] - ema21_5[i]) / c5[i] >= STRENGTH_PCT
    trend_up   = ema9_15[j] > ema21_15[j]
    trend_down = ema9_15[j] < ema21_15[j]
    rsi_ok_long  = rsi5[i] <= 55 and rsi5[i] > rsi5[i-1]
    rsi_ok_short = rsi5[i] >= 45 and rsi5[i] < rsi5[i-1]

    side_5m = "LONG" if hold_up else ("SHORT" if hold_down else "NEUTRAL")
    last_band_state[sym_base] = side_5m

    now = time.time()
    entry = c5[i]
    this_atr = atr5[i] if atr5[i] else entry * 0.01
    tp_dist = 1.5 * this_atr
    sl_dist = 1.0 * this_atr

    # LONG
    if cross_up_prev and hold_up and strength_now and trend_up and rsi_ok_long:
        if now - last_alert_time[sym_base] >= ALERT_COOLDOWN_SEC:
            msg = (f"🔔 BUY/LONG {sym_base}{FUT_SUFFIX} (5m подтверждённый)\n"
                   f"Цена: {entry:.6f}\n"
                   f"TF 5m • EMA {EMA_FAST}/{EMA_SLOW} • Тренд 15m OK\n"
                   f"Сила ≥ {STRENGTH_PCT*100:.2f}% • RSI(14) подтверждает\n"
                   f"TP ≈ {entry+tp_dist:.6f} (+{tp_dist:.6f}) • SL ≈ {entry-sl_dist:.6f} (−{sl_dist:.6f})")
            print(msg); send_telegram(msg)
            last_alert_time[sym_base] = now
        return

    # SHORT
    if cross_down_prev and hold_down and strength_now and trend_down and rsi_ok_short:
        if now - last_alert_time[sym_base] >= ALERT_COOLDOWN_SEC:
            msg = (f"🔔 SELL/SHORT {sym_base}{FUT_SUFFIX} (5m подтверждённый)\n"
                   f"Цена: {entry:.6f}\n"
                   f"TF 5m • EMA {EMA_FAST}/{EMA_SLOW} • Тренд 15m OK\n"
                   f"Сила ≥ {STRENGTH_PCT*100:.2f}% • RSI(14) подтверждает\n"
                   f"TP ≈ {entry-tp_dist:.6f} (−{tp_dist:.6f}) • SL ≈ {entry+sl_dist:.6f} (+{sl_dist:.6f})")
            print(msg); send_telegram(msg)
            last_alert_time[sym_base] = now
        return

    # Heartbeat: редкий статус без «почти сигналов»
    if now - last_heartbeat_time[sym_base] >= HEARTBEAT_SEC:
        hb = (f"ℹ️ {sym_base}{FUT_SUFFIX}: новых входов нет. Сейчас {side_5m} (5m), "
              f"цена {entry:.6f}. Тренд 15m: {'UP' if trend_up else ('DOWN' if trend_down else 'FLAT')}.")
        print(hb); send_telegram(hb)
        last_heartbeat_time[sym_base] = now

# ========= Цикл =========
def worker_loop():
    hdr = (f"🤖 Фьючерсный сигнальный бот запущен\n"
           f"Пары: {', '.join(s + FUT_SUFFIX for s in SYMBOLS)}\n"
           f"Входы: TF {WORK_TF} • EMA {EMA_FAST}/{EMA_SLOW}\n"
           f"Фильтр тренда: {HTF_TF}\n"
           f"Минимальная сила кросса: {STRENGTH_PCT*100:.2f}%\n"
           f"Кулдаун на сигналы: {ALERT_COOLDOWN_SEC//60} мин.")
    print(f"[{now_iso()}] worker started."); send_telegram(hdr)

    while True:
        for base in SYMBOLS:
            try:
                analyze_and_alert(base)
            except Exception as e:
                # сообщения об отключении уже отправлены в fetch_candles_exact
                print(f"[{base}{FUT_SUFFIX}] analyze error: {e}")
            time.sleep(SLEEP_BETWEEN_SYMBOLS)
        time.sleep(LOOP_SLEEP)

# ========= HTTP =========
@app.route("/")
def root():
    return "ok"

@app.route("/status")
def status():
    # красиво показываем disabled до какого времени
    disabled_view = {
        f"{k[0]}[{k[1]}]": {
            "reason": v["reason"],
            "until_ts": v["until_ts"],
            "until_iso": datetime.fromtimestamp(v["until_ts"], tz=timezone.utc).isoformat()
        } for k, v in disabled_symbols.items()
    }
    return jsonify({
        "ok": True,
        "mode": "futures-umcbl",
        "symbols": [s + FUT_SUFFIX for s in SYMBOLS],
        "work_tf": WORK_TF,
        "htf": HTF_TF,
        "ema": {"fast": EMA_FAST, "slow": EMA_SLOW},
        "strength_pct": STRENGTH_PCT,
        "cooldown_sec": ALERT_COOLDOWN_SEC,
        "heartbeat_sec": HEARTBEAT_SEC,
        "accepted_params": accepted_params,
        "disabled_symbols": disabled_view,
        "time": now_iso(),
        "last_band_state": last_band_state,
        "candles_count": last_candles_count,   # <- Сколько свечей получили по 5m и 15m
    })

@app.route("/ping")
def ping():
    ok = send_telegram(f"🧪 Ping от сервера: {now_iso()}")
    return jsonify({"sent": ok, "time": now_iso()})

# --- Вебхук Telegram: чтобы не было 404 и можно было слать команды ---
@app.route("/telegram", methods=["POST", "GET"])
def telegram_webhook():
    if request.method == "GET":
        return "telegram webhook ok", 200
    try:
        upd = request.get_json(force=True, silent=True) or {}
        msg = (upd.get("message") or upd.get("edited_message")) or {}
        text = (msg.get("text") or "").strip()
        if text in ("/start", "/help"):
            send_telegram("✅ Бот запущен. Команды: /status — показать текущее состояние.")
        elif text == "/status":
            lines = []
            for b in SYMBOLS:
                band = last_band_state.get(b, 'unknown')
                cnt5 = last_candles_count[b]["5m"]
                cnt15 = last_candles_count[b]["15m"]
                lines.append(f"{b}{FUT_SUFFIX}: {band} • candles 5m={cnt5}, 15m={cnt15}")
            send_telegram("📊 Статус:\n" + "\n".join(lines))
    except Exception as e:
        print(f"[telegram_webhook] error: {e}")
    return "OK", 200

def run():
    th = threading.Thread(target=worker_loop, daemon=True)
    th.start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=False)

if __name__ == "__main__":
    run()
