"""状态机逻辑冒烟测试 — 合成的2024年6–9月市场路径（近似真实收盘价）。

⚠️ 这不是 SPEC §7 的正式回测（正式回测须用真实数据：
   python -m backtest.run_backtest --window 2024）。
本脚本仅在无外网环境下验证状态机逻辑：关键锚点（7月干预、7/31 BOJ加息、
8/2 抛售、8/5 崩盘）下，系统必须在 8/2 前到橙、8/5 前(含)到红。

用法：python -m backtest.smoke_logic_2024
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).parents[1]))
from src.signals import evaluate                          # noqa: E402
from src.state_machine import STAGE_NAMES, StateMachine   # noqa: E402

ROOT = Path(__file__).parents[1]


def _daily(anchors: dict[str, float], start: str, end: str) -> pd.Series:
    """锚点收盘价 → 工作日序列（锚点间线性插值）。"""
    s = pd.Series({pd.Timestamp(k): v for k, v in anchors.items()}).sort_index()
    idx = pd.bdate_range(start, end)
    return s.reindex(idx.union(s.index)).interpolate(method="time").reindex(idx).dropna()


def build_fixture() -> dict[str, pd.Series]:
    usdjpy = _daily({
        "2024-04-01": 151.6, "2024-04-29": 156.3, "2024-05-31": 157.3,
        "2024-06-14": 157.4, "2024-06-26": 160.8, "2024-06-28": 160.9,
        "2024-07-03": 161.70, "2024-07-05": 160.7, "2024-07-09": 161.3,
        "2024-07-10": 161.69, "2024-07-11": 158.8, "2024-07-12": 157.9,
        "2024-07-16": 158.3, "2024-07-17": 156.2, "2024-07-18": 157.4,
        "2024-07-19": 157.5, "2024-07-22": 157.0, "2024-07-23": 155.6,
        "2024-07-25": 153.9, "2024-07-29": 154.0, "2024-07-30": 152.8,
        "2024-07-31": 150.0, "2024-08-01": 149.4, "2024-08-02": 146.5,
        "2024-08-05": 144.2, "2024-08-06": 144.3, "2024-08-07": 146.7,
        "2024-08-15": 149.3, "2024-09-02": 146.9, "2024-09-30": 143.6,
    }, "2024-04-01", "2024-09-30")
    vix = _daily({
        "2024-04-01": 13.7, "2024-06-28": 12.4, "2024-07-19": 16.5,
        "2024-07-24": 18.0, "2024-07-31": 16.4, "2024-08-01": 18.6,
        "2024-08-02": 23.4, "2024-08-05": 38.6, "2024-08-06": 27.7,
        "2024-08-15": 15.2, "2024-09-30": 16.7,
    }, "2024-04-01", "2024-09-30")
    nikkei = _daily({
        "2024-04-01": 39803, "2024-07-11": 42224, "2024-07-25": 37870,
        "2024-08-01": 38126, "2024-08-02": 35910, "2024-08-05": 31458,
        "2024-08-06": 34675, "2024-09-30": 37920,
    }, "2024-04-01", "2024-09-30")
    nasdaq = _daily({
        "2024-04-01": 16400, "2024-07-10": 18647, "2024-08-01": 17194,
        "2024-08-02": 16776, "2024-08-05": 16200, "2024-08-06": 16367,
        "2024-09-30": 18189,
    }, "2024-04-01", "2024-09-30")
    # CFTC 周度净头寸（报告日=周二对齐，SPEC §9）：2年样本 + 2024极值与平仓
    rng = np.random.default_rng(7)
    tue = pd.date_range("2022-06-07", "2024-03-26", freq="W-TUE")
    base = pd.Series(-rng.uniform(60_000, 115_000, len(tue)), index=tue)
    ramp = pd.Series({
        "2024-04-02": -125_000, "2024-04-23": -179_000, "2024-05-07": -168_000,
        "2024-06-04": -156_000, "2024-06-25": -173_000, "2024-07-02": -184_000,
        "2024-07-09": -182_000, "2024-07-16": -151_000, "2024-07-23": -107_000,
        "2024-07-30": -73_000, "2024-08-06": -11_000, "2024-08-13": 23_000,
        "2024-09-24": 55_000,
    })
    ramp.index = pd.to_datetime(ramp.index)
    cftc = pd.concat([base, ramp]).sort_index()
    return {"usdjpy": usdjpy, "vix": vix, "nikkei": nikkei,
            "nasdaq": nasdaq, "cftc_jpy": cftc}


def main() -> int:
    cfg = yaml.safe_load(open(ROOT / "config" / "thresholds.yaml", encoding="utf-8"))
    data = build_fixture()
    days = [d for d in data["usdjpy"].index
            if pd.Timestamp("2024-06-01") <= d <= pd.Timestamp("2024-09-30")]
    sm_path = Path("/tmp/smoke_state.json")
    sm_path.unlink(missing_ok=True)
    sm = StateMachine(path=sm_path)

    print("=== 逻辑冒烟测试：合成2024路径 ===")
    first_at: dict[int, str] = {}
    for d in days:
        ev = evaluate(data, cfg, [], asof=d)
        tr = sm.step(ev, cfg["state_machine"]["downgrade_quiet_days"])
        if tr:
            arrow = "↑" if tr.kind == "upgrade" else "↓"
            print(f"  {tr.date} {arrow} {tr.label}: {'；'.join(tr.conditions) or '静默降级'}")
            if tr.kind == "upgrade":
                for stg in range(tr.from_stage + 1, tr.to_stage + 1):
                    first_at.setdefault(stg, tr.date)

    ok = True
    o, r = first_at.get(2), first_at.get(3)
    p1 = o is not None and o <= "2024-08-02"
    p2 = r is not None and r <= "2024-08-05"
    print(f"  验收[橙≤2024-08-02]: {'PASS' if p1 else 'FAIL'}（首橙 {o}，提前 "
          f"{(pd.Timestamp('2024-08-02') - pd.Timestamp(o)).days if o else '-'} 天）")
    print(f"  验收[红≤2024-08-05]: {'PASS' if p2 else 'FAIL'}（首红 {r}，提前 "
          f"{(pd.Timestamp('2024-08-05') - pd.Timestamp(r)).days if r else '-'} 天）")
    ok = p1 and p2
    print(f"  结果：{'✅ 逻辑通过（仍须真实数据回测后方可上线）' if ok else '❌ 不通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
