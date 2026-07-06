"""Cboe 3个月隐含相关性指数 COR3M（SPEC §2.3-16）。

脆弱性结构信号：相关性处于历史低位 = 分散度极高 = 一旦冲击到来易同步崩塌。
2024-07-03 崩盘前低点 7.63；2026-07 读数 ~7.8。低读数 = 脆弱。

Cboe CDN 公开 CSV（DATE,OPEN,HIGH,LOW,CLOSE，DATE 为 MM/DD/YYYY）。
"""
from __future__ import annotations

import io
import logging

import pandas as pd

from .base import http_get, record_fetch

log = logging.getLogger("fetchers.cor3m")

URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/COR3M_History.csv"
# Cboe CDN 对部分 UA 返回 403，带常规浏览器 UA。
HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch() -> pd.Series | None:
    """返回 COR3M 收盘序列（指数点，DatetimeIndex）。"""
    r = http_get(URL, headers=HEADERS)
    s = None
    if r is not None:
        try:
            df = pd.read_csv(io.StringIO(r.text))
            idx = pd.to_datetime(df["DATE"], format="%m/%d/%Y", errors="coerce")
            s = pd.Series(pd.to_numeric(df["CLOSE"], errors="coerce").values, index=idx).dropna()
            s = s[s.index.notna()].sort_index()
        except Exception as e:
            log.warning("COR3M parse failed: %r", e)
            s = None
    ok = s is not None and not s.empty
    record_fetch("cboe:cor3m", ok)
    return s if ok else None
