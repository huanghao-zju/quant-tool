"""中国市场识别 + 头条打分。

两件事分开做：
  classify() —— 判定一条新闻是否中国市场相关，产出 is_china
  score_item() —— 给头条排序用的分数

头条栏刻意排除已进中国栏的条目，两栏互补不重复：
中国 GDP 数据这种"既是头条又是中国"的新闻，在 China 栏出现一次就够了。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import List, Tuple

from .dedupe import dedupe_events
from .models import NewsItem

# ---------------------------------------------------------------- 中国市场识别

# 强信号：直接指向中国的地域 / 政策 / 货币。命中任意一个即判定为中国相关。
CHINA_STRONG = [
    r"china", r"chinese", r"beijing", r"shanghai", r"shenzhen", r"guangzhou",
    r"guangdong", r"hong kong", r"macau", r"xinjiang", r"tibet",
    r"xi jinping", r"li qiang", r"he lifeng", r"pan gongsheng",
    r"pboc", r"people'?s bank of china", r"ndrc", r"communist party", r"ccp",
    r"state council", r"lianghui", r"national people'?s congress",
    r"yuan", r"renminbi", r"rmb", r"cnh", r"\bcny\b", r"onshore bond",
    r"a-?shares?", r"hang seng", r"csi ?300", r"shanghai composite",
    r"star market", r"chinext", r"northbound", r"southbound",
]

# 主体信号：中国公司 / 机构。单独出现也足以判定（标题里出现 BYD 基本就是中国新闻）。
CHINA_ENTITIES = [
    r"alibaba", r"ant group", r"tencent", r"byd", r"catl", r"huawei", r"xiaomi",
    r"jd\.com", r"baidu", r"meituan", r"pinduoduo", r"pdd", r"temu", r"shein",
    r"smic", r"country garden", r"evergrande", r"vanke", r"nio", r"li auto",
    r"xpeng", r"geely", r"icbc", r"cnooc", r"petrochina", r"sinopec",
    r"didi", r"bytedance", r"douyin", r"sensetime", r"wingtech", r"midea",
    r"china southern", r"air china", r"east money", r"futu", r"ke\.com",
    r"hong kong exchange", r"hkex", r"alipay", r"wechat pay",
]

# 刻意不收录 tariff / trade war / stimulus / export control 这类泛词。
# 它们单独出现时绝大多数是美国或欧洲的新闻，放进中国栏会让信噪比崩掉；
# 而这些词真正和中国相关时，标题里几乎必然已经出现 china / beijing / chinese
# 或某家中国公司，强信号和主体信号足够覆盖。

_CHINA_STRONG_RE = [re.compile(rf"\b{p}\b", re.I) for p in CHINA_STRONG]
_CHINA_ENTITY_RE = [re.compile(rf"\b{p}\b", re.I) for p in CHINA_ENTITIES]


def classify(item: NewsItem) -> NewsItem:
    """就地标记 item 是否中国市场相关，并记录命中词（便于调试误判）。"""
    hits = [p for p, rx in zip(CHINA_STRONG, _CHINA_STRONG_RE) if rx.search(item.title)]
    hits += [p for p, rx in zip(CHINA_ENTITIES, _CHINA_ENTITY_RE) if rx.search(item.title)]

    item.is_china = bool(hits)
    item.hits = hits
    return item

# ---------------------------------------------------------------- 头条打分

# 高权重：能左右全球风险偏好的事件
HEAVY = [
    r"federal reserve", r"\bfed\b", r"powell", r"rate cut", r"rate hike",
    r"inflation", r"\bcpi\b", r"\bpce\b", r"payrolls", r"jobs report",
    r"recession", r"gdp", r"tariffs?", r"trade war", r"sanctions?",
    r"opec", r"brent", r"crude", r"treasurys?", r"bond yields?",
    r"ecb", r"bank of japan", r"imf", r"debt ceiling", r"shutdown",
    r"default", r"bankrupt", r"crisis", r"plunge", r"surge", r"soar",
]

# 中权重：大公司 / 大国政治
MEDIUM = [
    r"\btrump\b", r"\bbiden\b", r"white house", r"congress", r"senate",
    r"election", r"nvidia", r"apple", r"tesla", r"microsoft", r"amazon",
    r"alphabet", r"google", r"meta", r"openai", r"\bai\b", r"samsung",
    r"tsmc", r"earnings", r"\bipo\b", r"merger", r"acquisition", r"layoffs",
    r"\boil\b", r"\bgold\b", r"bitcoin", r"dollar", r"euro", r"yen",
]

_HEAVY_RE = [re.compile(rf"{p}", re.I) for p in HEAVY]
_MEDIUM_RE = [re.compile(rf"{p}", re.I) for p in MEDIUM]

# 板块权重：硬新闻 > 特稿 > 快讯邮件 > 观点
SECTION_BONUS = {
    "articles": 1.5,
    "features": 0.5,
    "newsletters": 0.0,
    "opinion": -0.5,
    "video": 0.3,
    "audio": 0.3,
    "other": 0.0,
}


def score_item(item: NewsItem, now: datetime, max_age_hours: float = 30.0) -> NewsItem:
    """计算头条分。关键词命中 + 板块权重 + 时新度衰减。"""
    title = item.title

    heavy = sum(1 for rx in _HEAVY_RE if rx.search(title))
    medium = sum(1 for rx in _MEDIUM_RE if rx.search(title))
    # 关键词分封顶 9，避免长标题堆词刷分
    kw = min(heavy * 3.0 + medium * 1.2, 9.0)

    section = SECTION_BONUS.get(item.section, 0.0)

    age_h = (now - item.published).total_seconds() / 3600.0
    # 时新度 0~3 分，max_age_hours 之外归零
    recency = max(0.0, 3.0 * (1.0 - age_h / max_age_hours))

    item.score = round(kw + section + recency, 2)
    return item


# ------------------------------------------------------------ 中国栏专用打分

# 市场/政策类词。中国栏要的是"能影响资产价格的中国消息"，
# 不是"发生在中国的事"——后者会把地震、体育、社会新闻顶上来。
MARKET_WORDS = [
    r"stocks?", r"shares?", r"bonds?", r"yields?", r"\bipo\b", r"earnings",
    r"revenue", r"sales", r"profits?", r"\bgdp\b", r"econom(y|ic)", r"trade",
    r"exports?", r"imports?", r"loans?", r"banks?", r"lenders?", r"\brates?\b",
    r"inflation", r"\bcpi\b", r"\bppi\b", r"currency", r"markets?", r"investors?",
    r"funds?", r"valuation", r"debt", r"stimulus", r"polic(y|ies)", r"tariffs?",
    r"regulators?", r"sanctions?", r"mergers?", r"acquisition", r"buyback",
    r"default", r"slowdown", r"rebound", r"recovery", r"spending", r"demand",
]

# 社会/灾难/娱乐类。这些不是市场信号，命中即大幅降权。
SOFT_WORDS = [
    r"earthquakes?", r"kills?", r"killed", r"dies", r"deaths?", r"floods?",
    r"typhoons?", r"hurricanes?", r"wildfires?", r"accidents?", r"rescues?",
    r"football", r"basketball", r"olympic", r"celebrity", r"movies?", r"films?",
    r"albums?", r"weddings?", r"recipes?", r"fashion week",
]

_MARKET_RE = [re.compile(rf"\b{p}\b", re.I) for p in MARKET_WORDS]
_SOFT_RE = [re.compile(rf"\b{p}\b", re.I) for p in SOFT_WORDS]
# 中国是动作主体：「China's Exports Beat Estimates」比「Gucci Slumps in China」更重要
_CHINA_SUBJECT_RE = re.compile(r"^\s*(china|chinese|beijing|pboc)\b|china['’]s\b", re.I)


def score_china(item: NewsItem, now: datetime, max_age_hours: float = 30.0) -> NewsItem:
    """中国栏打分 = 通用分 + 市场词加分 + 主体加分 - 社会新闻降权。"""
    base = item.score if item.score else score_item(item, now, max_age_hours).score

    market_hits = sum(1 for rx in _MARKET_RE if rx.search(item.title))
    market_bonus = min(market_hits * 1.5, 4.5)

    subject_bonus = 2.0 if _CHINA_SUBJECT_RE.search(item.title) else 0.0

    soft_hits = sum(1 for rx in _SOFT_RE if rx.search(item.title))
    soft_penalty = min(soft_hits * 3.0, 6.0)

    item.score = round(base + market_bonus + subject_bonus - soft_penalty, 2)
    return item


def build_digest(
    items: List[NewsItem],
    now: datetime,
    china_slots: int = 6,
    headline_slots: int = 4,
    max_age_hours: float = 30.0,
) -> Tuple[List[NewsItem], List[NewsItem]]:
    """从候选池里分配出「中国市场」和「全球头条」两栏。

    中国栏优先且保底：即使当天中国市场新闻不足，也不拿头条新闻凑数，
    宁可少推几条 —— 用户要的是信号密度，不是凑满格。
    """
    scored = []
    for raw in items:
        it = NewsItem(
            url=raw.url, title=raw.title, published=raw.published,
            source=raw.source, section=raw.section,
        )
        score_item(it, now, max_age_hours)
        classify(it)
        scored.append(it)

    china_pool = [i for i in scored if i.is_china]
    for it in china_pool:
        score_china(it, now, max_age_hours)
    # 先按分排序再按事件去重，同一事件只留最高分那条；去重必须在截断之前，
    # 否则重复项会占掉名额，实际推送的条数比配置的少
    china_sorted = dedupe_events(sorted(china_pool, key=lambda x: x.score, reverse=True))
    china = china_sorted[:china_slots]
    china_keys = {i.key for i in china}

    # 中国栏空出来的名额让给头条，保持每次推送的总条数稳定。
    # 周末彭博中国稿极少（实测周日 40 条里一条中国相关都没有），
    # 不补的话用户会收到一张只有头条的空壳卡片。
    # 注意这里只补「头条栏的名额」，绝不把头条新闻塞进中国栏充数 ——
    # 两栏的语义必须各自干净。
    headline_slots_effective = headline_slots + (china_slots - len(china))

    headline_pool = [i for i in scored if not i.is_china and i.key not in china_keys]
    headline_sorted = dedupe_events(sorted(headline_pool, key=lambda x: x.score, reverse=True))
    headline = headline_sorted[:headline_slots_effective]

    return china, headline
