"""历史窗口回放（SPEC §7）。部署前验收门槛。

用法：python -m backtest.run_backtest --window 2024
输出：每次阶段迁移的日期、触发条件、相对事件锚点的提前/滞后天数。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).parents[1]))
from src import fetchers                      # noqa: E402
from src.fetchers import fred, yf, jgb, cftc  # noqa: E402
from src.signals import evaluate              # noqa: E402
from src.state_machine import STAGE_NAMES, StateMachine  # noqa: E402

ROOT = Path(__file__).parents[1]
CACHE = Path(__file__).parent / "data"

# 窗口定义：replay 区间、数据预热起点、事件锚点与验收标准
WINDOWS = {
    "2024": {
        "replay": ("2024-06-01", "2024-09-30"),
        "warmup": "2024-01-01",
        "cftc_start": "2022-06-01",           # 2年分位样本
        "desc": "日元套息平仓（真阳性）",
        "anchors": {"orange_by": "2024-08-02", "red_by": "2024-08-05"},
    },
    "2026-iran": {
        "replay": ("2026-02-01", "2026-04-30"),
        "warmup": "2025-09-01",
        "cftc_start": "2024-02-01",
        "desc": "伊朗冲击（通道区分）",
        "anchors": {"max_stage": 2},
    },
    "2025-quiet": {
        "replay": ("2025-07-01", "2025-12-31"),
        "warmup": "2025-02-01",
        "cftc_start": "2023-07-01",
        "desc": "平静期（假阳性）",
        "anchors": {"max_upgrades_above_0": 2},
    },
}


def _cached(key: str, loader) -> pd.Series | None:
    CACHE.mkdir(exist_ok=True)
    f = CACHE / f"{key}.csv"
    if f.exists():
        df = pd.read_csv(f, index_col=0, parse_dates=True)
        return df.iloc[:, 0]
    s = loader()
    if s is not None and not s.empty:
        s.to_csv(f)
    return s


def load_data(w: dict) -> dict[str, pd.Series]:
    warmup, end = w["warmup"], w["replay"][1]
    tag = w["replay"][0][:4]
    data: dict[str, pd.Series] = {}
    for key, tk in fetchers.YF_TICKERS.items():
        s = _cached(f"{tag}_{key}", lambda tk=tk: yf.fetch(tk, start=warmup, end=end))
        if s is not None:
            data[key] = s
    for key, sid in fetchers.FRED_SERIES.items():
        s = _cached(f"{tag}_{key}", lambda sid=sid: fred.fetch(sid, start=warmup, end=end))
        if s is not None:
            data[key] = s
    if "vix" not in data:
        s = _cached(f"{tag}_vix", lambda: yf.fetch("^VIX", start=warmup, end=end))
        if s is not None:
            data["vix"] = s
    for tenor, key in (("10", "jgb10"), ("30", "jgb30")):
        s = _cached(f"{tag}_{key}", lambda t=tenor: jgb.fetch(t, full_history=True))
        if s is not None:
            data[key] = s.loc[warmup:end]
    s = _cached(f"{tag}_cftc", lambda: cftc.fetch(start=w["cftc_start"], end=end))
    if s is not None:
        data["cftc_jpy"] = s
    return data


def run(window: str) -> bool:
    w = WINDOWS[window]
    cfg = yaml.safe_load(open(ROOT / "config" / "thresholds.yaml", encoding="utf-8"))
    events: list = []  # 按 SPEC §7 可补录当期公开新闻事件；2024 窗口暂不依赖
    data = load_data(w)
    print(f"\n=== 回测窗口 {window}：{w['desc']} ===")
    print(f"数据覆盖：{sorted(data.keys())}")

    start, end = map(pd.Timestamp, w["replay"])
    base = data.get("usdjpy")
    if base is None:
        print("FAIL: 缺少 USDJPY 主序列"); return False
    days = [d for d in base.index if start <= d <= end]

    sm = StateMachine(path=Path(f"/tmp/backtest_state_{window}.json"))
    if sm.path.exists():
        sm.path.unlink()
    sm = StateMachine(path=sm.path)
    transitions = []
    for d in days:
        ev = evaluate(data, cfg, events, asof=d)
        tr = sm.step(ev, cfg["state_machine"]["downgrade_quiet_days"])
        if tr:
            transitions.append(tr)
            arrow = "↑" if tr.kind == "upgrade" else "↓"
            print(f"  {tr.date} {arrow} {tr.label}: {'；'.join(tr.conditions) or '静默降级'}")

    return _verify(window, w, transitions)


def _verify(window: str, w: dict, transitions: list) -> bool:
    a = w["anchors"]
    ok = True
    first_at = {}
    for tr in transitions:
        if tr.kind == "upgrade" and tr.to_stage not in first_at:
            for stg in range(tr.from_stage + 1, tr.to_stage + 1):
                first_at.setdefault(stg, tr.date)
    if "orange_by" in a:
        got = first_at.get(2)
        lead = (pd.Timestamp(a["orange_by"]) - pd.Timestamp(got)).days if got else None
        passed = got is not None and got <= a["orange_by"]
        ok &= passed
        print(f"  验收[橙≤{a['orange_by']}]: {'PASS' if passed else 'FAIL'} "
              f"（首橙 {got or '未触发'}，提前 {lead} 天）")
    if "red_by" in a:
        got = first_at.get(3)
        lead = (pd.Timestamp(a["red_by"]) - pd.Timestamp(got)).days if got else None
        passed = got is not None and got <= a["red_by"]
        ok &= passed
        print(f"  验收[红≤{a['red_by']}]: {'PASS' if passed else 'FAIL'} "
              f"（首红 {got or '未触发'}，提前 {lead} 天）")
    if "max_stage" in a:
        top = max((t.to_stage for t in transitions if t.kind == "upgrade"), default=0)
        passed = top < 3
        ok &= passed
        print(f"  验收[日元状态机不得升至红色]: {'PASS' if passed else 'FAIL'}（最高 {STAGE_NAMES[top]}）")
    if "max_upgrades_above_0" in a:
        n = sum(1 for t in transitions if t.kind == "upgrade")
        passed = n <= a["max_upgrades_above_0"]
        ok &= passed
        print(f"  验收[升级次数≤{a['max_upgrades_above_0']}]: {'PASS' if passed else 'FAIL'}（{n} 次）")
    print(f"  结果：{'✅ 通过' if ok else '❌ 不通过（调参重跑，不得带病上线）'}")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", default="2024", choices=list(WINDOWS))
    args = ap.parse_args()
    sys.exit(0 if run(args.window) else 1)
