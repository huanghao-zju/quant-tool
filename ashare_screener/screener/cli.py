"""命令行入口。

    python -m screener update                 # 拉取行情快照 + 最新财务数据入库
    python -m screener update --spot-only     # 只刷新行情（盘中反复执行的就是这个）
    python -m screener screen                 # 按 config.yaml 筛选
    python -m screener screen -o result.csv   # 结果另存 CSV
    python -m screener status                 # 查看缓存数据新鲜度
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from . import fetch
from .cache import Cache
from .screen import screen_frames

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"


def cmd_update(args: argparse.Namespace) -> int:
    cache = Cache(args.db)
    spot = fetch.fetch_spot()
    cache.save_spot(spot)
    print(f"行情快照: {len(spot)} 只")
    if not args.spot_only:
        if args.report_date:
            fin = fetch.fetch_financials(args.report_date)
        else:
            fin = fetch.fetch_latest_financials()
        cache.save_financials(fin)
        print(f"财务数据: {len(fin)} 只，报告期 {fin['report_date'].iloc[0]}")
        if not args.no_metrics:
            metrics = fetch.fetch_metrics()
            cache.save_metrics(metrics)
            n_cagr = int(metrics["cagr_3y"].notna().sum())
            n_roe = int(metrics["roe_annual"].notna().sum())
            n_div = int(metrics["div_yield"].notna().sum())
            print(
                f"衍生指标(年报口径): CAGR {n_cagr} 只，护城河质量 {n_roe} 只，"
                f"股息率 {n_div} 只"
            )
    return 0


def cmd_screen(args: argparse.Namespace) -> int:
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    cache = Cache(args.db)
    spot, fetched_at = cache.load_spot()
    fin = cache.load_financials(config.get("report_date"))
    metrics = cache.load_metrics()
    if spot is None or fin is None or fin.empty:
        print("缓存为空，请先执行: python -m screener update", file=sys.stderr)
        return 1

    result = screen_frames(spot, fin, config, metrics)
    print(f"行情快照时间: {fetched_at} | 报告期: {fin['report_date'].iloc[0]}")
    print(f"命中 {len(result)} 只:\n")
    with __import__("pandas").option_context(
        "display.max_rows", None, "display.width", None
    ):
        print(result.to_string(index=False))
    if args.output:
        result.to_csv(args.output, index=False, encoding="utf-8-sig")
        print(f"\n已导出: {args.output}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    for k, v in Cache(args.db).status().items():
        print(f"{k}: {v or '(无数据)'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="screener", description="A 股量化筛选工具")
    parser.add_argument("--db", default=None, help="SQLite 缓存路径，默认 data/screener.db")
    sub = parser.add_subparsers(dest="command", required=True)

    p_update = sub.add_parser("update", help="拉取并缓存全市场数据")
    p_update.add_argument("--spot-only", action="store_true", help="只刷新行情快照")
    p_update.add_argument(
        "--no-metrics", action="store_true", help="跳过股息率/CAGR 拉取（较慢）"
    )
    p_update.add_argument("--report-date", help="指定报告期 YYYYMMDD，默认自动取最近一期")
    p_update.set_defaults(func=cmd_update)

    p_screen = sub.add_parser("screen", help="按配置筛选")
    p_screen.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="筛选配置 YAML")
    p_screen.add_argument("-o", "--output", help="结果导出 CSV 路径")
    p_screen.set_defaults(func=cmd_screen)

    p_status = sub.add_parser("status", help="查看缓存新鲜度")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    if args.db is None:
        from .cache import DEFAULT_DB

        args.db = DEFAULT_DB
    return args.func(args)
