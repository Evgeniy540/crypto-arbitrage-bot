  arrow = "🟢 LONG"
                conf = "✅ A" if res["confidence"]=="A" else "✔️ B"
                tg_send(
                    f"{arrow} сигнал {res['symbol']}\n"
                    f"Цена: ~ {fmt_price(res['price'])}\n"
                    f"TP: {fmt_price(res['tp'])} ({pct(TP_PCT)}) | SL: {fmt_price(res['sl'])} ({pct(SL_PCT)})\n"
                    f"RSI(5m): {res['rsi']} | Объём спайк: {'да' if res['vol_spike'] else 'нет'} | Уверенность: {conf}\n"
                    f"EMA5m 9/21: {res['ema5'][0]} / {res['ema5'][1]} | Тренд 1h: {res['ema1h'][0]} / {res['ema1h'][1]}"
                )

                # регистрируем и, если включено, покупаем
                register_signal(res['symbol'], "LONG", res['price'], res['tp'], res['sl'], source="EMA 9/21")
                try_autobuy(res['symbol'], res['price'], res['tp'], res['sl'])

            now = time.time()
            if not any_signal and now - last_no_signal_sent >= GLOBAL_OK_COOLDOWN:
                last_no_signal_sent = now
                tg_send("ℹ️ Пока без новых сигналов. Проверяю рынок…")
        except Exception as e:
            log.exception(f"Loop error: {e}")
        time.sleep(CHECK_EVERY_SEC)

# ====== FLASK ======
@app.route("/")
def home():
    return "Signals v3.2 running (SPOT V2 + AUTOTRADE). UTC: " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

@app.route("/positions")
def positions_view():
    try:
        pos = load_positions()
        opened = {k:v for k,v in pos.items() if v.get("is_open")}
        return {
            "opened": opened,
            "server_time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        }
    except Exception as e:
        return {"error": str(e)}, 500

def start_loop():
    threading.Thread(target=run_loop, daemon=True).start()

if __name__ == "__main__":
    start_closer()
    start_loop()
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
              
