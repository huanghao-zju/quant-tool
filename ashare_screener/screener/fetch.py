"""基于 AkShare 的全市场批量拉取。

只使用"一次请求返回全市场"的批量接口，避免逐股发 5000+ 次请求触发风控：
- 行情快照: ak.stock_zh_a_spot_em()          沪深京全部 A 股，含 PE/PB/市值
- 业绩报表: ak.stock_yjbb_em(date=报告期)     全市场财务摘要，含营收/净利/ROE/毛利率

注意：AkShare 返回的是中文列名，这里统一重命名为英文字段，筛选配置里
引用的就是这些英文字段名。东财改版可能导致列名变化，跑挂时优先核对
下面两个映射表。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

# ak.stock_zh_a_spot_em() 列名 -> 标准字段
SPOT_COLUMNS = {
    "代码": "code",
    "名称": "name",
    "最新价": "price",
    "涨跌幅": "pct_change",
    "成交额": "amount",
    "换手率": "turnover_rate",
    "量比": "volume_ratio",
    "市盈率-动态": "pe_ttm",
    "市净率": "pb",
    "总市值": "total_mcap",
    "流通市值": "float_mcap",
    "60日涨跌幅": "pct_60d",
    "年初至今涨跌幅": "pct_ytd",
}

# ak.stock_yjbb_em(date=...) 列名 -> 标准字段
FINANCIAL_COLUMNS = {
    "股票代码": "code",
    "股票简称": "name",
    "每股收益": "eps",
    "营业总收入-营业总收入": "revenue",
    "营业总收入-同比增长": "revenue_yoy",
    "净利润-净利润": "net_profit",
    "净利润-同比增长": "net_profit_yoy",
    "每股净资产": "bps",
    "净资产收益率": "roe",
    "每股经营现金流量": "ocf_per_share",
    "销售毛利率": "gross_margin",
    "所属行业": "industry",
    "最新公告日期": "announce_date",
}

QUARTER_ENDS = ((3, 31), (6, 30), (9, 30), (12, 31))


def report_dates(today: dt.date | None = None, n: int = 8) -> list[str]:
    """最近 n 个已结束的报告期，新到旧，格式 YYYYMMDD。

    报告期结束不代表财报已披露（一季报最晚 4 月底才出），调用方应在
    最近一期数据为空时自动回退到上一期。
    """
    today = today or dt.date.today()
    dates: list[str] = []
    year = today.year
    while len(dates) < n:
        for m, d in reversed(QUARTER_ENDS):
            q = dt.date(year, m, d)
            if q <= today:
                dates.append(q.strftime("%Y%m%d"))
                if len(dates) >= n:
                    break
        year -= 1
    return dates


def _rename(df: pd.DataFrame, mapping: dict[str, str]) -> pd.DataFrame:
    missing = [c for c in mapping if c not in df.columns]
    if missing:
        raise RuntimeError(
            f"AkShare 返回缺少列 {missing}，可能是接口改版，请核对列名映射"
        )
    return df[list(mapping)].rename(columns=mapping)


def fetch_spot() -> pd.DataFrame:
    """全市场实时行情快照，一次请求返回全部 5000+ 只。"""
    import akshare as ak

    df = _rename(ak.stock_zh_a_spot_em(), SPOT_COLUMNS)
    df["code"] = df["code"].astype(str).str.zfill(6)
    num_cols = [c for c in df.columns if c not in ("code", "name")]
    df[num_cols] = df[num_cols].apply(pd.to_numeric, errors="coerce")
    return df.dropna(subset=["price"]).reset_index(drop=True)


def fetch_financials(report_date: str) -> pd.DataFrame:
    """指定报告期的全市场业绩报表（东财已自动分页拉全）。"""
    import akshare as ak

    df = _rename(ak.stock_yjbb_em(date=report_date), FINANCIAL_COLUMNS)
    df["code"] = df["code"].astype(str).str.zfill(6)
    num_cols = [
        c for c in df.columns if c not in ("code", "name", "industry", "announce_date")
    ]
    df[num_cols] = df[num_cols].apply(pd.to_numeric, errors="coerce")
    df["report_date"] = report_date
    return df.reset_index(drop=True)


def fetch_latest_financials(
    today: dt.date | None = None, min_rows: int = 1000
) -> pd.DataFrame:
    """从最近报告期开始尝试，数据未披露（行数过少）则回退上一期。"""
    last_err: Exception | None = None
    for date in report_dates(today, n=4):
        try:
            df = fetch_financials(date)
        except Exception as e:  # 接口对未开始披露的报告期可能直接报错
            last_err = e
            continue
        if len(df) >= min_rows:
            return df
    raise RuntimeError(f"近 4 个报告期均无可用财务数据，最后错误: {last_err}")
