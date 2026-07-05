"""日本财务省国债收益率（SPEC §2.2-13）。

主通道：MOF 官网 jgbcm 系列 CSV（日期为和历，如 R6.8.5）。
回退：akshare 全球债券接口（可选依赖，国内网络环境用）。
"""
from __future__ import annotations

import io
import logging

import pandas as pd

from .base import http_get, record_fetch

log = logging.getLogger("fetchers.jgb")

URL_CURRENT = "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"
URL_ALL = "https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv"

# 和历 → 西历基准年（元年）
_ERA = {"M": 1867, "T": 1911, "S": 1925, "H": 1988, "R": 2018}


def _wareki_to_ts(s: str) -> pd.Timestamp | None:
    try:
        era, rest = s[0], s[1:]
        y, m, d = rest.split(".")
        return pd.Timestamp(_ERA[era] + int(y), int(m), int(d))
    except Exception:
        return None


def _parse_mof_csv(text: str, tenor_col: str) -> pd.Series | None:
    try:
        df = pd.read_csv(io.StringIO(text), skiprows=1, encoding_errors="ignore")
        df.columns = [c.strip() for c in df.columns]
        date_col = df.columns[0]
        idx = df[date_col].astype(str).map(_wareki_to_ts)
        vals = pd.to_numeric(df[tenor_col], errors="coerce")
        s = pd.Series(vals.values, index=idx).dropna()
        s = s[s.index.notna()]
        return s.sort_index()
    except Exception as e:
        log.warning("MOF csv parse failed: %r", e)
        return None


def fetch(tenor: str = "10", full_history: bool = False) -> pd.Series | None:
    """tenor: '10' | '20' | '30'（年）。返回收益率 %。"""
    col = f"{tenor}年"  # MOF CSV 列名为中/日文年限（如 "10年"），缺失值为 "-"
    # 当月文件 jgbcm.csv 仅含当月（月初可能仅数行，不足以算单周/两周速度型），
    # 全历史 jgbcm_all.csv 通常滞后数日。live 时两者拼接：全历史提供回溯窗口，
    # 当月覆盖最新交易日。
    parts = []
    for url in ([URL_ALL] if full_history else [URL_ALL, URL_CURRENT]):
        r = http_get(url)
        if r is not None:
            r.encoding = "shift_jis"
            p = _parse_mof_csv(r.text, col)
            if p is not None and not p.empty:
                parts.append(p)
    if parts:
        s = pd.concat(parts)
        s = s[~s.index.duplicated(keep="last")].sort_index()
    else:
        s = _fetch_akshare(tenor)
    ok = s is not None and not s.empty
    record_fetch(f"jgb:{tenor}y", ok)
    return s if ok else None


def _fetch_akshare(tenor: str) -> pd.Series | None:
    try:
        import akshare as ak
        df = ak.bond_zh_us_rate()  # 含日本10年期时才可用；其余期限无回退
        col = {"10": "日本国债收益率10年"}.get(tenor)
        if col and col in df.columns:
            s = pd.Series(pd.to_numeric(df[col], errors="coerce").values,
                          index=pd.to_datetime(df["日期"])).dropna()
            return s.sort_index()
    except Exception as e:
        log.warning("akshare fallback failed: %r", e)
    return None
