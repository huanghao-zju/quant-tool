import datetime as dt

import pandas as pd
import pytest

from screener.cache import Cache
from screener.fetch import latest_annual_year, report_dates
from screener.screen import (
    add_derived,
    add_stability,
    apply_filters,
    cap_by_industry,
    screen_frames,
)


@pytest.fixture
def spot():
    return pd.DataFrame(
        {
            "code": ["600000", "000001", "300750", "839999", "600519"],
            "name": ["浦发银行", "平安银行", "宁德时代", "ST某某", "贵州茅台"],
            "price": [8.0, 12.0, 200.0, 3.0, 1500.0],
            "pe_ttm": [5.0, 6.0, 30.0, -8.0, 28.0],
            "pb": [0.5, 0.7, 4.0, 1.1, 8.0],
            "total_mcap": [2.3e11, 2.5e11, 9.0e11, 1.0e9, 1.9e12],
        }
    )


@pytest.fixture
def financials():
    return pd.DataFrame(
        {
            "code": ["600000", "000001", "300750", "839999", "600519"],
            "name": ["浦发银行", "平安银行", "宁德时代", "ST某某", "贵州茅台"],
            "roe": [9.0, 11.0, 22.0, 1.0, 32.0],
            "revenue_yoy": [-2.0, 6.0, 18.0, -30.0, 15.0],
            "industry": ["银行", "银行", "电池", "其他", "白酒"],
            "report_date": ["20260331"] * 5,
        }
    )


def test_apply_filters_basic(spot):
    out = apply_filters(spot, [{"field": "pe_ttm", "op": "between", "value": [0, 10]}])
    assert set(out["code"]) == {"600000", "000001"}


def test_apply_filters_nan_rows_dropped(spot):
    spot.loc[0, "pe_ttm"] = None
    out = apply_filters(spot, [{"field": "pe_ttm", "op": "<", "value": 100}])
    assert "600000" not in set(out["code"])


def test_apply_filters_unknown_field(spot):
    with pytest.raises(KeyError):
        apply_filters(spot, [{"field": "nope", "op": ">", "value": 1}])


def test_screen_frames_full_pipeline(spot, financials):
    config = {
        "exclude_st": True,
        "filters": [
            {"field": "roe", "op": ">=", "value": 10},
            {"field": "pe_ttm", "op": "between", "value": [0, 30]},
        ],
        "sort_by": "roe",
        "top": 2,
        "output_fields": ["code", "name", "roe", "pe_ttm"],
    }
    out = screen_frames(spot, financials, config)
    # ST 被剔除；按 roe 降序取前 2：茅台(32) > 宁德(22)
    assert list(out["code"]) == ["600519", "300750"]
    assert list(out.columns) == ["code", "name", "roe", "pe_ttm"]


def test_screen_frames_exclude_bse(spot, financials):
    config = {"exclude_st": False, "exclude_bse": True, "filters": []}
    out = screen_frames(spot, financials, config)
    assert "839999" not in set(out["code"])


def test_report_dates():
    dates = report_dates(dt.date(2026, 6, 12), n=4)
    assert dates == ["20260331", "20251231", "20250930", "20250630"]


def test_latest_annual_year():
    assert latest_annual_year(dt.date(2026, 6, 23)) == 2025  # 5 月后取上一年
    assert latest_annual_year(dt.date(2026, 3, 1)) == 2024   # 年报季前再往前一年


def test_add_derived_pegy_uses_cagr_and_dividend():
    df = pd.DataFrame(
        {
            "pe_ttm": [10.0, 10.0],
            "net_profit_yoy": [100.0, 100.0],  # 单期暴涨，应被 cagr 覆盖
            "cagr_3y": [20.0, None],           # 第二只缺 cagr -> 回退单期同比
            "div_yield": [5.0, None],          # 第二只缺股息 -> 按 0
        }
    )
    out = add_derived(df)
    assert out.loc[0, "peg"] == 10.0 / 100.0
    assert out.loc[0, "pegy"] == 10.0 / (20.0 + 5.0)   # 用 cagr + 股息
    assert out.loc[1, "pegy"] == 10.0 / (100.0 + 0.0)  # 回退单期，股息 0


