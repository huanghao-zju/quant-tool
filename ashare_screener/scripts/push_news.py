#!/usr/bin/env python3
"""抓彭博新闻 -> 分中国市场/全球头条 -> 飞书推送。

用法：
    /usr/local/bin/python3 scripts/push_news.py --dry-run     # 只预览，不推送、不记去重
    /usr/local/bin/python3 scripts/push_news.py               # 真推送
    /usr/local/bin/python3 scripts/push_news.py --slot 晚间 --hours 12

定时由 launchd 调起，见 scripts/com.huanghao.bloomberg-digest.plist。
"""
from __future__ import annotations

import argparse
import html
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from news.bloomberg import fetch_news  # noqa: E402
from news.classify import rank  # noqa: E402
from news.feishu import build_card, send  # noqa: E402
from news.store import filter_new, mark_pushed  # noqa: E402
from news.translate import translate_titles  # noqa: E402

CST = timezone(timedelta(hours=8))

DEFAULTS = {
    "proxy": None,
    "timeout": 60,
    "lookback_hours": 14,
    "china_limit": 12,
    "headline_limit": 8,
    "translate": True,
    "feishu": {"webhook": "", "secret": ""},
}


def load_config(path: Path) -> dict:
    if not path.exists():
        logging.warning("配置文件不存在：%s，使用默认值", path)
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = dict(DEFAULTS)
    cfg.update(raw.get("news") or {})
    if not isinstance(cfg.get("feishu"), dict):
        cfg["feishu"] = DEFAULTS["feishu"]
    return cfg


def guess_slot(now: datetime) -> str:
    return "早间" if now.hour < 13 else "晚间"


