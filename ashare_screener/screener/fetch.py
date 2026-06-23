"""基于 AkShare 的全市场批量拉取。

只使用"一次请求返回全市场"的批量接口，避免逐股发 5000+ 次请求触发风控：
- 行情快照: ak.stock_zh_a_spot_em()          沪深京全部 A 股，含 PE/PB/市值
  备用: datacenter-web.eastmoney.com          push2 被 TLS 指纹拦截时自动切换
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
    "所处行业": "industry",
    "最新公告日期": "announce_date",
}

DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

DATACENTER_SPOT_COLUMNS = {
    "SECURITY_CODE": "code",
    "SECURITY_NAME_ABBR": "name",
    "CLOSE_PRICE": "price",
    "CHANGE_RATE": "pct_change",
    "TURNOVERRATE": "turnover_rate",
    "PE_DYNAMIC": "pe_ttm",
}

# RPT_VALUEANALYSIS_DET 列名 -> 标准字段（备用源补市值/PB 用，按交易日）
DATACENTER_VALUATION_COLUMNS = {
    "SECURITY_CODE": "code",
    "TOTAL_MARKET_CAP": "total_mcap",
    "NOTLIMITED_MARKETCAP_A": "float_mcap",
    "PB_MRQ": "pb",
}

# 估值报表补齐市值/PB 后，备用源仍缺这几个 push2 独有字段
DATACENTER_SPOT_MISSING = ["amount", "volume_ratio", "pct_60d", "pct_ytd"]

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


def _fetch_spot_akshare() -> pd.DataFrame:
    import akshare as ak

    df = _rename(ak.stock_zh_a_spot_em(), SPOT_COLUMNS)
    df["code"] = df["code"].astype(str).str.zfill(6)
    num_cols = [c for c in df.columns if c not in ("code", "name")]
    df[num_cols] = df[num_cols].apply(pd.to_numeric, errors="coerce")
    return df.dropna(subset=["price"]).reset_index(drop=True)


def _datacenter_pull(report: str, columns: str, extra: dict | None = None) -> pd.DataFrame:
    """datacenter-web 通用分页拉取，返回原始英文列 DataFrame。"""
    import requests as req

    rows: list[dict] = []
    page = 1
    while True:
        params = {
            "reportName": report,
            "columns": columns,
            "pageSize": "5000",
            "pageNumber": str(page),
            "source": "WEB",
            "client": "WEB",
        }
        if extra:
            params.update(extra)
        r = req.get(DATACENTER_URL, params=params, timeout=40)
        r.raise_for_status()
        result = r.json().get("result") or {}
        data = result.get("data")
        if not data:
            break
        rows.extend(data)
        if len(rows) >= result.get("count", 0):
            break
        page += 1
    return pd.DataFrame(rows)


def _latest_valuation_date(today: dt.date | None = None) -> str:
    """估值报表最近有数据的交易日 YYYY-MM-DD（从今天往回探最多 10 天）。"""
    import requests as req

    today = today or dt.date.today()
    for back in range(10):
        day = (today - dt.timedelta(days=back)).strftime("%Y-%m-%d")
        r = req.get(
            DATACENTER_URL,
            params={
                "reportName": "RPT_VALUEANALYSIS_DET",
                "columns": "SECURITY_CODE",
                "filter": f"(TRADE_DATE='{day}')",
                "pageSize": "1",
                "pageNumber": "1",
                "source": "WEB",
                "client": "WEB",
            },
            timeout=20,
        )
        if (r.json().get("result") or {}).get("data"):
            return day
    raise RuntimeError("datacenter 估值报表近 10 日均无数据")


def _fetch_valuation_datacenter(today: dt.date | None = None) -> pd.DataFrame:
    """估值报表里的市值/PB（备用源补 push2 缺失字段用）。"""
    day = _latest_valuation_date(today)
    df = _datacenter_pull(
        "RPT_VALUEANALYSIS_DET",
        ",".join(DATACENTER_VALUATION_COLUMNS),
        {"filter": f"(TRADE_DATE='{day}')"},
    ).rename(columns=DATACENTER_VALUATION_COLUMNS)
    df["code"] = df["code"].astype(str).str.zfill(6)
    for c in ("total_mcap", "float_mcap", "pb"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.drop_duplicates("code").reset_index(drop=True)


def _fetch_spot_datacenter() -> pd.DataFrame:
    df = _datacenter_pull(
        "RPT_DMSK_TS_STOCKNEW",
        ",".join(DATACENTER_SPOT_COLUMNS),
        {"sortColumns": "SECURITY_CODE", "sortTypes": "1"},
    ).rename(columns=DATACENTER_SPOT_COLUMNS)
    df["code"] = df["code"].astype(str).str.zfill(6)
    num_cols = [c for c in df.columns if c not in ("code", "name")]
    df[num_cols] = df[num_cols].apply(pd.to_numeric, errors="coerce")
    # 市值/PB 从估值报表补齐，其余 push2 独有字段置空
    df = df.merge(_fetch_valuation_datacenter(), on="code", how="left")
    for col in DATACENTER_SPOT_MISSING:
        df[col] = pd.NA
    return df.dropna(subset=["price"]).reset_index(drop=True)


def fetch_spot() -> pd.DataFrame:
    """全市场实时行情快照，一次请求返回全部 5000+ 只。

    优先走 AkShare（push2），若被 TLS 指纹拦截则自动切换到
    datacenter-web 备用接口（字段较少，缺少市值/PB 等）。
    """
    try:
        return _fetch_spot_akshare()
    except Exception:
        print(
            "AkShare 行情接口(push2)不可用，切换到备用接口(datacenter-web)…\n"
            f"注意：备用接口缺少字段 {DATACENTER_SPOT_MISSING}（市值/PB 已从估值报表补齐），"
            "用到缺失字段的筛选条件请从 config.yaml 中移除"
        )
        return _fetch_spot_datacenter()


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


def latest_annual_year(today: dt.date | None = None) -> int:
    """最近已基本披露完毕的年报年份。

    年报最晚次年 4 月底披露完，故 5 月起取上一年，否则再往前一年。
    """
    today = today or dt.date.today()
    return today.year - 1 if today.month >= 5 else today.year - 2


def fetch_dividends(today: dt.date | None = None) -> pd.DataFrame:
    """最近年报的全市场股息率（%），无分红的不在结果里。

    取每只股最新公告的那笔分红方案（同股多笔时按公告日取最新）。
    """
    year = latest_annual_year(today)
    df = _datacenter_pull(
        "RPT_SHAREBONUS_DET",
        "SECURITY_CODE,DIVIDENT_RATIO,PLAN_NOTICE_DATE",
        {
            "filter": f"(REPORT_DATE='{year}-12-31')",
            "sortColumns": "PLAN_NOTICE_DATE",
            "sortTypes": "-1",
        },
    )
    if df.empty:
        return pd.DataFrame(columns=["code", "div_yield"])
    df = df.rename(columns={"SECURITY_CODE": "code", "DIVIDENT_RATIO": "div_yield"})
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["div_yield"] = pd.to_numeric(df["div_yield"], errors="coerce") * 100  # -> %
    df = df.dropna(subset=["div_yield"]).drop_duplicates("code", keep="first")
    return df[["code", "div_yield"]].reset_index(drop=True)


def _annual_financials(year: int, cols: list[str] | None = None) -> pd.DataFrame:
    """某年年报，按 code 去重；cols 指定则只取这些列。"""
    df = fetch_financials(f"{year}1231")
    if cols:
        df = df[cols]
    return df.drop_duplicates("code").reset_index(drop=True)


def _cagr_from(end_np: pd.DataFrame, start_np: pd.DataFrame, years: int) -> pd.DataFrame:
    """由两期年报净利润算复合增速（%），两端均为正才计算。"""
    m = end_np.rename(columns={"net_profit": "np_end"}).merge(
        start_np.rename(columns={"net_profit": "np_start"}), on="code"
    )
    m = m[(m["np_end"] > 0) & (m["np_start"] > 0)]
    m["cagr_3y"] = ((m["np_end"] / m["np_start"]) ** (1 / years) - 1) * 100
    return m[["code", "cagr_3y"]].reset_index(drop=True)


def fetch_cagr(today: dt.date | None = None, years: int = 3) -> pd.DataFrame:
    """近 `years` 年净利润复合增速（%），基于年报净利润。

    用最近年报与其前 `years` 年的年报，两端净利均为正才计算（亏损年
    CAGR 无意义）。返回列名固定为 cagr_3y（默认 years=3）。
    """
    end_year = latest_annual_year(today)
    end = _annual_financials(end_year, ["code", "net_profit"])
    start = _annual_financials(end_year - years, ["code", "net_profit"])
    for d in (end, start):
        d["net_profit"] = pd.to_numeric(d["net_profit"], errors="coerce")
    return _cagr_from(end, start, years)


def fetch_metrics(today: dt.date | None = None, cagr_years: int = 3) -> pd.DataFrame:
    """按 code 的衍生指标表：CAGR + 年报口径护城河质量 + 股息率。

    护城河指标用**年报**口径（roe_annual/gross_margin_annual/cash_conv），
    而非缓存里 financials 的季度累计口径——季度 ROE 约为年化的 1/4，直接
    拿来做质量门槛会严重误判。年报数据复用 CAGR 的拉取，不额外发请求。
    """
    end_year = latest_annual_year(today)
    ann = _annual_financials(
        end_year, ["code", "net_profit", "roe", "gross_margin", "eps", "ocf_per_share"]
    )
    for c in ("net_profit", "roe", "gross_margin", "eps", "ocf_per_share"):
        ann[c] = pd.to_numeric(ann[c], errors="coerce")
    start = _annual_financials(end_year - cagr_years, ["code", "net_profit"])
    start["net_profit"] = pd.to_numeric(start["net_profit"], errors="coerce")

    cagr = _cagr_from(ann[["code", "net_profit"]], start, cagr_years)
    quality = ann[["code", "roe", "gross_margin", "eps", "ocf_per_share"]].copy()
    quality["cash_conv"] = quality["ocf_per_share"] / quality["eps"]  # 现金转化率
    quality = quality.rename(
        columns={"roe": "roe_annual", "gross_margin": "gross_margin_annual"}
    )[["code", "roe_annual", "gross_margin_annual", "cash_conv"]]
    div = fetch_dividends(today)

    return cagr.merge(quality, on="code", how="outer").merge(div, on="code", how="outer")