def test_add_derived_pegy_rejects_loss_making():
    """亏损股（PE<0）且增速为负时，pegy 必须是 NaN 而不是漂亮的小正数。

    反例（真实数据）：万科A PE=-1.62、增速-25%，旧公式算出 pegy=0.064，
    会被 "pegy<0.5" 判成极低估好标的。全市场有 441 只这样蒙混过关。
    """
    df = pd.DataFrame(
        {
            "pe_ttm": [-1.62, 10.0, 10.0, -5.0],
            "net_profit_yoy": [-25.15, 40.0, -3.0, 30.0],
        }
    )
    out = add_derived(df)
    assert pd.isna(out.loc[0, "pegy"])   # 亏损 + 负增速
    assert out.loc[1, "pegy"] == pytest.approx(10.0 / 40.0)  # 正常盈利
    assert pd.isna(out.loc[2, "pegy"])   # 增速为负 -> 分母 <=0
    assert pd.isna(out.loc[3, "pegy"])   # PE<0 -> 亏损

    # 4 只里只有第 2 只（pegy=0.25）该通过 <0.5 和 >0
    assert len(apply_filters(out, [{"field": "pegy", "op": "<", "value": 0.5}])) == 1
    assert len(apply_filters(out, [{"field": "pegy", "op": ">", "value": 0}])) == 1


def test_screen_frames_excludes_loss_making_from_pegy_screen():
    """端到端：亏损且利润下滑的公司不能出现在 PEGY 选股结果里。"""
    spot = pd.DataFrame(
        {
            "code": ["000001", "000002"],
            "name": ["正常公司", "亏损公司"],
            "pe_ttm": [10.0, -1.62],
            "total_mcap": [1e10, 1e10],
        }
    )
    fin = pd.DataFrame(
        {
            "code": ["000001", "000002"],
            "name": ["正常公司", "亏损公司"],
            "net_profit_yoy": [20.0, -25.15],
            "cagr_3y": [30.0, -40.0],
            "div_yield": [2.0, None],
            "report_date": ["20260630"] * 2,
        }
    )
    # 正常公司 pegy = 10/(30+2) = 0.31 < 0.5；亏损公司双负 -> NaN
    out = screen_frames(
        spot, fin, {"exclude_st": False, "filters": [{"field": "pegy", "op": "<", "value": 0.5}]}
    )
    assert out["code"].tolist() == ["000001"]


def test_cap_by_industry_keeps_best_per_industry():
    """限流保留每个行业里排序最靠前的 cap 只。"""
    df = pd.DataFrame(
        {
            "code": ["a1", "a2", "a3", "b1", "b2", "c1"],
            "industry": ["有色", "有色", "有色", "银行", "银行", "白酒"],
        }
    )
    out = cap_by_industry(df, 2)
    assert out["code"].tolist() == ["a1", "a2", "b1", "b2", "c1"]


def test_cap_by_industry_disabled():
    df = pd.DataFrame({"code": ["a1", "a2"], "industry": ["有色", "有色"]})
    # 未配置 / 0 / None 都应原样返回
    for cap in (None, 0, -1):
        assert cap_by_industry(df, cap)["code"].tolist() == ["a1", "a2"]


def test_cap_by_industry_handles_missing_industry():
    """行业缺失归入「未知」一档，不影响其他行业取满。"""
    df = pd.DataFrame(
        {
            "code": ["a1", "a2", "x1", "x2", "x3"],
            "industry": ["有色", "有色", None, None, None],
        }
    )
    out = cap_by_industry(df, 2)
    assert out["code"].tolist() == ["a1", "a2", "x1", "x2"]


