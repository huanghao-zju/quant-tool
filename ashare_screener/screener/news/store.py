"""已推送记录，用于跨次去重。

去重键是 URL。只记录「推送成功过」的条目，见过但没进榜单的不记录 ——
一条新闻这次被挤出 top 10，下一轮仍有机会进来，不该因为"扫到过"就被永久屏蔽。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Set

from .models import NewsItem


def _utc_iso(dt: Optional[datetime] = None) -> str:
    """统一用 UTC 的 ISO 串落库。

    表里的时间戳是按字符串比较的（pushed_at >= cutoff），一旦混入不同时区偏移，
    字符串序和真实时序就会不一致 —— 宿主机在 UTC、配置时区在 +08:00 时，
    「最近 14 天」会算错。全部转 UTC 是唯一稳妥的做法。
    """
    dt = dt or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()

SCHEMA = """
CREATE TABLE IF NOT EXISTS pushed_items (
    url        TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    published  TEXT,
    source     TEXT,
    slot_key   TEXT NOT NULL,
    pushed_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pushed_at ON pushed_items(pushed_at);

CREATE TABLE IF NOT EXISTS slots (
    slot_key       TEXT PRIMARY KEY,
    pushed_at      TEXT NOT NULL,
    china_count    INTEGER NOT NULL DEFAULT 0,
    headline_count INTEGER NOT NULL DEFAULT 0
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------ 去重

    def pushed_urls(self, days: int = 14) -> Set[str]:
        """最近 N 天推送过的 URL。窗口给足，避免同一条新闻跨天反复出现。"""
        cutoff = _utc_iso(datetime.now(timezone.utc) - timedelta(days=days))
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT url FROM pushed_items WHERE pushed_at >= ?", (cutoff,)
            ).fetchall()
        return {r["url"] for r in rows}

    def drop_pushed(self, items: Iterable[NewsItem], days: int = 14) -> List[NewsItem]:
        seen = self.pushed_urls(days)
        return [it for it in items if it.url not in seen]

    # ------------------------------------------------------------ 推送状态

    def slot_done(self, slot_key: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM slots WHERE slot_key = ?", (slot_key,)
            ).fetchone()
        return row is not None

    def record_push(self, slot_key: str, china: List[NewsItem], headline: List[NewsItem]) -> None:
        now = _utc_iso()
        with self._conn() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO pushed_items "
                "(url, title, published, source, slot_key, pushed_at) VALUES (?,?,?,?,?,?)",
                [
                    (it.url, it.title, _utc_iso(it.published), it.source, slot_key, now)
                    for it in list(china) + list(headline)
                ],
            )
            conn.execute(
                "INSERT OR REPLACE INTO slots (slot_key, pushed_at, china_count, headline_count) "
                "VALUES (?,?,?,?)",
                (slot_key, now, len(china), len(headline)),
            )

    # ------------------------------------------------------------ 维护

    def prune(self, keep_days: int = 60) -> int:
        cutoff = _utc_iso(datetime.now(timezone.utc) - timedelta(days=keep_days))
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM pushed_items WHERE pushed_at < ?", (cutoff,))
            conn.execute("DELETE FROM slots WHERE pushed_at < ?", (cutoff,))
        return cur.rowcount
