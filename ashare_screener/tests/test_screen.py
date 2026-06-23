import datetime as dt

import pandas as pd
import pytest

from screener.cache import Cache
from screener.fetch import _cagr_from, latest_annual_year, report_dates
from screener.screen import add_derived, apply_filters, screen_frames


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


def test_screen_frames_exclude_industries(spot, financials):
    # "金属" 子串应剔除"有色金属"类，但不误伤"银行"/"电池"
    financials = financials.copy()
    financials.loc[financials["code"] == "300750", "industry"] = "有色金属"
    config = {"exclude_st": False, "exclude_industries": ["金属", "白酒"], "filters": []}
    out = screen_frames(spot, financials, config)
    assert "300750" not in set(out["code"])  # 有色金属，剔除
    assert "600519" not in set(out["code"])  # 白酒，剔除
    assert "000001" in set(out["code"])      # 银行，保留


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


def test_cagr_from_two_positive_ends_only():
    end = pd.DataFrame({"code": ["A", "B", "C"], "net_profit": [200.0, 100.0, -5.0]})
    start = pd.DataFrame({"code": ["A", "B", "C"], "net_profit": [100.0, -50.0, 10.0]})
    out = _cagr_from(end, start, years=3).set_index("code")
    # A: (200/100)^(1/3)-1 ≈ 25.99%；B 起点为负、C 终点为负 -> 均剔除
    assert set(out.index) == {"A"}
    assert round(out.loc["A", "cagr_3y"], 2) == 25.99


def test_screen_frames_moat_filters_annual_fields():
    spot = pd.DataFrame(
        {"code": ["001", "002"], "name": ["甲", "乙"], "pe_ttm": [15.0, 15.0]}
    )
    fin = pd.DataFrame(
        {"code": ["001", "002"], "name": ["甲", "乙"], "report_date": ["20251231"] * 2}
    )
    # 甲年报 ROE 高、现金转化好；乙 ROE 低 -> 只甲过护城河门
    metrics = pd.DataFrame(
        {
            "code": ["001", "002"],
            "roe_annual": [25.0, 6.0],
            "cash_conv": [1.1, 1.1],
        }
    )
    config = {
        "exclude_st": False,
        "filters": [
            {"field": "roe_annual", "op": ">=", "value": 18},
            {"field": "cash_conv", "op": ">=", "value": 0.7},
        ],
        "output_fields": ["code", "roe_annual"],
    }
    out = screen_frames(spot, fin, config, metrics)
    assert set(out["code"]) == {"001"}


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
