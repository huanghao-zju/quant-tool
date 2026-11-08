"""事件级去重。

彭博对同一事件常发多篇（「A股进入熊市」+「中国股票跌入熊市，2.4 万亿美元蒸发」），
URL 不同，URL 级去重拦不住，一栏里并列两条显得很蠢。

但错杀比冗余更糟：把「美股大涨」和「欧股大涨」合成一条会丢信息。
所以这里用保守的组合规则，只在有把握时合并：

  1. 相似度 >= 0.72           → 近乎重复，直接合并
  2. 相似度 >= 0.42
     且 显著词交集 >= 2
     且 地区词不冲突           → 同一事件的不同措辞，合并

实测样本：
  A股熊市两条         sim 0.472，交集 {bear, market}，地区词相同 → 合并 ✓
  Hermès vs Gucci     sim 0.336，交集 {sales}              → 保留 ✗
  China AI vs A股熊市 sim 0.417，交集 0                    → 保留 ✗
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable, List, Set

from .models import NewsItem

# 泛用词不算「显著」——它们在任何两条同题材新闻里都会出现，用来判定同事件必然误杀
STOPWORDS = {
    "the", "a", "an", "as", "to", "of", "in", "on", "for", "and", "or", "but",
    "with", "from", "at", "by", "is", "are", "was", "were", "be", "been", "its",
    "this", "that", "these", "those", "after", "before", "over", "into", "amid",
    "than", "more", "less", "most", "very", "will", "may", "can", "could",
    "would", "should", "has", "have", "had", "not", "no", "yet", "now", "new",
    "says", "said", "set", "gets", "get", "said", "who", "what", "how", "why",
    "about", "against", "between", "during", "under", "while", "where", "when",
    "china", "chinese", "beijing", "shanghai", "shenzhen", "hong", "kong",
    "bloomberg", "news", "report", "reports",
}

# 地区词用于防误杀：两条讲不同地区的同模板新闻不能合并。
#
# 短缩写必须单独处理 —— 既躲不过 _TOKEN_RE 的 4 字符下限，也不能忽略大小写：
# 「US Stocks Rally」里的 US 是美国，「Fed tells us nothing」里的 us 是代词。
SHORT_REGIONS = ("US", "UK", "EU")
LONG_REGIONS = (
    "america", "american", "europe", "european", "japan", "japanese",
    "britain", "british", "germany", "german", "france", "french",
    "india", "indian", "korea", "korean", "canada", "canadian", "australia",
    "brazil", "mexico", "russia", "russian", "italy", "italian", "spain",
    "switzerland", "netherlands", "saudi", "turkey", "israel", "iran",
)

_TOKEN_RE = re.compile(r"[a-z0-9$%]{4,}")
_SHORT_REGION_RES = [(w, re.compile(rf"\b{w}\b")) for w in SHORT_REGIONS]
_LONG_REGION_RES = [(w, re.compile(rf"\b{w}\b", re.I)) for w in LONG_REGIONS]


def _tokens(title: str) -> Set[str]:
    words = _TOKEN_RE.findall(title.lower())
    return {w for w in words if w not in STOPWORDS}


def _regions(title: str) -> Set[str]:
    out = {w.lower() for w, rx in _SHORT_REGION_RES if rx.search(title)}
    out |= {w for w, rx in _LONG_REGION_RES if rx.search(title)}
    return out


def is_same_event(a: NewsItem, b: NewsItem) -> bool:
    """判断两条新闻是否指同一事件。"""
    # 发布时间差太远就不算同一事件（同一主题的后续进展应各自保留）
    if abs((a.published - b.published).total_seconds()) > 36 * 3600:
        return False

    # 地区冲突的检查必须排在最前面：两条标题指向不同地区，措辞再像也是两件事。
    # 「US Stocks Rally as Investors Bet on Rate Cuts」和它的 European 版本相似度
    # 高达 0.9，若先走相似度分支会直接把欧洲那条吃掉。
    ra, rb = _regions(a.title), _regions(b.title)
    if ra and rb and not (ra & rb):
        return False

    sim = SequenceMatcher(None, a.title.lower(), b.title.lower()).ratio()
    if sim >= 0.72:
        return True

    if sim < 0.42:
        return False

    shared = _tokens(a.title) & _tokens(b.title)
    return len(shared) >= 2


def dedupe_events(items: Iterable[NewsItem]) -> List[NewsItem]:
    """按事件去重，保留每组里分数最高的那条。

    传入顺序应当是分数降序 —— 先到先得，后面的重复项被丢弃。
    """
    kept: List[NewsItem] = []
    for item in items:
        if any(is_same_event(item, k) for k in kept):
            continue
        kept.append(item)
    return kept