def render_html(china: list, headline: list, *, slot: str, now: datetime) -> str:
    """本地预览页。用来自查排版和选稿质量，不对外发布。"""

    def block(title: str, items: list, accent: str) -> str:
        if not items:
            return ""
        rows = []
        for idx, item in enumerate(items, 1):
            zh = html.escape(item.title_zh) if item.title_zh else ""
            en = html.escape(item.title)
            primary, secondary = (zh, en) if zh and zh != en else (en, "")
            sub = f'<div class="en">{secondary}</div>' if secondary else ""
            rows.append(
                f'<li><a href="{html.escape(item.url)}" target="_blank">'
                f'<span class="idx">{idx}</span>'
                f'<span class="body"><span class="zh">{primary}</span>{sub}'
                f'<span class="meta">{item.local_time()} · 命中：'
                f'{html.escape(", ".join(item.matched[:4]))}</span></span></a></li>'
            )
        return (
            f'<section><h2 style="border-color:{accent}">{html.escape(title)}'
            f'<span class="count">{len(items)}</span></h2>'
            f'<ol>{"".join(rows)}</ol></section>'
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>彭博社简报 · {now.strftime('%m-%d')} {slot}</title>
<style>
  :root {{ --red:#c0392b; --ink:#1a1a1a; --muted:#8a8a8a; --line:#ebebeb; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:40px 20px 60px; background:#faf8f6;
         font:16px/1.6 -apple-system,"PingFang SC","Helvetica Neue",sans-serif; color:var(--ink); }}
  .wrap {{ max-width:760px; margin:0 auto; }}
  header {{ border-bottom:3px solid var(--red); padding-bottom:18px; margin-bottom:34px; }}
  h1 {{ margin:0 0 8px; font-size:30px; letter-spacing:-.5px; }}
  .when {{ color:var(--muted); font-size:14px; }}
  section {{ margin-bottom:38px; }}
  h2 {{ font-size:15px; font-weight:600; margin:0 0 16px; padding-left:11px;
        border-left:4px solid var(--red); display:flex; align-items:center; gap:9px; }}
  .count {{ color:var(--muted); font-weight:400; font-size:13px; }}
  ol {{ list-style:none; margin:0; padding:0; }}
  li {{ border-bottom:1px solid var(--line); }}
  li a {{ display:flex; gap:13px; padding:15px 6px; text-decoration:none; color:inherit;
          transition:background .15s; }}
  li a:hover {{ background:#fff; }}
  .idx {{ color:var(--red); font-variant-numeric:tabular-nums; font-weight:600;
          font-size:15px; min-width:22px; padding-top:2px; }}
  .body {{ display:block; }}
  .zh {{ display:block; font-size:17px; font-weight:500; }}
  .en {{ display:block; color:var(--muted); font-size:13px; margin-top:3px; }}
  .meta {{ display:block; color:#b0b0b0; font-size:12px; margin-top:6px; }}
  footer {{ color:var(--muted); font-size:12.5px; border-top:1px solid var(--line);
            padding-top:16px; line-height:1.8; }}
</style></head>
<body><div class="wrap">
<header>
  <h1>彭博社简报 · {now.strftime('%Y-%m-%d')} {slot}</h1>
  <div class="when">生成于 {now.strftime('%H:%M')} · 中国市场 {len(china)} 条 · 全球头条 {len(headline)} 条</div>
</header>
{block("🇨🇳 中国市场", china, "#c0392b")}
{block("🌍 全球头条", headline, "#2c6cb0")}
<footer>
  数据源：Bloomberg 官方新闻索引（sitemaps/news/latest.xml）。<br>
  只聚合标题与原文链接，不含正文 —— 完整内容请点开原文阅读。
</footer>
</div></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description="彭博新闻飞书推送")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--dry-run", action="store_true", help="只预览，不推送")
    parser.add_argument("--slot", choices=["早间", "晚间"], help="时段标签")
    parser.add_argument("--hours", type=float, help="回看小时数，覆盖配置")
    parser.add_argument("--no-translate", action="store_true")
    parser.add_argument("--no-dedupe", action="store_true", help="忽略已推记录")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(Path(args.config))
    now = datetime.now(CST)
    slot = args.slot or guess_slot(now)
    hours = args.hours or cfg["lookback_hours"]
    db_path = ROOT / "data" / "news.db"

    logging.info("拉取彭博新闻，回看 %.1f 小时", hours)
    try:
        raw = fetch_news(
            lookback_hours=hours, proxy=cfg["proxy"], timeout=cfg["timeout"]
        )
    except Exception as exc:  # noqa: BLE001
        logging.error("抓取失败：%s", exc)
        return 2
    logging.info("窗口内原始 %d 条", len(raw))
    if not raw:
        logging.warning("窗口内没有新闻，退出")
        return 0

    pool = raw if args.no_dedupe else filter_new(raw, db_path)
    dropped = len(raw) - len(pool)
    if dropped:
        logging.info("按去重记录剔除 %d 条", dropped)

    china, headline = rank(
        pool,
        china_limit=cfg["china_limit"],
        headline_limit=cfg["headline_limit"],
    )
    logging.info("入选：中国市场 %d 条 / 全球头条 %d 条", len(china), len(headline))

    if not china and not headline:
        logging.warning("没有命中分类的新闻，检查关键词表或放宽 hours")
        return 0

    translate_titles(
        china + headline,
        db_path=db_path,
        enabled=cfg["translate"] and not args.no_translate,
        proxy=cfg["proxy"],
    )

    if args.dry_run:
        out = ROOT / "reports" / "news_preview.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            render_html(china, headline, slot=slot, now=now), encoding="utf-8"
        )
        for group, label in ((china, "中国市场"), (headline, "全球头条")):
            print(f"\n=== {label} ===")
            for idx, item in enumerate(group, 1):
                print(f"{idx:>2}. [{item.local_time()}] {item.display_title}")
                if item.title_zh and item.title_zh != item.title:
                    print(f"    {item.title}")
                print(f"    {item.url}")
        print(f"\n预览页已生成：{out}")
        return 0

    payload = build_card(china, headline, slot=slot, now=now)
    ok, msg = send(
        cfg["feishu"].get("webhook", ""),
        payload,
        secret=cfg["feishu"].get("secret", ""),
    )
    if not ok:
        logging.error("推送失败：%s", msg)
        return 3

    mark_pushed(china + headline, db_path, slot=slot)
    logging.info("推送成功：%d 条", len(china) + len(headline))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
