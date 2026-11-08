"""筛选逻辑：合并行情 + 财务数据，按配置过滤排序。"""

from __future__ import annotations

import pandas as pd

# 报告期结束月 -> 年化倍数。
# 东财「业绩报表」的 ROE / 营收 / 净利 都是报告期累计值而非年化值，
# 所以同一个阈值在一季报和年报上含义完全不同（一季报 ROE>=12 相当于
# 年化 48%）。这里给出朴素年化倍数，把阈值拉回可比口径。
ANNUALIZE_FACTOR = {3: 4.0, 6: 2.0, 9: 4.0 / 3.0, 12: 1.0}

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
    history: pd.DataFrame | None = None,
) -> pd.DataFrame:
    fin = financials.drop(columns=["name"], errors="ignore")
    df = spot.merge(fin, on="code", how="inner")
    if metrics is not None and not metrics.empty:
        df = df.merge(metrics, on="code", how="left")
    if history is not None and not history.empty:
        df = df.merge(add_stability(history), on="code", how="left")
    return df


def add_stability(history: pd.DataFrame) -> pd.DataFrame:
    """从多期财报拼出的宽表算出「连续正增长」标记。

    ⚠ 只用同比增速（`revenue_yoy` / `net_profit_yoy`）判断，不碰累计绝对值：
    一季报营收只累计 3 个月、半年报累计 6 个月，跨期直接比大小毫无意义
    （实测 96.6% 的公司半年报营收都大于一季报，那是口径不是增长）。
    增速本身已是同比，与报告期长度无关，可以横向比较。

    产出两个 0/1 字段，两期都为正才算 1：
    - revenue_stable  营收连续正增长
    - profit_stable   净利连续正增长

    用于挡掉"单期暴增"的周期股：它们的 yoy 常是一期高得离谱、下一期转负。
    """
    if history.empty or "code" not in history.columns:
        return pd.DataFrame()
    out = history[["code"]].copy()
    for src, dst in (
        ("revenue_yoy", "revenue_stable"),
        ("net_profit_yoy", "profit_stable"),
    ):
        cols = [c for c in history.columns if c.startswith(f"{src}@")]
        if not cols:
            continue
        block = history[cols].apply(pd.to_numeric, errors="coerce")
        # 每期都必须 > 0（缺失或为负都算不通过），期数越多门槛越严
        out[dst] = ((block > 0).all(axis=1) & block.notna().all(axis=1)).astype(int)
    return out


def annualize_factor(report_date: str | None) -> float:
    """报告期累计值换算成年化值的倍数。无法识别时返回 1.0。"""
    try:
        return ANNUALIZE_FACTOR[int(str(report_date)[4:6])]
    except (ValueError, TypeError, KeyError):
        return 1.0


def add_derived(df: pd.DataFrame) -> pd.DataFrame:
    """派生估值字段，供 config 直接筛选：

    roe_annual = roe × 年化倍数（一季报 ×4、半年报 ×2、三季报 ×4/3、年报 ×1）
           业绩报表的 roe 是报告期累计值，直接卡阈值会随报告期漂移，
           想要「ROE 不低于 12%」这种年度口径的条件请用这个字段。
    peg  = pe_ttm / 净利同比增速（单期，波动大，仅作对比）
    pegy = pe_ttm / (增速 + 股息率)  —— 彼得林奇改进式
           增速优先用多年净利 CAGR（cagr_3y），缺失则回退单期同比；
           股息率（div_yield）缺失按 0 计。
           ⚠ 分母 ≤0（增速为负）或 pe_ttm ≤0（公司亏损）时置 NaN，
             否则「负 PE ÷ 负增速」会得到一个漂亮的小正数，把亏损股
             误判成低估值好标的。
    has_dividend = 1 当该股有分红记录，否则 0
           div_yield 缺失实际是「查不到分红方案」，多数是公司从未分红。
           这个字段把「不分红」和「股息率低」区分开，可直接筛选。
    """
    if "roe" in df.columns and "report_date" in df.columns:
        rd = df["report_date"].iloc[0] if len(df) else None
        df["roe_annual"] = pd.to_numeric(df["roe"], errors="coerce") * annualize_factor(rd)
    if "div_yield" in df.columns:
        dy = pd.to_numeric(df["div_yield"], errors="coerce")
        df["has_dividend"] = dy.notna().astype(int)
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
    denom = growth + dy

    # 亏损股陷阱：PE 为负（公司亏损）且增速也为负时，pe/denom 仍是正数，
    # 且往往很小 —— 例如 PE=-1.6、增速=-25% 算出 pegy=0.06，会被「pegy<0.5」
    # 判成"极低估"，实测有 441 只亏损股这样蒙混进来。所以分母为负、或 PE 为负
    # （增速口径下 PE<0 即亏损）一律置 NaN，让含 NaN 的筛选条件直接淘汰它们。
    df["pegy"] = pe.where((denom > 0) & (pe > 0)) / denom.where(denom > 0)
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


def cap_by_industry(df: pd.DataFrame, cap: int | None) -> pd.DataFrame:
    """按行业限流：每个行业最多保留 `cap` 只，超出的丢弃。

    必须在排序之后调用 —— 限流的意图是「每个行业只留最好的 N 只」，
    所以要先有优劣顺序。行业名缺失的归入「未知」一档，不占其他行业名额。

    不设 cap（或 cap <= 0）时原样返回，config 里不写这项即可保持旧行为。
    """
    if not cap or cap <= 0 or "industry" not in df.columns:
        return df
    industry = df["industry"].fillna("未知")
    # 按当前行序（即排序后的优劣顺序）逐行取，每行业取满 cap 就跳过后续同行业
    rank = industry.groupby(industry).cumcount()
    return df[rank < cap]


def screen_frames(
    spot: pd.DataFrame,
    financials: pd.DataFrame,
    config: dict,
    metrics: pd.DataFrame | None = None,
    history: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """纯函数版筛选入口，便于单测；CLI 从缓存读出各表后调它。

    `history` 是多期财报宽表（见 Cache.load_financials_history），传入后会
    多出 revenue_stable / profit_stable 两个连续正增长标记。
    """
    df = merge_frames(spot, financials, metrics, history)
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

    # 限流在排序之后：留下的是每个行业里按 sort_by 排最好的 cap 只。
    # 放在 top 之前，被限流掉的行业不会因为 top 截断而"挤"进来。
    df = cap_by_industry(df, config.get("max_per_industry"))

    top = config.get("top")
    if top:
        df = df.head(top)

    fields = config.get("output_fields")
    if fields:
        df = df[[c for c in fields if c in df.columns]]
    return df.reset_index(drop=True)