def test_screen_frames_caps_industry_after_sorting():
    """限流必须发生在排序之后，留下每行业最优的；且在 top 截断之前生效。"""
    spot = pd.DataFrame(
        {
            "code": [f"00000{i}" for i in range(6)],
            "name": [f"股{i}" for i in range(6)],
            # 三只有色、两只银行、一只白酒；pe 越小越好
            "pe_ttm": [1.0, 2.0, 3.0, 1.5, 2.5, 9.0],
        }
    )
    fin = pd.DataFrame(
        {
            "code": [f"00000{i}" for i in range(6)],
            "name": [f"股{i}" for i in range(6)],
            "industry": ["有色", "有色", "有色", "银行", "银行", "白酒"],
            "report_date": ["20260630"] * 6,
        }
    )
    cfg = {
        "exclude_st": False,
        "sort_by": "pe_ttm",
        "ascending": True,
        "max_per_industry": 2,
        "top": 50,
    }
    out = screen_frames(spot, fin, cfg)
    # 有色只留 pe 最低的两只（1.0、2.0），银行留两只
    assert out["code"].tolist() == ["000000", "000003", "000001", "000004", "000005"]
    assert out["pe_ttm"].tolist() == [1.0, 1.5, 2.0, 2.5, 9.0]


def test_screen_frames_industry_cap_applies_before_top():
    """限流要早于 top：否则被限流的行业会被 top 之前挤占名额。"""
    spot = pd.DataFrame(
        {
            "code": [f"{i:06d}" for i in range(5)],
            "name": [f"股{i}" for i in range(5)],
            "pe_ttm": [1.0, 1.1, 1.2, 1.3, 1.4],
        }
    )
    fin = pd.DataFrame(
        {
            "code": [f"{i:06d}" for i in range(5)],
            "name": [f"股{i}" for i in range(5)],
            "industry": ["有色"] * 3 + ["银行"] * 2,
            "report_date": ["20260630"] * 5,
        }
    )
    out = screen_frames(
        spot,
        fin,
        {
            "exclude_st": False,
            "sort_by": "pe_ttm",
            "ascending": True,
            "max_per_industry": 1,
            "top": 2,
        },
    )
    # 有色限 1 只、银行限 1 只，正好 2 只；若限流失效，top=2 会全是有色
    assert out["industry"].tolist() == ["有色", "银行"]


def test_add_derived_has_dividend_flag():
    """has_dividend 区分「不分红」与「股息率低」，供筛选直接用。"""
    df = pd.DataFrame(
        {
            "pe_ttm": [10.0, 10.0, 10.0],
            "net_profit_yoy": [20.0, 20.0, 20.0],
            "div_yield": [3.0, 0.5, None],  # 有分红 / 股息低 / 从未分红
        }
    )
    out = add_derived(df)
    assert out["has_dividend"].tolist() == [1, 1, 0]
    # 分红股用实际股息率，未分红股按 0
    assert out.loc[0, "pegy"] == pytest.approx(10.0 / 23.0)
    assert out.loc[1, "pegy"] == pytest.approx(10.0 / 20.5)
    assert out.loc[2, "pegy"] == pytest.approx(10.0 / 20.0)
    # 只有未分红的会被筛掉
    keep = apply_filters(out, [{"field": "has_dividend", "op": "==", "value": 1}])
    assert len(keep) == 2


