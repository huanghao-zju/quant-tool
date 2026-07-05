"""主流程：拉取 → 评估 → 状态机 → 推送（SPEC §5.2 推送时机）。

用法：
    python -m src.main             # 正常运行
    python -m src.main --dry-run   # 不真实推送
    python -m src.main --weekly    # 强制发周报（配合周六 cron）
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import yaml

from . import fetchers
from .fetchers.base import broken_sources
from .signals import evaluate
from .state_machine import STAGE_NAMES, StateMachine
from . import notify

ROOT = Path(__file__).parents[1]
DASH_FILE = Path("dashboard_score.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")


def load_yaml(name: str):
    with open(ROOT / "config" / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _due_reminders(cal: dict) -> list[dict]:
    """返回今日/明日到期的日历提醒（东京时区，对齐 JGB 拍卖）。"""
    import pandas as pd
    today = pd.Timestamp.now(tz="Asia/Tokyo").normalize().tz_localize(None)
    due = []
    for r in (cal.get("reminders") or []):
        try:
            d = pd.Timestamp(r["date"])
        except (KeyError, ValueError):
            continue
        if d == today:
            due.append({**r, "_when": "today"})
        elif d == today + pd.Timedelta(days=1):
            due.append({**r, "_when": "tomorrow"})
    return due


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--weekly", action="store_true")
    args = ap.parse_args()

    cfg = load_yaml("thresholds.yaml")
    actions = load_yaml("actions.yaml")
    events = (load_yaml("events.yaml") or {}).get("events") or []

    data = fetchers.fetch_all()
    if not data:
        log.error("全部数据源失败，本次运行中止")
        notify.push("**[数据源故障]** 本次运行所有数据源均拉取失败", dry_run=args.dry_run)
        return 1
    log.info("拉取成功指标：%s", sorted(data.keys()))

    ev = evaluate(data, cfg, events)
    sm = StateMachine()
    tr = sm.step(ev, cfg["state_machine"]["downgrade_quiet_days"])
    sm.save()

    pushed = False
    # (a) 阶段迁移
    if tr is not None:
        notify.push(notify.transition_msg(tr, ev, actions), dry_run=args.dry_run)
        pushed = True
    # (c) 慢变量看板分变动
    prev_score = json.loads(DASH_FILE.read_text())["score"] if DASH_FILE.exists() else None
    cur_score = sum(ev.dashboard.values())
    DASH_FILE.write_text(json.dumps({"score": cur_score}))
    if prev_score is not None and cur_score != prev_score and not pushed:
        hit = [k for k, v in ev.dashboard.items() if v]
        notify.push(f"**[看板分变动] {prev_score}→{cur_score}**\n命中：" +
                    ("、".join(hit) if hit else "无") + f"\n数据时间戳：{ev.date.date()}",
                    dry_run=args.dry_run)
        pushed = True
    # (b) 周报（心跳）
    broken = broken_sources(cfg["fetch"]["stale_alert_days"])
    if args.weekly:
        notify.push(notify.weekly_msg(ev, sm.stage, STAGE_NAMES[sm.stage], broken),
                    dry_run=args.dry_run)
        pushed = True
    # 数据源故障单独告警
    if broken and not args.weekly:
        notify.push(notify.fault_msg(broken), dry_run=args.dry_run)

    # 事件日历提醒（§2.3-17 拍卖 / §2.3-19 capex）：命中今日或明日的条目
    due = _due_reminders(load_yaml("calendar.yaml") or {})
    if due:
        notify.push(notify.reminder_msg(due), dry_run=args.dry_run)
        pushed = True

    log.info("阶段=%s(%d) 看板分=%s 迁移=%s 推送=%s",
             STAGE_NAMES[sm.stage], sm.stage, ev.dashboard_score,
             tr.label if tr else "无", pushed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
