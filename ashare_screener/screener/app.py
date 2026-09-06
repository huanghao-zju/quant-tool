"""Streamlit 浏览器界面。

    python -m screener ui            # 等价于 streamlit run screener/app.py

左侧调条件，右侧实时出结果。筛选逻辑完全复用 screen.screen_frames，
这里只负责把 config.yaml 的结构映射成表单控件，不重复实现任何筛选规则。
"""

from __future__ import annotations

import datetime as dt
import io
import os
import sys
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

# 直接 `streamlit run screener/app.py` 时包不在 sys.path 里，补一下
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from screener import fetch  # noqa: E402
from screener.cache import DEFAULT_DB, Cache  # noqa: E402
from screener.screen import screen_frames  # noqa: E402

# 由 `screener ui --db / -c` 通过环境变量传入，便于对不同库/策略各开一个界面
CONFIG_PATH = Path(os.environ.get("SCREENER_CONFIG") or ROOT / "config.yaml")
DB_PATH = str(os.environ.get("SCREENER_DB") or DEFAULT_DB)

# 字段 -> 中文名。字典顺序即下拉框顺序。
FIELD_LABELS: dict[str, str] = {
    "code": "代码",
    "name": "名称",
    "industry": "所属行业",
    "price": "最新价",
    "pct_change": "涨跌幅%",
    "pct_60d": "60日涨跌幅%",
    "pct_ytd": "年初至今%",
    "amount": "成交额",
    "turnover_rate": "换手率%",
    "volume_ratio": "量比",
    "pe_ttm": "动态PE",
    "pb": "PB",
    "total_mcap": "总市值",
    "float_mcap": "流通市值",
    "eps": "每股收益",
    "revenue": "营业总收入",
    "revenue_yoy": "营收同比%",
    "net_profit": "净利润",
    "net_profit_yoy": "净利同比%",
    "bps": "每股净资产",
    "roe": "ROE%",
    "ocf_per_share": "每股经营现金流",
    "gross_margin": "毛利率%",
    "div_yield": "股息率%",
    "cagr_3y": "3年净利CAGR%",
    "peg": "PEG",
    "pegy": "PEGY",
}
TEXT_FIELDS = {"code", "name", "industry"}
# 以「元」为单位的大数：输入框和表格都换算成「亿」显示，配置里仍存元
YI_FIELDS = {"total_mcap", "float_mcap", "revenue", "net_profit", "amount"}
NUMERIC_OPS = [">", ">=", "<", "<=", "==", "!=", "between"]
TEXT_OPS = ["contains", "==", "!=", "in"]


# 行情快照超过这个天数就提示重新拉取
STALE_DAYS = 3


def label(field: str) -> str:
    return f"{FIELD_LABELS.get(field, field)} ({field})"


def stale_days(fetched_at: str | None) -> int | None:
    """行情快照距今天数，解析失败返回 None。"""
    if not fetched_at:
        return None
    try:
        return (dt.datetime.now() - dt.datetime.fromisoformat(fetched_at)).days
    except ValueError:
        return None


# ---------- 数据加载 ----------


def cache_status(db_path: str) -> dict:
    cache = Cache(db_path)
    try:
        return cache.status()
    finally:
        cache.close()


@st.cache_data(show_spinner="读取缓存…")
def load_frames(db_path: str, fetched_at: str | None, report_date: str | None):
    """参数带上时间戳，只是为了让 update 之后缓存自动失效。"""
    cache = Cache(db_path)
    try:
        spot, _ = cache.load_spot()
        fin = cache.load_financials(report_date)
        metrics = cache.load_metrics()
    finally:
        cache.close()
    return spot, fin, metrics


def run_update(db_path: str, spot_only: bool) -> None:
    cache = Cache(db_path)
    try:
        with st.status("拉取行情快照…", expanded=True) as status:
            spot = fetch.fetch_spot()
            cache.save_spot(spot)
            st.write(f"行情快照 {len(spot)} 只")
            if not spot_only:
                status.update(label="拉取财务数据…")
                fin = fetch.fetch_latest_financials()
                cache.save_financials(fin)
                st.write(f"财务数据 {len(fin)} 只，报告期 {fin['report_date'].iloc[0]}")
                status.update(label="拉取股息率 / CAGR（较慢）…")
                metrics = fetch.fetch_metrics()
                cache.save_metrics(metrics)
                st.write(f"衍生指标 {len(metrics)} 只")
            status.update(label="更新完成", state="complete")
    finally:
        cache.close()
    load_frames.clear()


# ---------- 筛选条件表单 ----------


def default_config() -> dict:
    if CONFIG_PATH.exists():
        return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return {}


