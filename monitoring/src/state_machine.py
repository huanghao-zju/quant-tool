"""阶段状态机（SPEC §4）。持久化 state.json。

规则：
- 允许跳级：任一更高阶段条件直接满足 → 直接迁移；
- 降级：连续 downgrade_quiet_days 个交易日无当前阶段及以上条件 → 降一级；
- 慢变量看板不参与分级（HY OAS>400 例外，已作为红色条件进入 evaluate）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .signals import Evaluation

STAGE_NAMES = {0: "绿色", 1: "黄色", 2: "橙色", 3: "红色"}
STATE_FILE = Path("state.json")


@dataclass
class Transition:
    date: str
    from_stage: int
    to_stage: int
    kind: str                    # "upgrade" | "downgrade"
    conditions: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{STAGE_NAMES[self.from_stage]}→{STAGE_NAMES[self.to_stage]}"


class StateMachine:
    def __init__(self, path: Path = STATE_FILE):
        self.path = path
        if path.exists():
            st = json.loads(path.read_text())
        else:
            st = {}
        self.stage: int = st.get("stage", 0)
        self.entered: str | None = st.get("entered")
        self.quiet_days: int = st.get("quiet_days", 0)
        self.snapshot: list[str] = st.get("snapshot", [])
        self.last_eval_date: str | None = st.get("last_eval_date")
        self.downgrade_quiet_days: int = 10  # 由 step() 传入覆盖

    def save(self) -> None:
        self.path.write_text(json.dumps({
            "stage": self.stage, "entered": self.entered,
            "quiet_days": self.quiet_days, "snapshot": self.snapshot,
            "last_eval_date": self.last_eval_date,
        }, ensure_ascii=False, indent=1))

    def step(self, ev: Evaluation, quiet_days_limit: int) -> Transition | None:
        """输入一天的评估结果，返回迁移（若发生）。每个交易日调用一次。"""
        date = str(ev.date.date())
        if self.last_eval_date == date:
            return None  # 同日重复运行，幂等
        self.last_eval_date = date

        target = ev.highest_stage
        if target > self.stage:  # 升级/跳级
            tr = Transition(date, self.stage, target, "upgrade",
                            ev.conditions_at_or_above(target))
            self.stage, self.entered = target, date
            self.quiet_days, self.snapshot = 0, tr.conditions
            return tr

        if self.stage > 0:  # 降级计数
            if ev.conditions_at_or_above(self.stage):
                self.quiet_days = 0
            else:
                self.quiet_days += 1
                if self.quiet_days >= quiet_days_limit:
                    tr = Transition(date, self.stage, self.stage - 1, "downgrade")
                    self.stage -= 1
                    self.entered, self.quiet_days = date, 0
                    return tr
        return None
