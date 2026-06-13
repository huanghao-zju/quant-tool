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

    def status(self) -> dict[str, str | None]:
        return {
            "spot_fetched_at": self._get_meta("spot_fetched_at"),
            "financials_report_date": self._get_meta("financials_report_date"),
            "financials_fetched_at": self._get_meta("financials_fetched_at"),
        }
