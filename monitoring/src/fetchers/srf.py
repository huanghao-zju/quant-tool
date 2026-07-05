"""纽联储 SRF（常备回购便利）用量（SPEC §2.2-15）。

NY Fed Markets 公开 JSON API，repo operations 端点。
返回日度 totalAmtAccepted（十亿美元）。
"""
from __future__ import annotations

import logging

import pandas as pd

from .base import http_get, record_fetch

log = logging.getLogger("fetchers.srf")

URL = "https://markets.newyorkfed.org/api/rp/repo/all/results/last/90.json"


def fetch() -> pd.Series | None:
    r = http_get(URL)
    s = None
    if r is not None:
        try:
            ops = r.json().get("repo", {}).get("operations", [])
            daily: dict[pd.Timestamp, float] = {}
            for op in ops:
                d = pd.Timestamp(op["operationDate"])
                amt = float(op.get("totalAmtAccepted") or 0) / 1e9
                daily[d] = daily.get(d, 0.0) + amt
            if daily:
                s = pd.Series(daily).sort_index()
        except Exception as e:
            log.warning("SRF parse failed: %r", e)
    ok = s is not None and not s.empty
    record_fetch("nyfed:srf", ok)
    return s if ok else None
