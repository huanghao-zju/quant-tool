"""FRED 数据源。优先官方 REST（FRED_API_KEY），缺失时回退 fredgraph.csv 公开接口。

fredgraph.csv 同时是长历史回测取数通道（SPEC §9：BAMLH0A0HYM2 API 仅保留3年观测）。
"""
from __future__ import annotations

import os

import pandas as pd

from .base import csv_to_series, http_get, record_fetch

API_URL = "https://api.stlouisfed.org/fred/series/observations"
GRAPH_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


def fetch(series_id: str, start: str | None = None, end: str | None = None) -> pd.Series | None:
    s = None
    key = os.environ.get("FRED_API_KEY")
    if key:
        params = {"series_id": series_id, "api_key": key, "file_type": "json"}
        if start:
            params["observation_start"] = start
        if end:
            params["observation_end"] = end
        r = http_get(API_URL, params=params)
        if r is not None:
            try:
                obs = r.json()["observations"]
                s = pd.Series(
                    {pd.Timestamp(o["date"]): float(o["value"]) for o in obs if o["value"] != "."}
                ).sort_index()
            except Exception:
                s = None
    if s is None or s.empty:
        params = {"id": series_id}
        if start:
            params["cosd"] = start
        if end:
            params["coed"] = end
        r = http_get(GRAPH_URL, params=params)
        if r is not None:
            df_text = r.text
            # fredgraph.csv 列名: observation_date,<SERIES_ID>（旧版为 DATE）
            date_col = "observation_date" if "observation_date" in df_text.splitlines()[0] else "DATE"
            s = csv_to_series(df_text, date_col, series_id)
    ok = s is not None and not s.empty
    record_fetch(f"fred:{series_id}", ok)
    return s if ok else None