def test_add_stability_requires_all_periods_positive():
    """连续正增长：所有对比期 yoy 都 > 0 才算 1，任一期为负或缺失都算 0。"""
    hist = pd.DataFrame(
        {
            "code": ["a", "b", "c", "d"],
            "revenue_yoy@20260630": [10.0, -5.0, 10.0, None],
            "revenue_yoy@20260331": [8.0, 20.0, None, 5.0],
            "net_profit_yoy@20260630": [3.0, 3.0, 3.0, 3.0],
            "net_profit_yoy@20260331": [2.0, 2.0, 2.0, 2.0],
        }
    )
    out = add_stability(hist)
    # a 两期都正 -> 1
    assert out.loc[out.code == "a", "revenue_stable"].iloc[0] == 1
    # b 有一期为负 -> 0；c 缺一期 -> 0；d 缺一期 -> 0
    assert out.loc[out.code == "b", "revenue_stable"].iloc[0] == 0
    assert out.loc[out.code == "c", "revenue_stable"].iloc[0] == 0
    assert out.loc[out.code == "d", "revenue_stable"].iloc[0] == 0
    assert out["profit_stable"].tolist() == [1, 1, 1, 1]


def test_add_stability_single_period_is_permissive():
    """只有一期可比时，等价于单期 yoy > 0，门槛不应过严。"""
    hist = pd.DataFrame(
        {
            "code": ["a", "b"],
            "revenue_yoy@20260630": [10.0, -5.0],
        }
    )
    out = add_stability(hist)
    assert out["revenue_stable"].tolist() == [1, 0]


def test_add_stability_empty_history():
    assert add_stability(pd.DataFrame()).empty


def test_cache_load_financials_history_pivots_periods(tmp_path):
    """多期宽表：列名带报告期后缀，且只保留各期都有的股票。"""
    cache = Cache(tmp_path / "h.db")
    for rd, vals in (
        ("20260331", {"code": ["000001", "000002"], "revenue_yoy": [10.0, 20.0]}),
        ("20260630", {"code": ["000001", "000002"], "revenue_yoy": [15.0, 25.0]}),
    ):
        cache.save_financials(pd.DataFrame({**vals, "report_date": [rd] * 2}))
    hist = cache.load_financials_history(["revenue_yoy"], periods=2)
    assert set(hist["revenue_yoy@20260331"]) == {10.0, 20.0}
    assert set(hist["revenue_yoy@20260630"]) == {15.0, 25.0}
    assert cache.available_report_dates() == ["20260630", "20260331"]


def test_cache_screen_history_roundtrip(tmp_path):
    """留痕写入后能读回，含入选时的价格与名次。"""
    cache = Cache(tmp_path / "h.db")
    result = pd.DataFrame(
        {
            "code": ["000001", "000002"],
            "name": ["甲", "乙"],
            "industry": ["有色", "银行"],
            "price": [10.0, 20.0],
            "total_mcap": [1e10, 2e10],
            "pe_ttm": [5.0, 6.0],
            "pegy": [0.2, 0.3],
        }
    )
    at = cache.save_screen_history(
        result, {"sort_by": "pegy"}, "20260630", "hash123", "{}", matched=2
    )
    hist = cache.list_history()
    assert len(hist) == 1 and hist["matched"].iloc[0] == 2
    picks = cache.load_picks(at)
    assert picks["code"].tolist() == ["000001", "000002"]
    assert picks["rank"].tolist() == [1, 2]
    assert picks["price"].tolist() == [10.0, 20.0]


def test_cache_screen_history_tolerates_missing_columns(tmp_path):
    """result 里没有 price/pegy 等列时不应崩，缺失存 NULL。"""
    cache = Cache(tmp_path / "h.db")
    result = pd.DataFrame({"code": ["000001"], "name": ["甲"]})
    at = cache.save_screen_history(result, {}, "20260630", "h", "{}")
    picks = cache.load_picks(at)
    assert picks["price"].isna().all()


def test_cache_prune_history(tmp_path):
    cache = Cache(tmp_path / "h.db")
    cache.save_screen_history(pd.DataFrame({"code": ["000001"]}), {}, None, "h", "{}")
    assert len(cache.list_history()) == 1
    cache.prune_history(keep_days=3650)  # 保留 10 年，不该删
    assert len(cache.list_history()) == 1
    cache.prune_history(keep_days=0)     # 全部删掉
    assert len(cache.list_history()) == 0


