"""yfinance 数据源（非官方接口：重试 + 空值跳过，SPEC §9）。"""
from __future__ import annotations

import logging

import pandas as pd

from .base import record_fetch, _cfg

log = logging.getLogger("fetchers.yf")


def fetch(ticker: str, start: str | None = None, end: str | None = None,
          days: int | None = None) -> pd.Series | None:
    """返回日度收盘价 Series。"""
    retries = _cfg()["fetch"]["max_retries"]
    if days is None and start is None:
        days = _cfg()["fetch"]["history_days"]
    s = None
    for _ in range(retries):
        try:
            import yfinance as yfin
            kw = {"interval": "1d", "auto_adjust": False, "progress": False}
            if start:
                kw["start"] = start
                if end:
                    kw["end"] = end
            else:
                kw["period"] = f"{days}d"
            df = yfin.download(ticker, **kw)
            if df is not None and not df.empty:
                close = df["Close"]
                if isinstance(close, pd.DataFrame):  # 多级列
                    close = close.iloc[:, 0]
                s = close.dropna()
                s.index = pd.to_datetime(s.index).tz_localize(None)
                break
        except Exception as e:
            log.warning("yfinance %s failed: %r", ticker, e)
    ok = s is not None and not s.empty
    record_fetch(f"yf:{ticker}", ok)
    return s if ok else None
