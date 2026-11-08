"""SQLite 本地缓存。

财务数据一个季度才变一次、行情快照盘中才有意义，筛选时直接读缓存，
避免每跑一次就把全市场数据重拉一遍。
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pandas as pd

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "screener.db"


class Cache:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )

    def close(self) -> None:
        self.conn.close()

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        )
        self.conn.commit()

    def _get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    # ---- 行情快照（整表覆盖） ----

    def save_spot(self, df: pd.DataFrame) -> None:
        df.to_sql("spot", self.conn, if_exists="replace", index=False)
        self._set_meta("spot_fetched_at", dt.datetime.now().isoformat(timespec="seconds"))

    def load_spot(self) -> tuple[pd.DataFrame | None, str | None]:
        fetched_at = self._get_meta("spot_fetched_at")
        if fetched_at is None:
            return None, None
        return pd.read_sql("SELECT * FROM spot", self.conn), fetched_at

    # ---- 财务数据（按报告期覆盖对应分区） ----

    def save_financials(self, df: pd.DataFrame) -> None:
        report_date = df["report_date"].iloc[0]
        try:
            self.conn.execute(
                "DELETE FROM financials WHERE report_date=?", (report_date,)
            )
        except sqlite3.OperationalError:
            pass  # 表还不存在
        df.to_sql("financials", self.conn, if_exists="append", index=False)
        self._set_meta("financials_report_date", report_date)
        self._set_meta(
            "financials_fetched_at", dt.datetime.now().isoformat(timespec="seconds")
        )

    def load_financials(self, report_date: str | None = None) -> pd.DataFrame | None:
        report_date = report_date or self._get_meta("financials_report_date")
        if report_date is None:
            return None
        try:
            return pd.read_sql(
                "SELECT * FROM financials WHERE report_date=?",
                self.conn,
                params=(report_date,),
            )
        except (pd.errors.DatabaseError, sqlite3.OperationalError):
            return None

    def available_report_dates(self, limit: int = 8) -> list[str]:
        """缓存里已有哪些报告期，新到旧。只取实际有数据的那些。"""
        try:
            rows = self.conn.execute(
                "SELECT report_date FROM financials GROUP BY report_date "
                "ORDER BY report_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [r[0] for r in rows]

    def load_financials_history(self, fields: list[str], periods: int = 2) -> pd.DataFrame:
        """把最近 `periods` 期财报的指定字段按 code 横向拼成一张宽表。

        ⚠ 跨报告期的「累计值」不可直接比较：一季报 revenue 只累计 3 个月，
        半年报累计 6 个月，前者天然只有后者的一半。所以对比必须用同比增速
        这类本身可比的字段（`revenue_yoy` / `net_profit_yoy`），不能用
        revenue / net_profit / roe 的绝对值。

        返回的列名带报告期后缀，如 revenue_yoy@20260331，便于识别口径。
        """
        dates = self.available_report_dates(periods)
        if not dates:
            return pd.DataFrame()
        want = ["code", *fields]
        frames = []
        for d in dates:
            try:
                part = pd.read_sql(
                    f"SELECT {','.join(want)} FROM financials WHERE report_date=?",
                    self.conn,
                    params=(d,),
                )
            except (pd.errors.DatabaseError, sqlite3.OperationalError):
                continue
            if part.empty:
                continue
            part = part.rename(columns={f: f"{f}@{d}" for f in fields})
            frames.append(part)
        if not frames:
            return pd.DataFrame()
        out = frames[0]
        for f in frames[1:]:
            out = out.merge(f, on="code", how="inner")
        return out

    # ---- 衍生指标：股息率 / 多年 CAGR（整表覆盖） ----

    def save_metrics(self, df: pd.DataFrame) -> None:
        df.to_sql("metrics", self.conn, if_exists="replace", index=False)
        self._set_meta(
            "metrics_fetched_at", dt.datetime.now().isoformat(timespec="seconds")
        )

    def load_metrics(self) -> pd.DataFrame | None:
        if self._get_meta("metrics_fetched_at") is None:
            return None
        try:
            return pd.read_sql("SELECT * FROM metrics", self.conn)
        except (pd.errors.DatabaseError, sqlite3.OperationalError):
            return None

    def status(self) -> dict[str, str | None]:
        return {
            "spot_fetched_at": self._get_meta("spot_fetched_at"),
            "financials_report_date": self._get_meta("financials_report_date"),
            "financials_fetched_at": self._get_meta("financials_fetched_at"),
            "metrics_fetched_at": self._get_meta("metrics_fetched_at"),
        }

    # ---- 选股留痕（回测用） ----

    def init_history(self) -> None:
        """建留痕表。每次筛选留一份快照，事后可算这些股票后续表现。"""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS screen_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                screened_at TEXT NOT NULL,
                report_date TEXT,
                config_hash TEXT,
                config_json TEXT,
                matched INTEGER,
                UNIQUE(screened_at)
            );
            CREATE TABLE IF NOT EXISTS screen_history_items (
                screened_at TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT,
                industry TEXT,
                price REAL,
                total_mcap REAL,
                pe_ttm REAL,
                pegy REAL,
                rank INTEGER,
                PRIMARY KEY (screened_at, code)
            );
            CREATE INDEX IF NOT EXISTS idx_items_code
                ON screen_history_items(code);
            """
        )
        self.conn.commit()

    def save_screen_history(
        self,
        result: pd.DataFrame,
        config: dict,
        report_date: str | None,
        config_hash: str,
        config_json: str,
        matched: int | None = None,
    ) -> str:
        """把一次筛选结果连同当时的条件存进留痕表。

        存 price / pe_ttm 等当时的值，是为了让"入选即买入"这个假设可复现。
        同一次筛选重复执行会覆盖（ON CONFLICT REPLACE），不会重复计数。
        """
        self.init_history()
        screened_at = dt.datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT OR REPLACE INTO screen_history "
            "(screened_at, report_date, config_hash, config_json, matched) "
            "VALUES (?,?,?,?,?)",
            (screened_at, report_date, config_hash, config_json, matched),
        )
        rows = [
            (
                screened_at,
                str(r.get("code")),
                r.get("name"),
                r.get("industry"),
                _as_float(r.get("price")),
                _as_float(r.get("total_mcap")),
                _as_float(r.get("pe_ttm")),
                _as_float(r.get("pegy")),
                i + 1,
            )
            for i, (_, r) in enumerate(result.iterrows())
            if r.get("code") is not None
        ]
        if rows:
            self.conn.executemany(
                "INSERT OR REPLACE INTO screen_history_items "
                "(screened_at, code, name, industry, price, total_mcap, pe_ttm, pegy, rank) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )
        self.conn.commit()
        return screened_at

    def list_history(self, limit: int = 30) -> pd.DataFrame:
        """列出留痕记录，新到旧。"""
        self.init_history()
        return pd.read_sql(
            "SELECT screened_at, report_date, matched, config_hash "
            "FROM screen_history ORDER BY screened_at DESC LIMIT ?",
            self.conn,
            params=(limit,),
        )

    def load_picks(self, screened_at: str) -> pd.DataFrame:
        """取某次留痕的选股明细。"""
        return pd.read_sql(
            "SELECT * FROM screen_history_items WHERE screened_at=? ORDER BY rank",
            self.conn,
            params=(screened_at,),
        )

    def latest_picks(self) -> pd.DataFrame | None:
        """最近一次留痕的选股明细。"""
        self.init_history()
        row = self.conn.execute(
            "SELECT screened_at FROM screen_history ORDER BY screened_at DESC LIMIT 1"
        ).fetchone()
        return self.load_picks(row[0]) if row else None

    def prune_history(self, keep_days: int = 365) -> int:
        """删除太老的留痕，返回删除的记录数。

        比较只取日期部分（截到天）：`screened_at` 精确到秒，若 cutoff 也取到秒
        会与同一天的记录相等，删不掉。用 <= 保证 cutoff 当天及之前的都清掉。
        """
        self.init_history()
        cutoff = (dt.datetime.now() - dt.timedelta(days=keep_days)).date().isoformat()
        n = self.conn.execute(
            "SELECT COUNT(*) FROM screen_history WHERE substr(screened_at, 1, 10) <= ?",
            (cutoff,),
        ).fetchone()[0]
        self.conn.execute(
            "DELETE FROM screen_history_items WHERE substr(screened_at, 1, 10) <= ?",
            (cutoff,),
        )
        self.conn.execute(
            "DELETE FROM screen_history WHERE substr(screened_at, 1, 10) <= ?", (cutoff,)
        )
        self.conn.commit()
        return n


def _as_float(v) -> float | None:
    """留痕表里的数值列，安全转 float（None/NaN/非数一律存 NULL）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f
