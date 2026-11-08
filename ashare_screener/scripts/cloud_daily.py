"""云端日报：拉数据 → 筛选 → 生成简报 → 推送飞书。

与本地 `scripts/verify_daily.py` 的区别：这里产出的是**给人看的**报告，
不是给 CI 看的断言输出。

云端运行的关键约束（都已在 workflow 里处理，这里只管业务逻辑）：
- 每次运行是全新环境，SQLite 库从零开始，必须自己把历史财报期补齐，
  否则 `stability_periods: 2` 拿不到对比期，`revenue_stable` 全是空值。
- 报告期自动取最新已披露的一期（年报/一季报/半年报/三季报）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import yaml

from screener.cache import Cache, DEFAULT_DB
from screener.cli import screen_frames
from screener.fetch import fetch_financials, fetch_metrics, fetch_spot, report_dates

ROOT = Path(__file__).resolve().parent.parent


def load_cfg() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}


def build_db(db_path: str, cfg: dict, history_periods: int) -> None:
    """从零构建缓存：行情 + 最新财报期 + 往前 history_periods 期财报 + 衍生指标。

    多期财报是 `stability_periods` 的前提。最新一期用 fetch_latest_financials
    的回退逻辑（未披露就退上一期），历史期则直接按季末日期取。
    """
    cache = Cache(db_path)

    print("[1/4] 拉行情快照…", flush=True)
    cache.save_spot(fetch_spot())

    print("[2/4] 拉财报（最新期）…", flush=True)
    fin = None
    last_err: Exception | None = None
    for d in report_dates(dt.date.today(), n=4):
        try:
            cand = fetch_financials(d)
        except Exception as e:
            last_err = e
            continue
        if len(cand) >= 1000:
            fin = cand
            break
    if fin is None:
        raise RuntimeError(f"近 4 个报告期都拿不到财报数据：{last_err}")
    cache.save_financials(fin)
    latest = fin["report_date"].iloc[0]
    print(f"      最新报告期 {latest}，{len(fin)} 只", flush=True)

    # ⚠ 保存顺序很关键：save_financials 每次调用都会把 meta 的
    # financials_report_date 覆盖成本期。所以**必须最后存最新期**，
    # 否则 meta 会停在历史期上，screen 会拿一季报当最新期筛。
    # （先存历史期 → 再存最新期，meta 才是对的。）
    dates = report_dates(dt.date.today(), n=8)
    older = [d for d in dates if d < latest][:history_periods]
    for d in reversed(older):  # 旧的先存，最新的最后存
        try:
            hist = fetch_financials(d)
        except Exception as e:
            print(f"      历史期 {d} 跳过：{e}", flush=True)
            continue
        if len(hist) >= 1000:
            cache.save_financials(hist)
            print(f"      历史期 {d}，{len(hist)} 只", flush=True)

    # 最后再存一次最新期，把 meta 钉回最新报告期
    cache.save_financials(fin)

    print("[3/4] 拉衍生指标（股息率/CAGR）…", flush=True)
    metrics = fetch_metrics()
    cache.save_metrics(metrics)
    print(f"      股息率 {int(metrics['div_yield'].notna().sum())} 只，"
          f"CAGR {int(metrics['cagr_3y'].notna().sum())} 只", flush=True)

    have = cache.available_report_dates(history_periods + 1)
    print(f"[4/4] 缓存报告期：{have}", flush=True)
    if len(have) < 2:
        print("      ⚠ 只有 1 期财报，revenue_stable/profit_stable 将为空", flush=True)


def compute(cfg: dict, db_path: str) -> dict:
    """跑四种口径的筛选，返回报告所需的全部统计量。"""
    cache = Cache(db_path)
    spot, fetched_at = cache.load_spot()
    fin = cache.load_financials(cfg.get("report_date"))
    metrics = cache.load_metrics()
    periods = int(cfg.get("stability_periods", 2))
    history = None
    if periods > 1:
        history = cache.load_financials_history(
            ["revenue_yoy", "net_profit_yoy"], periods=periods
        )

    pool = screen_frames(spot, fin, {"filters": [], "top": None,
                                      "max_per_industry": None,
                                      "exclude_st": cfg["exclude_st"]},
                         metrics, history)
    pre = screen_frames(spot, fin, {**cfg, "top": None, "max_per_industry": None},
                        metrics, history)
    post = screen_frames(spot, fin, {**cfg, "top": None}, metrics, history)
    final = screen_frames(spot, fin, cfg, metrics, history)

    kept = set(post["code"])
    dropped = pre[~pre["code"].isin(kept)]
    return {
        "spot_fetched_at": fetched_at,
        "report_date": cfg.get("report_date") or (fin["report_date"].iloc[0] if len(fin) else None),
        "pool": len(pool), "pre": len(pre), "post": len(post), "final": len(final),
        "ind_pre": int(pre["industry"].nunique()),
        "ind_post": int(post["industry"].nunique()),
        "ind_final": int(final["industry"].nunique()),
        "top3_pre": pre["industry"].value_counts().head(3).to_dict(),
        "top3_final": final["industry"].value_counts().head(3).to_dict(),
        "dropped": dropped["industry"].value_counts().to_dict(),
        "final_df": final,
        "pre_df": pre,
    }


def render_markdown(stats: dict, cfg: dict, now: dt.datetime) -> str:
    """生成 Markdown 简报。"""
    final = stats["final_df"]
    t10 = final.head(10).reset_index(drop=True)
    fetched = stats["spot_fetched_at"]
    lines: list[str] = []
    A = lines.append

    A(f"# A 股基本面筛选日报 · {now:%Y-%m-%d}")
    A("")
    A(f"> **策略**：彼得林奇改进 PEGY < {cfg['filters'][0]['value']}（config.yaml）")
    A(f"> **报告期**：{stats['report_date']}")
    A(f"> **生成时间**：{now:%Y-%m-%d %H:%M}（云端 GitHub Actions）")
    A("")

    # ---- 命中概览 ----
    A("## 筛选结果概览")
    A("")
    A("| 阶段 | 数量 |")
    A("|---|---|")
    A(f"| 可筛选池（全市场剔除 ST/退市） | {stats['pool']:,} |")
    A(f"| 条件命中（限流前） | **{stats['pre']}** |")
    A(f"| 行业限流后（每行业≤{cfg.get('max_per_industry')}） | {stats['post']} |")
    A(f"| `top: {cfg.get('top')}` 截断后实际导出 | {stats['final']} |")
    A("")
    if stats["dropped"]:
        A("行业限流剔除 "
          + "、".join(f"{k} {v}" for k, v in stats["dropped"].items())
          + f"，共 {sum(stats['dropped'].values())} 只。")
        A("")

    # ---- 前 10 ----
    A("## 前 10 只（按 PEGY 升序）")
    A("")
    A("| 代码 | 名称 | 行业 | 最新价 | 动态PE | 3年净利CAGR% | 股息率% | PEGY | 年化ROE% | 营收同比% |")
    A("|---|---|---|---|---|---|---|---|---|---|")

    def num(v, dec=2):
        return "—" if pd.isna(v) else f"{v:.{dec}f}"

    for _, r in t10.iterrows():
        A(f"| {r['code']} | {str(r['name']).strip()} | {r['industry']} | "
          f"{num(r['price'])} | {num(r['pe_ttm'])} | {num(r['cagr_3y'])} | "
          f"{num(r['div_yield'])} | {num(r['pegy'], 3)} | {num(r['roe_annual'])} | "
          f"{num(r['revenue_yoy'])} |")
    A("")

    if len(t10):
        A("### 前 10 特征")
        A("")
        A(f"- **PE 区间 {t10['pe_ttm'].min():.2f} ~ {t10['pe_ttm'].max():.2f}**；"
          f"PEGY 区间 {t10['pegy'].min():.3f} ~ {t10['pegy'].max():.3f}")
        A(f"- **年化 ROE 区间 {t10['roe_annual'].min():.2f}% ~ {t10['roe_annual'].max():.2f}%**")
        ind = t10["industry"].value_counts().to_dict()
        A(f"- **行业分布**：" + "、".join(f"{k} {v} 只" for k, v in ind.items()))
        A("")

    # ---- 整体分布 ----
    A("## 全量整体分布")
    A("")
    A("| 指标 | 中位数 | 最小 | 最大 |")
    A("|---|---|---|---|")
    A(f"| 动态 PE | {final['pe_ttm'].median():.2f} | {final['pe_ttm'].min():.2f} | {final['pe_ttm'].max():.2f} |")
    A(f"| PEGY | {final['pegy'].median():.3f} | {final['pegy'].min():.3f} | {final['pegy'].max():.3f} |")
    A(f"| 年化 ROE | {final['roe_annual'].median():.2f}% | {final['roe_annual'].min():.2f}% | {final['roe_annual'].max():.2f}% |")
    A(f"| 3 年净利 CAGR | {final['cagr_3y'].median():.2f}% | — | — |")
    A(f"| 股息率 | {final['div_yield'].median():.2f}% | — | — |")
    A(f"| 营收同比 | {final['revenue_yoy'].median():.2f}% | — | — |")
    A(f"| PB | {final['pb'].median():.2f} | — | — |")
    A(f"| 总市值 | {final['total_mcap'].median() / 1e8:.1f} 亿 | — | — |")
    A("")
    A(f"- 覆盖 **{stats['ind_final']} 个行业**，"
      f"前三行业（{'/'.join(stats['top3_final'].keys())} 各 {list(stats['top3_final'].values())[0]} 只）"
      f"占比 {sum(list(stats['top3_final'].values())[:3]) / len(final) * 100:.0f}%")
    A("")

    # ---- 使用提示 ----
    A("## ⚠️ 使用提示")
    A("")
    A("1. **周期股 PEGY 陷阱**：PEGY 分母用过去 3 年 CAGR，周期股盈利峰值会压低 PEGY —— "
      "增速接近翻倍的标的需人工复核盈利可持续性")
    A("2. 金融股（保险/证券/银行）无毛利率概念，`gross_margin` 为空属正常")
    A("3. 数据源走东财 datacenter-web 备用源，缺 `amount` / `volume_ratio` / `pct_60d` / `pct_ytd` 字段")
    A("")
    A(f"**行情快照**：{fetched}　**留痕**：`screened_at` 已写入 screen_history 表")
    return "\n".join(lines)


def push_feishu(markdown: str, webhook: str, secret: str) -> bool:
    """推送到飞书群机器人。webhook 为空则跳过（不算失败）。"""
    if not webhook:
        print("未配置飞书 webhook，跳过推送", flush=True)
        return False
    import base64
    import hashlib
    import hmac
    import time
    import urllib.request

    payload: dict = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "elements": [{"tag": "div",
                          "text": {"tag": "lark_md", "content": markdown[:4000]}}],
        },
    }
    if secret:
        ts = int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"] = base64.b64encode(
            hmac.new(f"{ts}\n{secret}".encode(), b"", hashlib.sha256).digest()
        ).decode()
    req = urllib.request.Request(
        webhook, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", "ignore")
        print(f"飞书推送结果：{body[:200]}", flush=True)
        return '"code":0' in body or '"StatusCode":0' in body
    except Exception as e:
        print(f"飞书推送失败：{e}", flush=True)
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out-csv", default="out_daily.csv")
    ap.add_argument("--out-md", default="")
    ap.add_argument("--skip-build", action="store_true",
                    help="跳过拉数据，直接用现有库筛选（调试用）")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    cfg = load_cfg()
    now = dt.datetime.now()

    if not args.skip_build:
        build_db(args.db, cfg, int(cfg.get("stability_periods", 2)))

    stats = compute(cfg, args.db)

    # 导出 CSV（与本地 screener screen 的产物同构）
    csv_path = Path(args.out_csv)
    if not csv_path.is_absolute():
        csv_path = ROOT / csv_path
    stats["final_df"].to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"CSV 已写出：{csv_path}（{len(stats['final_df'])} 行）", flush=True)

    md = render_markdown(stats, cfg, now)
    if args.out_md:
        md_path = Path(args.out_md)
        if not md_path.is_absolute():
            md_path = ROOT / md_path
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md, encoding="utf-8")
        print(f"Markdown 已写出：{md_path}", flush=True)

    if not args.no_push:
        news_cfg = (cfg.get("news") or {}).get("feishu") or {}
        push_feishu(md, news_cfg.get("webhook") or "", news_cfg.get("secret") or "")

    print("\n=== 摘要 ===")
    print(f"命中 {stats['pre']} → 限流后 {stats['post']} → 导出 {stats['final']}")
    print(f"行业数 {stats['ind_final']}，前 10 首行："
          f"{stats['final_df'].iloc[0]['code']} {stats['final_df'].iloc[0]['name']} "
          f"pegy={stats['final_df'].iloc[0]['pegy']:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
