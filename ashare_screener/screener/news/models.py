"""新闻条目数据结构。

彭博只能拿到 title / url / published 三要素（正文在付费墙后，且不应搬运），
所以字段刻意保持最小，不提供 content 字段，避免下游误以为能拿到全文。
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List


@dataclass
class NewsItem:
    url: str
    title: str
    published: datetime          # 发布时间（本地时区）
    source: str = "bloomberg"    # bloomberg / wallstreetcn
    section: str = ""            # 彭博 URL 推导出的板块：articles / newsletters / opinion
    score: float = 0.0           # 头条打分，仅排序用，不对外
    is_china: bool = False       # 是否命中中国市场关键词
    hits: List[str] = field(default_factory=list)  # 命中的关键词，便于调试

    @property
    def key(self) -> str:
        """去重键。用 URL 而不是标题：同一事件彭博会改标题重发。"""
        return self.url

    @property
    def published_str(self) -> str:
        return self.published.strftime("%m-%d %H:%M")


@dataclass
class Digest:
    """一次推送的内容载体。"""
    slot: str                                  # morning / evening
    generated: datetime
    china: List[NewsItem] = field(default_factory=list)
    headline: List[NewsItem] = field(default_factory=list)
    fallback_note: str = ""                    # 降级说明，VPN 不通时写给用户看

    @property
    def total(self) -> int:
        return len(self.china) + len(self.headline)

    @property
    def is_empty(self) -> bool:
        return self.total == 0
