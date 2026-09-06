#!/usr/bin/env python3
"""Диагностика (владелец не просил -- реальный сбой обнаружен по факту:
`sleeping_refs_macro_metrics.py` получил y_method=futures_fx_intraday
для ES=F/GC=F/... но 0 реальных строк по ВСЕМ 8 доступным выходным --
подозрительно системно, не гадаем причину, смотрим на сырые данные).

Реальный часовой intraday с yfinance для ES=F вокруг ОДНОГО недавнего
выходного (2026-08-28..08-31) -- печатаем ВСЕ реальные бары с их
реальными UTC-таймстемпами, чтобы увидеть, покрывает ли ответ Yahoo
границы пт17:00/вс17:55/вс18:00/вс19:00 ET вообще."""
from __future__ import annotations

import pandas as pd
import yfinance as yf

SYMBOL = "ES=F"
START = "2026-08-27"
END = "2026-09-01"


def run() -> int:
    hist = yf.Ticker(SYMBOL).history(start=START, end=END, interval="60m")
    print(f"реальных строк: {len(hist)}")
    print(f"реальный index dtype/tz: {hist.index.dtype}, tz={hist.index.tz}")
    hist_utc = hist.copy()
    hist_utc.index = pd.to_datetime(hist_utc.index, utc=True)
    for ts, row in hist_utc.iterrows():
        et = ts.tz_convert("America/New_York")
        print(f"  {ts.isoformat()}  (ET: {et.isoformat()})  close={row['Close']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
