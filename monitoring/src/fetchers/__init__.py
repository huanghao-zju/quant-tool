"""汇总所有 v1 指标（SPEC §2.1 + §2.2）。单一数据源故障不得中断整体运行。"""
from __future__ import annotations

import logging

import pandas as pd

from . import cftc, fred, jgb, srf, yf

log = logging.getLogger("fetchers")

# key → (fetcher, kwargs)
FRED_SERIES = {
    "hy_oas": "BAMLH0A0HYM2",
    "vix": "VIXCLS",
    "sofr": "SOFR",
    "iorb": "IORB",
    "payems": "PAYEMS",
    "sahm": "SAHMREALTIME",
}
YF_TICKERS = {
    "usdjpy": "JPY=X",
    "brent": "BZ=F",
    "nikkei": "^N225",
    "nasdaq": "^IXIC",
    "owl": "OWL",
    "ares": "ARES",
    "bizd": "BIZD",
}


def fetch_all(start: str | None = None, end: str | None = None) -> dict[str, pd.Series]:
    """返回 {key: pd.Series}。失败的指标不出现在结果中（降级，SPEC §6）。"""
    data: dict[str, pd.Series] = {}
    for key, sid in FRED_SERIES.items():
        s = fred.fetch(sid, start=start, end=end)
        if s is not None:
            data[key] = s
    for key, tk in YF_TICKERS.items():
        s = yf.fetch(tk, start=start, end=end)
        if s is not None:
            data[key] = s
    for tenor, key in (("10", "jgb10"), ("20", "jgb20"), ("30", "jgb30")):
        s = jgb.fetch(tenor, full_history=start is not None)
        if s is not None:
            data[key] = s if start is None else s.loc[:end or None]
    s = cftc.fetch(start=None, end=end)  # 需要2年历史算分位，start 不截断
    if s is not None:
        data["cftc_jpy"] = s
    if start is None:  # SRF 端点只有近90天，回测模式跳过
        s = srf.fetch()
        if s is not None:
            data["srf"] = s
    # VIX 回退：FRED 失败用 ^VIX
    if "vix" not in data:
        s = yf.fetch("^VIX", start=start, end=end)
        if s is not None:
            data["vix"] = s
    return data
