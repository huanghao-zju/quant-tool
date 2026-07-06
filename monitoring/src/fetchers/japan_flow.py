"""日本对外证券投资周度流量（SPEC §2.3-18）。

传导确认信号：日本投资者净卖出海外中长期债 = 资金回流 = 套息平仓的资金面印记。

数据源：财务省"対外及び対内証券投資（週次）" week.csv（Shift-JIS，多层表头）。
列布局（0-indexed，参见官方表头）：
  0  期間 Period（"2026．6．22〜 6．28" 形式）
  1-3   対外・株式（取得/処分/ネット）
  4-6   対外・中長期債（取得/処分/ネット）   ← col6 = 净额（取得−处分）
负 col6 = 净卖出海外中长期债 = 回流信号。单位：亿日元。
"""
from __future__ import annotations

import io
import logging
import re

import pandas as pd

from .base import http_get, record_fetch

log = logging.getLogger("fetchers.japan_flow")

URL = ("https://www.mof.go.jp/policy/international_policy/reference/"
       "itn_transactions_in_securities/week.csv")
HEADERS = {"User-Agent": "Mozilla/5.0"}
NET_COL = 6  # 対外・中長期債・ネット

_PERIOD = re.compile(r"^\s*(\d{4})．(\d{1,2})．(\d{1,2})")


def _period_start(cell: str) -> pd.Timestamp | None:
    m = _PERIOD.match(str(cell).replace("\n", ""))
    if not m:
        return None
    try:
        return pd.Timestamp(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except Exception:
        return None


def _num(cell) -> float | None:
    try:
        return float(str(cell).replace(",", "").replace(" ", "").strip())
    except (ValueError, AttributeError):
        return None


def fetch() -> pd.Series | None:
    """返回对外中长期债周度净流量序列（亿日元；负=净卖出=回流）。"""
    r = http_get(URL, headers=HEADERS)
    s = None
    if r is not None:
        try:
            r.encoding = "shift_jis"
            rows = list(io.StringIO(r.text))
            recs: dict[pd.Timestamp, float] = {}
            for line in rows:
                cols = _split_csv(line)
                if len(cols) <= NET_COL:
                    continue
                d = _period_start(cols[0])
                v = _num(cols[NET_COL])
                if d is not None and v is not None:
                    recs[d] = v
            if recs:
                s = pd.Series(recs).sort_index()
        except Exception as e:
            log.warning("MOF week.csv parse failed: %r", e)
            s = None
    ok = s is not None and not s.empty
    record_fetch("mof:japan_flow", ok)
    return s if ok else None


def _split_csv(line: str) -> list[str]:
    """轻量 CSV 行解析（处理带引号的千分位字段，如 \"1,689 \"）。"""
    import csv
    return next(csv.reader([line]))
