"""命令行入口。

    python -m screener update                 # 拉取行情快照 + 最新财务数据入库
    python -m screener update --spot-only     # 只刷新行情（盘中反复执行的就是这个）
    python -m screener screen                 # 按 config.yaml 筛选（自动记留痕）
    python -m screener screen -o result.csv   # 结果另存 CSV
    python -m screener history                # 查看选股留痕，供事后回测
    python -m screener status                 # 查看缓存数据新鲜度
    python -m screener ui                     # 启动浏览器界面
"""

from __future__ import annotations

import argparse
import json
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
            n_div = int(metrics["div_yield"].notna().sum())
            n_cagr = int(metrics["cagr_3y"].notna().sum())
            print(f"衍生指标: 股息率 {n_div} 只，CAGR {n_cagr} 只")
    return 0


def _config_hash(config: dict) -> str:
    """筛选条件的指纹。条件变了指纹就变，回测时能区分不同策略的留痕。"""
    import hashlib

    payload = {
        k: config.get(k)
        for k in ("filters", "exclude_st", "exclude_bse", "sort_by", "max_per_industry", "top")
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:12]


def cmd_screen(args: argparse.Namespace) -> int:
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    cache = Cache(args.db)
    spot, fetched_at = cache.load_spot()
    fin = cache.load_financials(config.get("report_date"))
    metrics = cache.load_metrics()
    # 多期财报宽表，用于算连续正增长标记。取不到就退化成单期，不影响主流程。
    history = None
    if not args.no_stability and config.get("stability_periods", 2) > 1:
        history = cache.load_financials_history(
            ["revenue_yoy", "net_profit_yoy"],
            periods=int(config.get("stability_periods", 2)),
        )
    if spot is None or fin is None or fin.empty:
        print("缓存为空，请先执行: python -m screener update", file=sys.stderr)
        return 1

    # 先不限流、不截断跑一次拿真实命中数，留痕里要记这个数
    live = {**config, "top": None, "max_per_industry": config.get("max_per_industry")}
    full = screen_frames(spot, fin, live, metrics, history)
    matched = len(full)
    result = full if config.get("top") is None else full.head(int(config["top"]))
    fields = config.get("output_fields")
    if fields:
        result = result[[c for c in fields if c in result.columns]]

    if not args.no_record:
        cache.save_screen_history(
            result,
            config,
            str(fin["report_date"].iloc[0]),
            _config_hash(config),
            json.dumps(config, ensure_ascii=False, default=str),
            matched=matched,
        )

    print(f"行情快照时间: {fetched_at} | 报告期: {fin['report_date'].iloc[0]}")
    if history is not None and not history.empty:
        n_dates = len([c for c in history.columns if c.startswith("revenue_yoy@")])
        print(f"稳定性对比期数: {n_dates}（revenue_stable / profit_stable 基于同比增速）")
    cap = config.get("max_per_industry")
    cap_txt = f"，行业限流后剩 {matched} 只" if cap and matched != len(result) else ""
    print(f"条件命中 {matched} 只{cap_txt}，输出 {len(result)} 只:\n")
    with __import__("pandas").option_context(
        "display.max_rows", None, "display.width", None
    ):
        print(result.to_string(index=False))
    if args.output:
        result.to_csv(args.output, index=False, encoding="utf-8-sig")
        print(f"\n已导出: {args.output}")
    if not args.no_record:
        print("已记入选股留痕（python -m screener history 查看）")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    cache = Cache(args.db)
    df = cache.list_history(args.limit)
    if df.empty:
        print("还没有留痕记录，跑一次 python -m screener screen 即可开始积累。")
        return 0
    with __import__("pandas").option_context(
        "display.max_rows", None, "display.width", None
    ):
        print(df.to_string(index=False))
    print(f"\n共 {len(df)} 次记录。留痕包含入选时的价格，可用于事后算收益。")
    if args.prune_days:
        n = cache.prune_history(args.prune_days)
        print(f"已清理 {args.prune_days} 天前的 {n} 条记录。")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    for k, v in Cache(args.db).status().items():
        print(f"{k}: {v or '(无数据)'}")
    return 0


def ui_command(args: argparse.Namespace) -> list[str]:
    """拼 streamlit 启动命令。"""
    app = Path(__file__).resolve().parent / "app.py"
    return [
        sys.executable, "-m", "streamlit", "run", str(app),
        "--server.port", str(args.port),
        # 默认只监听本机：Streamlit 默认绑 0.0.0.0，会把持仓筛选结果暴露到局域网/公网
        "--server.address", args.host,
        "--browser.gatherUsageStats", "false",
        # 始终 headless。非 headless 时 Streamlit 首次运行会卡在 "Email:" 引导
        # 提示上等输入，服务起不来；浏览器改由下面自己打开。
        "--server.headless", "true",
    ]


def cmd_ui(args: argparse.Namespace) -> int:
    import os
    import subprocess
    import threading
    import webbrowser

    url = f"http://{args.host}:{args.port}"
    if not args.headless:
        threading.Timer(2.0, webbrowser.open, args=(url,)).start()
    print(f"筛选器界面: {url}   (Ctrl-C 退出)")
    # 全局 --db 与 -c 通过环境变量传给 Streamlit 子进程
    env = {**os.environ, "SCREENER_DB": str(args.db), "SCREENER_CONFIG": str(args.config)}
    try:
        return subprocess.call(ui_command(args), env=env)
    except KeyboardInterrupt:
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
    p_screen.add_argument(
        "--no-record", action="store_true", help="不记入选股留痕（默认记，回测用）"
    )
    p_screen.add_argument(
        "--no-stability",
        action="store_true",
        help="不加载多期财报，跳过连续正增长标记",
    )
    p_screen.set_defaults(func=cmd_screen)

    p_history = sub.add_parser("history", help="查看选股留痕（回测用）")
    p_history.add_argument("--limit", type=int, default=30, help="显示最近几条")
    p_history.add_argument(
        "--prune-days", type=int, help="顺便清理 N 天前的旧留痕"
    )
    p_history.set_defaults(func=cmd_history)

    p_status = sub.add_parser("status", help="查看缓存新鲜度")
    p_status.set_defaults(func=cmd_status)

    p_ui = sub.add_parser("ui", help="启动 Streamlit 浏览器界面")
    p_ui.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="筛选配置 YAML（界面初始值）")
    p_ui.add_argument("--port", type=int, default=8501)
    p_ui.add_argument(
        "--host", default="localhost", help="监听地址，默认仅本机；填 0.0.0.0 可局域网访问"
    )
    p_ui.add_argument("--headless", action="store_true", help="不自动打开浏览器")
    p_ui.set_defaults(func=cmd_ui)

    args = parser.parse_args(argv)
    if args.db is None:
        from .cache import DEFAULT_DB

        args.db = DEFAULT_DB
    return args.func(args)
