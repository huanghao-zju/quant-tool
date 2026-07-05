"""CFTC 日元投机净头寸（SPEC §2.2-14）。

Socrata 开放 API，期货 legacy 报告（6dca-aqww），日元合约代码 097741。
net = non-commercial long − short（负值 = 净空头）。
按报告日期（周二）对齐，而非发布日期（周五）——SPEC §9。
"""
from __future__ import annotations

import pandas as pd

from .base import http_get, record_fetch, _cfg

URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
JPY_CODE = "097741"


def fetch(start: str | None = None, end: str | None = None) -> pd.Series | None:
    weeks = _cfg()["fetch"]["cftc_lookback_weeks"]
    where = f"cftc_contract_market_code='{JPY_CODE}'"
    if start:
        where += f" AND report_date_as_yyyy_mm_dd>='{start}T00:00:00.000'"
    if end:
        where += f" AND report_date_as_yyyy_mm_dd<='{end}T00:00:00.000'"
    params = {
        "$where": where,
        "$select": "report_date_as_yyyy_mm_dd,noncomm_positions_long_all,noncomm_positions_short_all",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": str(weeks),
    }
    r = http_get(URL, params=params)
    s = None
    if r is not None:
        try:
            rows = r.json()
            s = pd.Series({
                pd.Timestamp(row["report_date_as_yyyy_mm_dd"][:10]):
                    float(row["noncomm_positions_long_all"]) - float(row["noncomm_positions_short_all"])
                for row in rows
            }).sort_index()
        except Exception:
            s = None
    ok = s is not None and not s.empty
    record_fetch("cftc:jpy", ok)
    return s if ok else None