def init_state(config: dict) -> None:
    if "filters" in st.session_state:
        return
    st.session_state.filters = []
    for i, f in enumerate(config.get("filters", [])):
        st.session_state.filters.append({**f, "_id": i})
    st.session_state.next_id = len(st.session_state.filters)


def add_filter() -> None:
    st.session_state.filters.append(
        {"_id": st.session_state.next_id, "field": "roe", "op": ">=", "value": 10}
    )
    st.session_state.next_id += 1


def remove_filter(fid: int) -> None:
    st.session_state.filters = [f for f in st.session_state.filters if f["_id"] != fid]


def _num_input(container, text: str, value: float, key: str, field: str) -> float:
    """大额字段以「亿」为单位收值，返回仍是元。"""
    if field in YI_FIELDS:
        v = container.number_input(
            f"{text}（亿）", value=float(value) / 1e8, key=key, step=1.0,
            label_visibility="collapsed",
        )
        return v * 1e8
    return container.number_input(
        text, value=float(value), key=key, step=1.0, label_visibility="collapsed"
    )


def render_filter_row(f: dict, available: list[str]) -> None:
    """侧栏窄，一个条件占两行：上行字段+删除，下行操作符+值。"""
    fid = f["_id"]
    fields = [x for x in FIELD_LABELS if x in available]
    if f.get("field") not in fields:
        f["field"] = fields[0]

    top, btn = st.columns([5, 1])
    f["field"] = top.selectbox(
        "字段", fields, index=fields.index(f["field"]), key=f"field_{fid}",
        format_func=label, label_visibility="collapsed",
    )
    btn.button("✕", key=f"del_{fid}", on_click=remove_filter, args=(fid,), help="删除此条件")

    is_text = f["field"] in TEXT_FIELDS
    ops = TEXT_OPS if is_text else NUMERIC_OPS
    if f.get("op") not in ops:
        f["op"] = ops[0]
        f["value"] = "" if is_text else 0.0
    c_op, c_val = st.columns([2, 3])
    f["op"] = c_op.selectbox(
        "操作符", ops, index=ops.index(f["op"]), key=f"op_{fid}",
        label_visibility="collapsed",
    )

    # key 里带上控件类型：字段在数值/文本之间切换时不会复用不兼容的旧值
    if is_text:
        raw = f.get("value")
        if f["op"] == "in":
            text = c_val.text_input(
                "值", value=",".join(raw) if isinstance(raw, list) else str(raw or ""),
                key=f"val_{fid}_list", placeholder="逗号分隔", label_visibility="collapsed",
            )
            f["value"] = [s.strip() for s in text.split(",") if s.strip()]
        else:
            f["value"] = c_val.text_input(
                "值", value="" if isinstance(raw, list) else str(raw or ""),
                key=f"val_{fid}_text", label_visibility="collapsed",
            )
    elif f["op"] == "between":
        raw = f.get("value")
        v = raw if isinstance(raw, list) and len(raw) == 2 else [0.0, 0.0]
        lo, hi = c_val.columns(2)
        a = _num_input(lo, "下限", v[0], f"lo_{fid}", f["field"])
        b = _num_input(hi, "上限", v[1], f"hi_{fid}", f["field"])
        f["value"] = [a, b]
    else:
        raw = f.get("value")
        v = 0.0 if isinstance(raw, (list, str)) else (raw or 0.0)
        f["value"] = _num_input(c_val, "值", v, f"val_{fid}_num", f["field"])


def config_to_yaml(config: dict) -> str:
    """去掉内部字段，输出可直接粘回 config.yaml 的文本。"""
    clean = dict(config)
    clean["filters"] = [
        {k: v for k, v in f.items() if not k.startswith("_")} for f in config["filters"]
    ]
    return yaml.safe_dump(
        clean, allow_unicode=True, sort_keys=False, default_flow_style=None
    )


# ---------- 结果展示 ----------


