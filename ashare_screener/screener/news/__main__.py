"""彭博简报命令行入口。

    python -m screener.news                  # 按当前时间自动判断该推早报还是晚报
    python -m screener.news --slot morning   # 强制指定时段
    python -m screener.news --preview        # 只在终端看筛选结果，不推送
    python -m screener.news --dry-run        # 打印卡片 JSON，不推送
    python -m screener.news --force          # 忽略时间窗口和「已推送」标记
    python -m screener.news --test-webhook   # 发一条测试消息验证 webhook

设计要点：launchd 每 20 分钟唤醒一次，落在窗口内且当日该时段未成功推送时才真正执行。
这样 VPN 没在 8:30 打开、9:10 才连上，也能补推；已推过则直接退出，不会刷屏。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Dict, Optional, Tuple
from zoneinfo import ZoneInfo

import yaml

from .bloomberg import BloombergError, fetch_news
from .feishu import FeishuError, build_card, send, send_digest, send_text
from .models import Digest
from .rank import build_digest
from .store import Store

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"

DEFAULT_WINDOWS = {
    "morning": ("08:30", "11:00"),
    "evening": ("20:30", "23:00"),
}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%F %T')}] {msg}", flush=True)


def _parse_hhmm(text: str) -> dtime:
    hh, mm = text.strip().split(":")
    return dtime(int(hh), int(mm))


def resolve_tz(name: Optional[str]):
    """把配置里的时区名解析成 tzinfo。空值表示跟随宿主机。

    部署在境外 VPS 上时必须显式配 Asia/Shanghai —— VPS 普遍是 UTC，
    否则「08:30 早报」的窗口判断会整体偏 8 小时，且不会报错，只是永远不触发。
    """
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception as exc:
        raise SystemExit(f"时区 {name!r} 无法解析（{exc}），检查系统是否装了 tzdata") from exc


def load_config(path: Path) -> Dict:
    if not path.exists():
        raise SystemExit(f"配置文件不存在：{path}")
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    cfg = raw.get("news") or {}
    # 补齐默认值，配置文件里只写想改的项即可
    cfg.setdefault("feishu", {})
    cfg.setdefault("proxy", None)
    cfg.setdefault("timezone", "Asia/Shanghai")
    cfg.setdefault("china_slots", 6)
    cfg.setdefault("headline_slots", 4)
    cfg.setdefault("max_age_hours", 30)
    cfg.setdefault("db", "data/news.db")
    cfg.setdefault("windows", DEFAULT_WINDOWS)
    return cfg


def resolve_slot(now: datetime, windows: Dict[str, Tuple[str, str]]) -> Optional[str]:
    """判断当前时刻落在哪个推送窗口内。

    不支持跨零点的窗口（如 23:30-01:00）。真要那种排班，改 launchd 的触发时间更直接。
    """
    cur = now.time()
    for name, (start, end) in windows.items():
        if _parse_hhmm(start) <= cur <= _parse_hhmm(end):
            return name
    return None


def _near_window_end(now: datetime, slot: str, windows: Dict[str, Tuple[str, str]],
                     margin_min: int = 40) -> bool:
    """是否已接近窗口末尾 —— 到了这一步还没推成功，就该告警了。"""
    _, end = windows[slot]
    end_dt = now.replace(
        hour=_parse_hhmm(end).hour, minute=_parse_hhmm(end).minute, second=0, microsecond=0
    )
    return 0 <= (end_dt - now).total_seconds() <= margin_min * 60


def cmd_preview(china, headline) -> None:
    def dump(title, items):
        print(f"\n=== {title}（{len(items)} 条）===")
        for i, it in enumerate(items, 1):
            flag = f"  << {','.join(it.hits[:3])}" if it.hits else ""
            print(f"{i:>2}. [{it.score:>5.2f}] {it.published_str}  {it.title}{flag}")

    dump("中国市场", china)
    dump("全球头条", headline)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m screener.news", description="彭博社新闻简报")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--slot", choices=["morning", "evening"], help="强制指定时段")
    parser.add_argument("--preview", action="store_true", help="只打印筛选结果，不推送")
    parser.add_argument("--dry-run", action="store_true", help="打印卡片 JSON，不推送")
    parser.add_argument("--force", action="store_true", help="忽略窗口与已推送标记")
    parser.add_argument("--test-webhook", action="store_true", help="发测试消息")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    webhook = (cfg["feishu"].get("webhook") or "").strip()
    secret = (cfg["feishu"].get("secret") or "").strip() or None
    db_path = Path(cfg["db"])
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    store = Store(db_path)
    tz = resolve_tz(cfg.get("timezone"))
    now = datetime.now(tz)
    windows = {k: tuple(v) for k, v in cfg["windows"].items()}

    # ---- 发测试消息
    if args.test_webhook:
        try:
            send_text(webhook, f"彭博简报 webhook 连通测试 {now:%Y-%m-%d %H:%M:%S}", secret=secret)
        except FeishuError as exc:
            log(f"测试失败：{exc}")
            return 1
        log("测试消息已发出，去飞书群里看一眼")
        return 0

    # ---- 决定时段
    slot = args.slot or resolve_slot(now, windows)
    if not slot:
        # preview / dry-run 只是本地看结果，不受时间窗口约束
        if args.preview or args.dry_run:
            slot = "morning"
        elif not args.force:
            log(f"当前 {now:%H:%M} 不在推送窗口内，跳过")
            return 0
        else:
            slot = "morning"
            log("不在窗口内，但 --force 生效，按时段 morning 执行")

    slot_key = f"{now:%Y-%m-%d}:{slot}"
    if store.slot_done(slot_key) and not args.force:
        log(f"{slot_key} 已推送过，跳过")
        return 0

    # ---- 抓取
    try:
        items = fetch_news(max_age_hours=cfg["max_age_hours"], proxy=cfg["proxy"], tz=tz)
    except BloombergError as exc:
        log(f"抓取失败（{exc.kind}）：{exc}")
        # 窗口快结束了还没成功，才值得打扰用户
        alert_key = f"{now:%Y-%m-%d}:{slot}:alert"
        if _near_window_end(now, slot, windows) and not store.slot_done(alert_key):
            hint = (
                "VPN 未连接，或彭博出口 IP 被限。连上 VPN 后窗口内会自动重试。"
                if exc.kind in ("unreachable", "blocked")
                else str(exc)
            )
            try:
                send(webhook, {
                    "msg_type": "interactive",
                    "card": {
                        "config": {"wide_screen_mode": True},
                        "header": {
                            "template": "red",
                            "title": {"tag": "plain_text", "content": "彭博简报抓取失败"},
                        },
                        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": hint}}],
                    },
                }, secret=secret)
                store.record_push(alert_key, [], [])
                log("已发出告警")
            except FeishuError as fexc:
                log(f"告警也发不出去：{fexc}")
        return 1

    log(f"抓到 {len(items)} 条 {cfg['max_age_hours']} 小时内的新闻")

    fresh = store.drop_pushed(items)
    log(f"去重后剩余 {len(fresh)} 条未推送")

    china, headline = build_digest(
        fresh,
        now=now,
        china_slots=cfg["china_slots"],
        headline_slots=cfg["headline_slots"],
        max_age_hours=cfg["max_age_hours"],
    )

    digest = Digest(slot=slot, generated=now, china=china, headline=headline)

    if args.preview:
        cmd_preview(china, headline)
        return 0

    if digest.is_empty:
        log("没有新的可用新闻，标记该时段为已完成，避免反复重试")
        store.record_push(slot_key, [], [])
        return 0

    payload = build_card(digest)

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    try:
        send_digest(webhook, digest, secret=secret)
    except FeishuError as exc:
        log(f"推送失败：{exc}")
        return 1

    store.record_push(slot_key, china, headline)
    log(f"已推送 {slot_key}：中国市场 {len(china)} 条 / 全球头条 {len(headline)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
