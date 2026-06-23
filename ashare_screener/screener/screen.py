"""筛选逻辑：合并行情 + 财务数据，按配置过滤排序。"""

from __future__ import annotations

import pandas as pd

OPS = {
    ">": lambda s, v: s > v,
    ">=": lambda s, v: s >= v,
    "<": lambda s, v: s < v,
    "<=": lambda s, v: s <= v,
    "==": lambda s, v: s == v,
    "!=": lambda s, v: s != v,
    "between": lambda s, v: s.between(v[0], v[1]),
    "in": lambda s, v: s.isin(v),
    "contains": lambda s, v: s.astype(str).str.contains(v, na=False),
}


def merge_frames(
    spot: pd.DataFrame,
    financials: pd.DataFrame,
    metrics: pd.DataFrame | None = None,
) -> pd.DataFrame:
    fin = financials.drop(columns=["name"], errors="ignore")
    df = spot.merge(fin, on="code", how="inner")
    if metrics is not None and not metrics.empty:
        df = df.merge(metrics, on="code", how="left")
    return df


def add_derived(df: pd.DataFrame) -> pd.DataFrame:
    """派生估值字段，供 config 直接筛选：

    peg  = pe_ttm / 净利同比增速（单期，波动大，仅作对比）
    pegy = pe_ttm / (增速 + 股息率)  —— 彼得林奇改进式
           增速优先用多年净利 CAGR（cagr_3y），缺失则回退单期同比；
           股息率（div_yield）缺失按 0 计。
    """
    if "pe_ttm" not in df.columns or "net_profit_yoy" not in df.columns:
        return df
    pe = pd.to_numeric(df["pe_ttm"], errors="coerce")
    yoy = pd.to_numeric(df["net_profit_yoy"], errors="coerce")
    df["peg"] = pe / yoy

    growth = yoy
    if "cagr_3y" in df.columns:
        growth = pd.to_numeric(df["cagr_3y"], errors="coerce").fillna(yoy)
    dy = (
        pd.to_numeric(df["div_yield"], errors="coerce").fillna(0)
        if "div_yield" in df.columns
        else 0
    )
    df["pegy"] = pe / (growth + dy)
    return df


def apply_filters(df: pd.DataFrame, filters: list[dict]) -> pd.DataFrame:
    """filters 形如 [{"field": "roe", "op": ">=", "value": 15}, ...]，
    任一条件中字段为 NaN 的行直接淘汰。"""
    mask = pd.Series(True, index=df.index)
    for f in filters:
        field, op, value = f["field"], f["op"], f["value"]
        if field not in df.columns:
            raise KeyError(f"未知筛选字段: {field}，可用字段: {sorted(df.columns)}")
        if op not in OPS:
            raise KeyError(f"未知操作符: {op}，支持: {sorted(OPS)}")
        mask &= OPS[op](df[field], value).fillna(False)
    return df[mask]


def screen_frames(
    spot: pd.DataFrame,
    financials: pd.DataFrame,
    config: dict,
    metrics: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """纯函数版筛选入口，便于单测；CLI 从缓存读出各表后调它。"""
    df = merge_frames(spot, financials, metrics)
    df = add_derived(df)

    if config.get("exclude_st", True):
        df = df[~df["name"].str.contains("ST|退", na=False)]
    if config.get("exclude_bse", False):
        # 北交所代码以 8 / 4 / 92 开头
        df = df[~df["code"].str.match(r"^(8|4|92)")]

    df = apply_filters(df, config.get("filters", []))

    sort_by = config.get("sort_by")
    if sort_by:
        df = df.sort_values(sort_by, ascending=config.get("ascending", False))

    top = config.get("top")
    if top:
        df = df.head(top)

    fields = config.get("output_fields")
    if fields:
        df = df[[c for c in fields if c in df.columns]]
    return df.reset_index(drop=True)