def test_screen_frames_filters_on_pegy():
    spot = pd.DataFrame(
        {
            "code": ["000001", "000002"],
            "name": ["甲", "乙"],
            "pe_ttm": [10.0, 40.0],
        }
    )
    fin = pd.DataFrame(
        {
            "code": ["000001", "000002"],
            "name": ["甲", "乙"],
            "net_profit_yoy": [30.0, 30.0],
            "report_date": ["20251231"] * 2,
        }
    )
    metrics = pd.DataFrame({"code": ["000001", "000002"], "cagr_3y": [30.0, 30.0]})
    config = {
        "exclude_st": False,
        "filters": [{"field": "pegy", "op": "<", "value": 0.5}],
        "output_fields": ["code", "pegy"],
    }
    out = screen_frames(spot, fin, config, metrics)
    # 甲 pegy=10/30≈0.33 命中；乙 pegy=40/30≈1.33 出局
    assert set(out["code"]) == {"000001"}


def test_cache_roundtrip(tmp_path, spot, financials):
    cache = Cache(tmp_path / "t.db")
    cache.save_spot(spot)
    cache.save_financials(financials)

    loaded_spot, fetched_at = cache.load_spot()
    assert fetched_at is not None
    assert len(loaded_spot) == len(spot)

    loaded_fin = cache.load_financials()
    assert len(loaded_fin) == len(financials)

    # 同一报告期重复保存应覆盖而不是追加
    cache.save_financials(financials)
    assert len(cache.load_financials()) == len(financials)
    cache.close()


def test_annualize_factor_by_report_period():
    from screener.screen import annualize_factor

    assert annualize_factor("20260331") == 4.0      # 一季报累计 3 个月
    assert annualize_factor("20260630") == 2.0      # 半年报累计 6 个月
    assert annualize_factor("20260930") == pytest.approx(4 / 3)
    assert annualize_factor("20251231") == 1.0      # 年报本身就是年化
    # 认不出来的一律按 1.0，不要把数据放大
    assert annualize_factor(None) == 1.0
    assert annualize_factor("garbage") == 1.0


def test_roe_annual_makes_threshold_comparable(spot):
    """同一门槛在不同报告期含义不同，roe_annual 把它拉回年度口径。"""
    from screener.screen import add_derived

    fin = pd.DataFrame(
        {
            "code": ["600000"],
            "roe": [4.0],  # 单季 4% ≈ 年化 16%
            "report_date": ["20260331"],
        }
    )
    q1 = add_derived(fin.copy())
    assert q1["roe_annual"].iloc[0] == pytest.approx(16.0)

    # 同样的 4%，如果是年报就只是 4%
    annual = add_derived(fin.assign(report_date=["20251231"]))
    assert annual["roe_annual"].iloc[0] == pytest.approx(4.0)

    # 年化门槛 12：一季报的 4% 过，年报的 4% 不过
    assert len(apply_filters(q1, [{"field": "roe_annual", "op": ">=", "value": 12}])) == 1
    assert len(apply_filters(annual, [{"field": "roe_annual", "op": ">=", "value": 12}])) == 0


def test_latest_published_report_uses_disclosure_deadlines():
    from screener.fetch import latest_published_report

    # 半年报截止 8/31：8/30 时最新仍是一季报，9/1 起才是半年报
    assert latest_published_report(dt.date(2026, 8, 30)) == "20260331"
    assert latest_published_report(dt.date(2026, 9, 1)) == "20260630"
    # 三季报截止 10/31
    assert latest_published_report(dt.date(2026, 10, 30)) == "20260630"
    assert latest_published_report(dt.date(2026, 11, 1)) == "20260930"
    # 年报与次年一季报同为 4/30 截止
    assert latest_published_report(dt.date(2026, 4, 29)) == "20250930"
    assert latest_published_report(dt.date(2026, 5, 1)) == "20260331"
