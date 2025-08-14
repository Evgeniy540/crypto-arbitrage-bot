import os, time, json, math, threading, socketserver, http.server
from datetime import datetime, timezone
from typing import List, Dict, Tuple
import requests

# ===============================
# НАСТРОЙКИ
# ===============================
TELEGRAM_BOT_TOKEN = "7630671081:AAG17gVyITruoH_CYreudyTBm5RTpvNgwMA"
TELEGRAM_CHAT_ID   = "5723086631"

# Монеты для слежения (Spot)
SYMBOLS = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","TRXUSDT","PEPEUSDT","BGBUSDT"]

# Стратегия
EMA_FAST = 7
EMA_SLOW = 14
TF_FAST  = "1min"   # рабочий ТФ
TF_CONF  = "5min"   # подтверждение тренда

TP_PCT = 0.40/100    # минимальный профит, чтобы отправить сигнал (напр. 0.40%)
SL_PCT = 0.25/100    # виртуальный стоп для оценки R:R
MIN_RR  = 1.2        # минимальное соотношение TP/SL (R>=1.2)
ATR_LEN = 14         # длина ATR
ATR_GATE = 0.8       # требуемая волатильность: ATR% >= TP_PCT*ATR_GATE

POLL_SEC = 20        # период опроса символов

BITGET = "https://api.bitget.com"
HEADERS = {"User-Agent":"signal-bot/1.0"}

# Память для антиспама (последнее направление кросса)
last_cross_state: Dict[str, str] = {}   # symbol -> "long"/"short"/"none"

# ===============================
# ВСПОМОГАТЕЛЬНЫЕ
# ===============================
def ts_iso(ts_ms:int)->str:
    return datetime.fromtimestamp(ts_ms/1000, tz=timezone.utc).isoformat()

def ema(series:List[float], n:int)->List[float]:
    if len(series) < n: return []
    k = 2/(n+1)
    out = [None]*(n-1)
    sm = sum(series[:n])/n
    out.append(sm)
    for i in range(n, len(series)):
        sm = series[i]*k + sm*(1-k)
        out.append(sm)
    return out

def true_range(h:List[float], l:List[float], c:List[float])->List[float]:
    tr = [h[0]-l[0]]
    for i in range(1,len(c)):
        tr.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    return tr

def atr(h:List[float], l:List[float], c:List[float], n:int)->List[float]:
    tr = true_range(h,l,c)
    return ema(tr, n)

def pct(a:float, b:float)->float:
    return (a-b)/b