def display_frame(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """元 -> 亿，并生成列名与数字格式配置。"""
    out = df.copy()
    col_cfg: dict = {}
    for c in out.columns:
        name = FIELD_LABELS.get(c, c)
        if c in YI_FIELDS:
            out[c] = pd.to_numeric(out[c], errors="coerce") / 1e8
            col_cfg[c] = st.column_config.NumberColumn(f"{name}(亿)", format="%.2f")
        elif c in TEXT_FIELDS:
            col_cfg[c] = st.column_config.TextColumn(name)
        else:
            col_cfg[c] = st.column_config.NumberColumn(name, format="%.2f")
    return out, col_cfg


# ---------- 页面 ----------


def main() -> None:
    st.set_page_config(page_title="A 股筛选器", page_icon="📈", layout="wide")
    st.title("A 股量化筛选器")

    db_path = DB_PATH
    config = default_config()
    init_state(config)
    status = cache_status(db_path)

    with st.sidebar:
        st.subheader("数据")
        st.caption(f"行情快照：{status['spot_fetched_at'] or '无'}")
        st.caption(
            f"财务报告期：{status['financials_report_date'] or '无'}"
            f"（拉取于 {status['financials_fetched_at'] or '无'}）"
        )
        st.caption(f"衍生指标：{status['metrics_fetched_at'] or '无'}")
        b1, b2 = st.columns(2)
        if b1.button("刷新行情", width="stretch", help="盘中只更新价格/PE/市值"):
            run_update(db_path, spot_only=True)
            st.rerun()
        if b2.button(
            "全量更新", width="stretch", help="行情 + 财务 + 股息率/CAGR，需数分钟"
        ):
            run_update(db_path, spot_only=False)
            st.rerun()

    spot, fin, metrics = load_frames(
        db_path, status["spot_fetched_at"], config.get("report_date")
    )
    if spot is None or fin is None or fin.empty:
        st.warning(
            "缓存为空，请先点左侧「全量更新」，或在终端执行 `python -m screener update`。"
        )
        return

    # 可选字段 = 合并后实际存在的列
    available = list(spot.columns)
    available += [c for c in fin.columns if c not in available]
    if metrics is not None:
        available += [c for c in metrics.columns if c not in available]
    if "pe_ttm" in available and "net_profit_yoy" in available:
        available += ["peg", "pegy"]

    with st.sidebar:
        st.subheader("筛选条件")
        exclude_st = st.checkbox(
            "剔除 ST / 退市风险", value=config.get("exclude_st", True), key="exclude_st"
        )
        exclude_bse = st.checkbox(
            "剔除北交所", value=config.get("exclude_bse", False), key="exclude_bse"
        )
        for f in st.session_state.filters:
            render_filter_row(f, available)
            st.divider()
        st.button("＋ 添加条件", on_click=add_filter, width="stretch")

        st.subheader("排序与输出")
        sortable = [x for x in FIELD_LABELS if x in available and x not in TEXT_FIELDS]
        cfg_sort = config.get("sort_by")
        default_sort = cfg_sort if cfg_sort in sortable else (sortable[0] if sortable else None)
        sort_by = (
            st.selectbox(
                "排序字段", sortable, index=sortable.index(default_sort),
                format_func=label, key="sort_by",
            )
            if sortable
            else None
        )
        ascending = st.toggle("升序", value=config.get("ascending", False), key="ascending")
        top = st.number_input(
            "最多显示", min_value=1, max_value=5000,
            value=int(config.get("top") or 50), key="top",
        )
        field_choices = [x for x in FIELD_LABELS if x in available]
        default_out = [c for c in config.get("output_fields") or [] if c in field_choices] or [
            c
            for c in ["code", "name", "industry", "price", "pe_ttm", "pb", "total_mcap", "roe"]
            if c in field_choices
        ]
        output_fields = st.multiselect(
            "输出列", field_choices, default=default_out, format_func=label, key="output_fields"
        )

    live_config = {
        "report_date": config.get("report_date"),
        "exclude_st": exclude_st,
        "exclude_bse": exclude_bse,
        "filters": st.session_state.filters,
        "sort_by": sort_by,
        "ascending": ascending,
        "top": None,  # 先取全量以便显示真实命中数，截断在下面做
        "output_fields": output_fields,
    }

    try:
        result = screen_frames(spot, fin, live_config, metrics)
    except (KeyError, TypeError, ValueError) as e:
        st.error(f"筛选条件有误：{e}")
        return

    matched = len(result)
    result = result.head(int(top))

    m1, m2, m3 = st.columns(3)
    m1.metric(
        "命中", f"{matched} 只",
        delta=f"显示前 {len(result)} 只" if matched > len(result) else None,
        delta_color="off",
    )
    m2.metric("行情快照", (status["spot_fetched_at"] or "")[:16].replace("T", " "))
    m3.metric("财务报告期", str(fin["report_date"].iloc[0]))

    days = stale_days(status["spot_fetched_at"])
    if days is not None and days >= STALE_DAYS:
        st.warning(
            f"行情快照已是 {days} 天前的数据，价格 / PE / 市值均已过时，"
            "请点左侧「刷新行情」。"
        )

    shown, col_cfg = display_frame(result)
    st.dataframe(
        shown, column_config=col_cfg, width="stretch", hide_index=True, height=600
    )

    buf = io.StringIO()
    result.to_csv(buf, index=False)
    d1, d2 = st.columns([1, 3])
    d1.download_button(
        "下载 CSV",
        buf.getvalue().encode("utf-8-sig"),
        "screen_result.csv",
        "text/csv",
        width="stretch",
    )
    with d2.expander("当前条件对应的 config.yaml"):
        st.code(config_to_yaml({**live_config, "top": int(top)}), language="yaml")


main()
