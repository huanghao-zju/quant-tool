"""水平/速度双条件计算（SPEC §3）与阶段条件评估（SPEC §4）。

约定（SPEC §5.1）：
- 水平型条件：连续 confirm_closes 个收盘确认；
- 速度型条件：即时生效。
evaluate() 可传 asof 在历史任意日期重放（回测复用同一逻辑）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class Evaluation:
    date: pd.Timestamp
    yellow: list[str] = field(default_factory=list)   # 阶段1 条件命中
    orange: list[str] = field(default_factory=list)   # 阶段2
    red: list[str] = field(default_factory=list)      # 阶段3
    dashboard: dict[str, bool] = field(default_factory=dict)  # 慢变量看板 9 项
    readings: dict[str, str] = field(default_factory=dict)    # 现值快照

    @property
    def dashboard_score(self) -> str:
        return f"{sum(self.dashboard.values())}/{len(self.dashboard)}"

    def conditions_at_or_above(self, stage: int) -> list[str]:
        pools = {1: self.yellow + self.orange + self.red,
                 2: self.orange + self.red,
                 3: self.red}
        return pools.get(stage, self.yellow + self.orange + self.red)

    @property
    def highest_stage(self) -> int:
        if self.red:
            return 3
        if self.orange:
            return 2
        if self.yellow:
            return 1
        return 0


def _cut(s: pd.Series | None, asof: pd.Timestamp) -> pd.Series | None:
    if s is None:
        return None
    s = s.loc[:asof]
    return s if not s.empty else None


def _confirmed(s: pd.Series, cond, n: int) -> bool:
    """水平型：最近 n 个收盘全部满足 cond。"""
    if len(s) < n:
        return False
    return bool(cond(s.iloc[-n:]).all())


def _last(s: pd.Series | None) -> float | None:
    return None if s is None or s.empty else float(s.iloc[-1])


def _chg(s: pd.Series, periods: int) -> float | None:
    """当前值 − periods 个观测前的值。"""
    if len(s) <= periods:
        return None
    return float(s.iloc[-1] - s.iloc[-1 - periods])


def evaluate(data: dict[str, pd.Series], cfg: dict, events: list[dict],
             asof: pd.Timestamp | None = None) -> Evaluation:
    if asof is None:
        candidates = [s.index[-1] for s in data.values() if s is not None and not s.empty]
        asof = max(candidates) if candidates else pd.Timestamp.utcnow().tz_localize(None).normalize()
    asof = pd.Timestamp(asof)
    d = {k: _cut(v, asof) for k, v in data.items()}
    yc, db, sm = cfg["yen_channel"], cfg["dashboard"], cfg["state_machine"]
    n_confirm = sm["confirm_closes"]
    ev = Evaluation(date=asof)

    usdjpy, jgb10, jgb30 = d.get("usdjpy"), d.get("jgb10"), d.get("jgb30")
    cftc_s, vix, nikkei, nasdaq = d.get("cftc_jpy"), d.get("vix"), d.get("nikkei"), d.get("nasdaq")
    hy = d.get("hy_oas")

    # ── 阶段1 · 黄色（Mode B：贬值失控）──────────────
    c = yc["usdjpy"]
    if usdjpy is not None and _confirmed(usdjpy, lambda x: x >= c["level_high"], n_confirm):
        ev.yellow.append(f"USDJPY≥{c['level_high']} 连续{n_confirm}收盘确认（现值 {_last(usdjpy):.2f}）")
    if jgb30 is not None and jgb10 is not None:
        j30_ath = _confirmed(jgb30, lambda x: x >= jgb30.max(), 1) and len(jgb30) > 20
        j10_hi = _confirmed(jgb10, lambda x: x > yc["jgb_10y"]["level_pct"], n_confirm)
        if j30_ath and j10_hi:
            ev.yellow.append(f"30Y JGB 创新高（{_last(jgb30):.3f}%）且 10Y>{yc['jgb_10y']['level_pct']}%（{_last(jgb10):.3f}%）")
    for e in events:
        if str(e.get("type")) == "fiscal_surprise" and pd.Timestamp(e["date"]) <= asof:
            ev.yellow.append(f"事件录入：财政包超预期（{e['date']}：{e.get('note','')}）")

    # ── 阶段2 · 橙色（政策被迫转向）─────────────────
    if usdjpy is not None and len(usdjpy) >= sm["usdjpy_30d_high_lookback"]:
        w30 = usdjpy.iloc[-sm["usdjpy_30d_high_lookback"]:]
        hi = float(w30.max())
        drop1w = _chg(usdjpy, c["week_days"])  # 现值 − 5交易日前
        if (drop1w is not None and -drop1w > sm["orange_drop_1w_yen"]
                and hi - _last(usdjpy) > sm["orange_drop_1w_yen"]):
            ev.orange.append(f"USDJPY 单周急跌 {-drop1w:.1f}円（30日高点 {hi:.2f} → {_last(usdjpy):.2f}）")
    for e in events:
        if str(e.get("type")) in ("boj_intermeeting", "mof_intervention") and pd.Timestamp(e["date"]) <= asof:
            ev.orange.append(f"事件录入：{e['type']}（{e['date']}：{e.get('note','')}）")
    unwind = _cftc_unwind(cftc_s, yc["cftc_jpy"])
    if unwind is not None and unwind > yc["cftc_jpy"]["unwind_start_2w_pct"]:
        ev.orange.append(f"CFTC 日元净空头从极值两周骤减 {unwind:.0%}")

    # ── 阶段3 · 红色（Mode A：套息平仓 + 全球传导）──
    if usdjpy is not None:
        drop2w = _chg(usdjpy, c["two_week_days"])
        if drop2w is not None and -drop2w > c["drop_2w_yen"]:
            vix_jump = vix is not None and (j := _chg(vix, 1)) is not None and j > db["vix"]["daily_jump"]
            joint = (nikkei is not None and nasdaq is not None
                     and (nk := _pct_chg(nikkei, 1)) is not None and (nq := _pct_chg(nasdaq, 1)) is not None
                     and nk < yc["nikkei"]["joint_drop_pct"] and nq < yc["nikkei"]["joint_drop_pct"])
            if vix_jump or joint:
                ev.red.append(f"USDJPY 两周下跌 {-drop2w:.1f}円 且 {'VIX单日跳升' if vix_jump else '日经+纳指同日<-3%'}")
    if unwind is not None and unwind > yc["cftc_jpy"]["unwind_2w_pct"]:
        ev.red.append(f"CFTC 日元净空头两周骤减 {unwind:.0%}（>50%，平仓进行中）")
    if hy is not None and _confirmed(hy, lambda x: x > db["hy_oas"]["distress_bp"] / 100.0, n_confirm):
        ev.red.append(f"HY OAS 突破 {db['hy_oas']['distress_bp']}bp（现值 {_last(hy)*100:.0f}bp）→ 任何阶段直接跳红")

    # ── 慢变量看板（9 项，不参与分级）────────────────
    ev.dashboard = _dashboard(d, db, asof)

    # ── 现值快照 ────────────────────────────────────
    _readings(ev, d, yc)
    return ev


def _pct_chg(s: pd.Series, periods: int) -> float | None:
    if len(s) <= periods or s.iloc[-1 - periods] == 0:
        return None
    return float(s.iloc[-1] / s.iloc[-1 - periods] - 1) * 100


def _cftc_unwind(s: pd.Series | None, c: dict) -> float | None:
    """净空头从极值两周内的收缩比例。返回 None = 无净空头极值前提。"""
    if s is None or len(s) < 8:
        return None
    shorts = (-s).clip(lower=0)  # 净空头规模
    if shorts.iloc[-1] < 0 or len(shorts) < 8:
        return None
    recent = shorts.iloc[-3:]            # 两周 ≈ 3 个周度报告点（含当期）
    ref = float(recent.iloc[0])
    if ref <= 0:
        return None
    pctile = float((shorts < ref).mean())  # ref 在全样本（约2年）中的分位
    if pctile < c["crowding_pctile"]:
        return None
    return (ref - float(shorts.iloc[-1])) / ref


def _dashboard(d: dict, db: dict, asof: pd.Timestamp) -> dict[str, bool]:
    out: dict[str, bool] = {}
    hy = d.get("hy_oas")
    out["HY OAS≥350bp"] = hy is not None and _last(hy) * 100 >= db["hy_oas"]["warn_bp"]
    vix = d.get("vix")
    out["VIX>25或单日+8"] = vix is not None and (
        _last(vix) > db["vix"]["close_level"]
        or ((j := _chg(vix, 1)) is not None and j > db["vix"]["daily_jump"]))
    sofr, iorb = d.get("sofr"), d.get("iorb")
    flag = False
    if sofr is not None and iorb is not None:
        n = db["sofr_iorb"]["spread_positive_days"]
        spread = (sofr - iorb).dropna()
        if len(spread) >= n:
            w = spread.iloc[-n:]
            flag = bool((w > 0).all() and w.iloc[-1] > w.iloc[0])
    out[f"SOFR−IORB 转正走阔"] = flag
    srf = d.get("srf")
    flag = False
    if srf is not None and not srf.empty:
        last_d, v = srf.index[-1], float(srf.iloc[-1])
        is_period_end = last_d.day >= (last_d.days_in_month - 2)
        flag = (not is_period_end) and v > db["srf"]["normal_max_usd_bn"] * db["srf"]["anomaly_multiple"]
    out["SRF 非期末异常用量"] = flag
    brent = d.get("brent")
    out[f"布伦特>{db['brent']['level_usd']}"] = brent is not None and _last(brent) > db["brent"]["level_usd"]
    pay = d.get("payems")
    flag = False
    if pay is not None and len(pay) >= db["payrolls"]["negative_months"] + 1:
        diffs = pay.diff().dropna().iloc[-db["payrolls"]["negative_months"]:]
        flag = bool((diffs < 0).all())
    out["非农连续2月负增长"] = flag
    sahm = d.get("sahm")
    out["Sahm≥0.50"] = sahm is not None and _last(sahm) >= db["sahm"]["level"]
    flag = False
    for k in ("owl", "ares"):
        s = d.get(k)
        if s is not None and (r := _pct_chg(s, 5)) is not None and r < db["owl_ares"]["drop_5d_pct"]:
            flag = True
    out["OWL/ARES 5日<-10%"] = flag
    bizd = d.get("bizd")
    flag = False
    if bizd is not None and len(bizd) > 10:
        hi52 = float(bizd.iloc[-252:].max()) if len(bizd) >= 252 else float(bizd.max())
        flag = (_last(bizd) / hi52 - 1) * 100 < db["bizd"]["drawdown_52w_pct"]
    out["BIZD 52周回撤>15%"] = flag
    return out


def _readings(ev: Evaluation, d: dict, yc: dict) -> None:
    fmt = {
        "usdjpy": ("USDJPY", "{:.2f}"), "jgb10": ("10Y JGB", "{:.3f}%"),
        "jgb30": ("30Y JGB", "{:.3f}%"), "vix": ("VIX", "{:.1f}"),
        "hy_oas": ("HY OAS", None), "brent": ("布伦特", "{:.1f}"),
        "nikkei": ("日经225", "{:.0f}"), "nasdaq": ("纳指", "{:.0f}"),
        "sofr": ("SOFR", "{:.2f}%"), "iorb": ("IORB", "{:.2f}%"),
        "sahm": ("Sahm", "{:.2f}"), "bizd": ("BIZD", "{:.2f}"),
        "owl": ("OWL", "{:.2f}"), "ares": ("ARES", "{:.2f}"),
        "srf": ("SRF用量", "{:.1f}bn"),
    }
    for k, (name, f) in fmt.items():
        v = _last(d.get(k))
        if v is None:
            continue
        ev.readings[name] = f"{v*100:.0f}bp" if k == "hy_oas" else f.format(v)
    cftc_s = d.get("cftc_jpy")
    if cftc_s is not None and not cftc_s.empty:
        net = float(cftc_s.iloc[-1])
        shorts = (-cftc_s).clip(lower=0)
        pctile = float((shorts < max(-net, 0)).mean()) if net < 0 else 0.0
        ev.readings["CFTC日元净头寸"] = f"{net:,.0f} 手" + (f"（净空头2年分位 {pctile:.0%}）" if net < 0 else "")
