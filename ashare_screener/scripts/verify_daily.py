"""复核脚本：真实命中数、限流影响、前 10 校验、分布统计。"""
from __future__ import annotations

import pandas as pd
import yaml
from pathlib import Path

from screener.cache import Cache, DEFAULT_DB
from screener.cli import screen_frames

cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8")) or {}
cache = Cache(DEFAULT_DB)
spot, fetched_at = cache.load_spot()
fin = cache.load_financials(cfg.get("report_date"))
metrics = cache.load_metrics()
history = cache.load_financials_history(
    ["revenue_yoy", "net_profit_yoy"], periods=int(cfg.get("stability_periods", 2))
)

# 1) 筛选池
pool = screen_frames(spot, fin, {"filters": [], "top": None,
                                 "max_per_industry": None,
                                 "exclude_st": cfg["exclude_st"]}, metrics, history)
# 2) 限流前真实命中
pre = screen_frames(spot, fin, {**cfg, "top": None, "max_per_industry": None}, metrics, history)
# 3) 限流后（不限 top）
post = screen_frames(spot, fin, {**cfg, "top": None}, metrics, history)
# 4) 最终输出
final = screen_frames(spot, fin, cfg, metrics, history)

print("POOL", len(pool))
print("PRE_LIMIT_MATCHED", len(pre))
print("POST_LIMIT_MATCHED", len(post))
print("FINAL_OUT", len(final))
print("INDUSTRY_PRE", pre["industry"].nunique())
print("INDUSTRY_POST", post["industry"].nunique())
print("INDUSTRY_FINAL", final["industry"].nunique())
top3_pre = pre["industry"].value_counts().head(3)
print("TOP3_PRE", dict(top3_pre), "share=%.3f" % (top3_pre.sum() / len(pre)))
top3_post = post["industry"].value_counts().head(3)
print("TOP3_POST", dict(top3_post), "share=%.3f" % (top3_post.sum() / len(post)))

# 被限流剔除的
kept_codes = set(post["code"])
dropped = pre[~pre["code"].isin(kept_codes)]
print("DROPPED_BY_CAP", len(dropped), dict(dropped["industry"].value_counts()))

# 前 10 校验
t10 = final.head(10).reset_index(drop=True)
for _, r in t10.iterrows():
    assert 0 < r["pegy"] < 0.5, (r["code"], r["pegy"])
    assert 15 <= r["cagr_3y"] <= 60, (r["code"], r["cagr_3y"])
    assert r["revenue_yoy"] >= 0, (r["code"], r["revenue_yoy"])
    assert r["roe_annual"] >= 12, (r["code"], r["roe_annual"])
    assert r["total_mcap"] >= 5.0e9, (r["code"], r["total_mcap"])
    assert r["pe_ttm"] > 0, (r["code"], r["pe_ttm"])
pegy_list = list(t10["pegy"])
assert pegy_list == sorted(pegy_list), "pegy 未升序"
print("ASSERT_TOP10_OK")

# 统计
for label, df in (("pre", pre), ("post", post), ("final", final)):
    print(f"STAT_{label} n={len(df)} pe_med={df['pe_ttm'].median():.2f} "
          f"pe_min={df['pe_ttm'].min():.2f} pe_max={df['pe_ttm'].max():.2f} "
          f"pegy_med={df['pegy'].median():.3f} pegy_min={df['pegy'].min():.3f} "
          f"pegy_max={df['pegy'].max():.3f} roe_med={df['roe_annual'].median():.2f} "
          f"roe_min={df['roe_annual'].min():.2f} roe_max={df['roe_annual'].max():.2f} "
          f"cagr_med={df['cagr_3y'].median():.2f} "
          f"div_nan={int(df['div_yield'].isna().sum())} "
          f"ind_n={df['industry'].nunique()}")

print("T10_INDUSTRY", dict(t10["industry"].value_counts()))
print("T10_PE_RANGE", round(t10["pe_ttm"].min(), 2), round(t10["pe_ttm"].max(), 2))
print("T10_PEGY_RANGE", round(t10["pegy"].min(), 3), round(t10["pegy"].max(), 3))
print("T10_ROE_RANGE", round(t10["roe_annual"].min(), 2), round(t10["roe_annual"].max(), 2))
print("FINAL_IND_COUNTS", dict(final["industry"].value_counts()))
print("FINAL_TOP3", dict(final["industry"].value_counts().head(3)),
      "share=%.3f" % (final["industry"].value_counts().head(3).sum() / len(final)))

# CSV 校验
csv = pd.read_csv("out_daily.csv", encoding="utf-8-sig", dtype={"code": str})
csv["code"] = csv["code"].str.zfill(6)
print("CSV_ROWS", len(csv), "CSV_COLS", list(csv.columns))
merged = final.reset_index(drop=True).merge(
    csv, on="code", how="inner", suffixes=("", "_csv"))
print("CSV_MATCH_ROWS", len(merged))
assert len(csv) == 50, len(csv)
assert len(merged) == 50, len(merged)
print("CSV_OK")