def get_candles(symbol:str, granularity:str, limit:int=200)->Tuple[List[int],List[float],List[float],List[float],List[float]]:
    """
    Возвращает (t, o, h, l, c) отсортированные по времени (старые -> новые).
    Bitget v2 spot/market/candles: [ts, open, high, low, close, volume]
    """
    url = f"{BITGET}/api/v2/spot/market/candles"
    params = {"symbol":symbol, "granularity":granularity, "limit":str(limit)}
    r = requests.get(url, params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    # Ожидаем {'code':'00000','data':[[...],...]}
    if isinstance(data, dict) and "data" in data:
        rows = data["data"]
    else:
        rows = data
    # приходят от новых к старым -> перевернем
    rows = list(reversed(rows))
    t,o,h,l,c = [],[],[],[],[]
    for row in rows:
        # строки или числа - приведем
        ts = int(row[0])
        o1 = float(row[1]); h1=float(row[2]); l1=float(row[3]); c1=float(row[4])
        t.append(ts); o.append(o1); h.append(h1); l.append(l1); c.append(c1)
    return t,o,h,l,c

def send_tg(text:str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print("TELEGRAM_ERROR:", e)

def crossed_up(fast:List[float], slow:List[float])->bool:
    if len(fast)<2 or len(slow)<2: return False
    return fast[-2] is not None and slow[-2] is not None and fast[-1] is not None and slow[-1] is not None and fast[-2] < slow[-2] and fast[-1] > slow[-1]

def crossed_down(fast:List[float], slow:List[float])->bool:
    if len(fast)<2 or len(slow)<2: return False
    return fast[-2] is not None and slow[-2] is not None and fast[-1] is not None and slow[-1] is not None and fast[-2] > slow[-2] and fast[-1] < slow[-1]

def rr_okay(price:float, tp_pct:float, sl_pct:float, min_rr:float)->bool:
    rr = tp_pct/sl_pct if sl_pct>0 else 0
    return rr >= min_rr

def enough_volatility(close:List[float])->bool:
    # ATR% от цены
    # Сгенерим High/Low как +- скользящий High/Low (если Bitget дал реальные high/low — ок, мы передаем их)
    return True  # будет оценено выше в generate_signal (по реальным H/L)

# ===============================
# СИГНАЛЫ
# ===============================
def trend_confirmed(symbol:str)->str:
    """Подтверждение тренда по 5m: возвращает 'bull'/'bear'/'none'"""
    try:
        t,o,h,l,c = get_candles(symbol, TF_CONF, 200)
        ef = ema(c, EMA_FAST); es = ema(c, EMA_SLOW)
        if not ef or not es: return "none"
        if ef[-1] > es[-1]: return "bull"
        if ef[-1] < es[-1]: return "bear"
        return "none"
    except Exception:
        return "none"

def generate_signal(symbol:str):
    global last_cross_state
    try:
        # 1) Основные свечи 1m
        t,o,h,l,c = get_candles(symbol, TF_FAST, 300)
        ef = ema(c, EMA_FAST)
        es = ema(c, EMA_SLOW)
        if not ef or not es or len(ef)!=len(c) or len(es)!=len(c):
            return

        # 2) ATR на 1m (волатильность)
        a = atr(h,l,c, ATR_LEN)
        if not a or a[-1] is None:
            return
        atr_pct = a[-1] / c[-1]

        # 3) Подтверждение тренда 5m
        conf = trend_confirmed(symbol)

        price = c[-1]
        now_iso = ts_iso(t[-1])

        # long-кросс
        if crossed_up(ef, es):
            # антиспам
            if last_cross_state.get(symbol) == "long":
                return
            last_cross_state[symbol] = "long"

            # фильтры
            if conf != "bull":
                return
            if atr_pct < TP_PCT*ATR_GATE:
                return
            if not rr_okay(price, TP_PCT, SL_PCT, MIN_RR):
                return

            tp = price*(1+TP_PCT)
            sl = price*(1-SL_PCT)

            msg = (
                f"🔔 BUY {symbol}\n"
                f"Цена: {price:.6f}\n"
                f"EMA{EMA_FAST} vs EMA{EMA_SLOW} (TF {TF_FAST}) + подтверждение {TF_CONF}\n"
                f"ATR%≈{atr_pct*100:.2f}%  | TP {TP_PCT*100:.2f}%  SL {SL_PCT*100:.2f}%  R≈{TP_PCT/SL_PCT:.2f}\n"
                f"TP: {tp:.6f}  | SL: {sl:.6f}\n"
                f"{now_iso}"
            )
            send_tg(msg)

        # short-кросс (для спота — это сигнал на ПРОДАЖУ/выход)
        elif crossed_down(ef, es):
            if last_cross_state.get(symbol) == "short":
                return
            last_cross_state[symbol] = "short"

            if conf != "bear":
                return
            if atr_pct < TP_PCT*ATR_GATE:
                return
            if not rr_okay(price, TP_PCT, SL_PCT, MIN_RR):
                return

            tp = price*(1-TP_PCT)
            sl = price*(1+SL_PCT)

            msg = (
                f"🔔 SELL {symbol}\n"
                f"Цена: {price:.6f}\n"
                f"EMA{EMA_FAST} vs EMA{EMA_SLOW} (TF {TF_FAST}) + подтверждение {TF_CONF}\n"
                f"ATR%≈{atr_pct*100:.2f}%  | TP {TP_PCT*100:.2f}%  SL {SL_PCT*100:.2f}%  R≈{TP_PCT/SL_PCT:.2f}\n"
                f"TP: {tp:.6f}  | SL: {sl:.6f}\n"
                f"{now_iso}"
            )
            send_tg(msg)
        # нет нового кросса — ничего не шлём
    except requests.HTTPError as e:
        print(f"{symbol} HTTP_ERROR:", e.response.text if e.response else e)
    except Exception as e:
        print(f"{symbol} ERROR:", e)

# ===============================
# ЛЁГКИЙ HEALTH-СЕРВЕР (для Render Web)
# ===============================
class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/","/health","/favicon.ico"):
            self.send_response(200); self.send_header("Content-Type","text/plain"); self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404); self.end_headers()

def start_http_if_needed():
    port = os.getenv("PORT")
    if not port: 
        return
    port = int(port)
    def run():
        with socketserver.TCPServer(("", port), Handler) as httpd:
            print(f"Health server on :{port}")
            httpd.serve_forever()
    th = threading.Thread(target=run, daemon=True)
    th.start()

# ===============================
# MAIN LOOP
# ===============================
def main():
    start_http_if_needed()
    send_tg(f"🤖 Бот запущен! EMA {EMA_FAST}/{EMA_SLOW}, TF {TF_FAST}. Сообщения — только по факту новых пересечений.")
    for s in SYMBOLS:
        last_cross_state.setdefault(s,"none")

    while True:
        for s in SYMBOLS:
            generate_signal(s)
            time.sleep(0.2)  # чуток между запросами
        time.sleep(POLL_SEC)

if __name__ == "__main__":
    main()
