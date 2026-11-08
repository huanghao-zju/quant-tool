"""新闻简报模块测试。

全部离线：sitemap 用固定 XML 片段，飞书用假的 webhook 校验签名与卡片结构，
不碰网络。
"""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest

from screener.news.bloomberg import _parse_ts, _section_of, parse_sitemap
from screener.news.dedupe import dedupe_events, is_same_event
from screener.news.feishu import _gen_sign, build_card
from screener.news.models import Digest, NewsItem
from screener.news.rank import build_digest, classify
from screener.news.store import Store

TZ = datetime.now().astimezone().tzinfo
NOW = datetime.now(TZ)


def make(title, hours_ago=1.0, section="articles", url=None):
    return NewsItem(
        url=url or f"https://www.bloomberg.com/news/articles/{abs(hash(title))}",
        title=title,
        published=NOW - timedelta(hours=hours_ago),
        section=section,
    )


# ---------------------------------------------------------------- sitemap 解析

SITEMAP_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
  <url>
    <loc>https://www.bloomberg.com/news/articles/2026-10-22/china-shares-enter-bear-market</loc>
    <lastmod>2026-10-22T21:02:04.190Z</lastmod>
    <news:news>
      <news:publication><news:name>Bloomberg News</news:name></news:publication>
      <news:publication_date>2026-10-22T17:16:07.202Z</news:publication_date>
      <news:title>China Shares Enter Bear Market</news:title>
    </news:news>
  </url>
  <url>
    <loc>https://www.bloomberg.com/news/newsletters/2026-10-22/markets-daily</loc>
    <lastmod>2026-10-22T20:00:00.000Z</lastmod>
    <news:news>
      <news:publication_date>2026-10-22T20:00:00.000Z</news:publication_date>
      <news:title>Markets Daily: What Moved Overnight</news:title>
    </news:news>
  </url>
  <url>
    <loc>https://www.bloomberg.com/news/articles/2026-10-22/no-title-here</loc>
    <lastmod>2026-10-22T19:00:00.000Z</lastmod>
  </url>
</urlset>
"""


def test_parse_sitemap_keeps_title_url_time():
    items = parse_sitemap(SITEMAP_XML)
    # 第三条没有 news:title，应当被丢弃
    assert len(items) == 2
    first = items[0]
    assert first.title == "China Shares Enter Bear Market"
    assert first.url.endswith("china-shares-enter-bear-market")
    assert first.section == "articles"
    assert items[1].section == "newsletters"
    # Z 时间必须转成本地时区，且换算正确
    assert first.published.utcoffset() == NOW.utcoffset()


def test_section_of():
    assert _section_of("https://www.bloomberg.com/opinion/articles/x") == "opinion"
    assert _section_of("https://www.bloomberg.com/news/features/x") == "features"
    assert _section_of("https://www.bloomberg.com/something/else") == "other"


def test_parse_ts_handles_millis_and_missing():
    assert _parse_ts("2026-10-22T17:16:07.202Z") is not None
    assert _parse_ts("2026-10-22T17:16:07Z") is not None
    assert _parse_ts("") is None
    assert _parse_ts("not-a-date") is None


# ---------------------------------------------------------------- 中国市场识别


@pytest.mark.parametrize(
    "title",
    [
        "China Shares Enter Bear Market as Fears Over AI Bubble Grow",
        "PBOC Cuts Reserve Requirement Ratio",
        "Alibaba Plans $5 Billion Bond Sale",
        "Hong Kong IPO Pipeline Swells",
        "Yuan Weakens Past 7.3 Per Dollar",
    ],
)
def test_classify_china_positive(title):
    assert classify(make(title)).is_china


@pytest.mark.parametrize(
    "title",
    [
        "Fed Holds Rates Steady as Inflation Cools",
        "Trump Punts on Wind Turbine Tariffs",
        # 泛词不该单独触发：tariff 和 US 都出现了，但跟中国无关
        "US Tariff Exemption Gives Relief to European Carmakers",
        "Meta Credit Risk Gauge Surges to Record",
    ],
)
def test_classify_china_negative(title):
    assert not classify(make(title)).is_china


# ---------------------------------------------------------------- 事件去重


def test_dedupe_merges_same_event_different_wording():
    """实测样本：同一事件两篇不同措辞，相似度只有 0.47，但必须合并。"""
    a = make("China Shares Enter Bear Market as Fears Over AI Bubble Grow", url="u1")
    b = make("Chinese Stocks Tumble Into Bear Market as $2.4 Trillion Vanishes", url="u2")
    assert is_same_event(a, b)
    assert len(dedupe_events([a, b])) == 1


def test_dedupe_keeps_different_companies():
    """Hermès 和 Gucci 都在讲中国消费降级，但不是同一件事，不能合并。"""
    a = make("Hermès Sales Lag Estimates as Chinese Shoppers Curb Spending", url="u1")
    b = make("Gucci Sales Disappoint on China Slump as Turnaround Falters", url="u2")
    assert not is_same_event(a, b)
    assert len(dedupe_events([a, b])) == 2


def test_dedupe_keeps_different_regions():
    """同模板不同地区不能合并，否则会丢信息。"""
    a = make("US Stocks Rally as Investors Bet on Rate Cuts", url="u1")
    b = make("European Stocks Rally as Investors Bet on Rate Cuts", url="u2")
    assert not is_same_event(a, b)


def test_dedupe_respects_time_gap():
    """同一主题隔了一天多，属于后续进展，各自保留。"""
    a = make("China Shares Enter Bear Market", hours_ago=1, url="u1")
    b = make("China Shares Enter Bear Market", hours_ago=60, url="u2")
    assert not is_same_event(a, b)


# ---------------------------------------------------------------- 分栏


def test_build_digest_separates_and_respects_slots():
    items = [
        make("China Shares Enter Bear Market as Fears Grow"),
        make("PBOC Cuts Reserve Requirement Ratio"),
        make("Alibaba Plans $5 Billion Bond Sale"),
        make("Fed Holds Rates Steady as Inflation Cools"),
        make("Trump Punts on Wind Turbine Tariffs"),
        make("Meta Credit Risk Gauge Surges to Record"),
    ]
    china, headline = build_digest(items, now=NOW, china_slots=2, headline_slots=2)
    assert len(china) == 2
    assert len(headline) == 2
    assert all(i.is_china for i in china)
    assert not any(i.is_china for i in headline)
    # 两栏不重复
    assert not ({i.key for i in china} & {i.key for i in headline})


def test_build_digest_does_not_pad_china_with_headlines():
    """中国新闻不足时，头条新闻不能挪进中国栏冒充中国消息。"""
    items = [
        make("PBOC Cuts Reserve Requirement Ratio"),
        make("Fed Holds Rates Steady as Inflation Cools"),
        make("Trump Punts on Wind Turbine Tariffs"),
    ]
    china, _ = build_digest(items, now=NOW, china_slots=5, headline_slots=2)
    assert len(china) == 1
    assert all(i.is_china for i in china)


def test_build_digest_lends_empty_china_slots_to_headline():
    """中国栏空出来的名额让给头条，总条数保持稳定——否则周末会收到空壳卡片。"""
    items = [
        make("Fed Holds Rates Steady as Inflation Cools"),
        make("Trump Punts on Wind Turbine Tariffs"),
        make("Meta Credit Risk Gauge Surges to Record"),
        make("Gold Falls as Higher Oil Fuels Inflation Fears"),
    ]
    china, headline = build_digest(items, now=NOW, china_slots=2, headline_slots=2)
    assert len(china) == 0
    # 2 个头条名额 + 空出来的 2 个中国名额
    assert len(headline) == 4


def test_social_news_ranks_below_market_news():
    """地震这类社会新闻不该压过市场新闻。"""
    quake = make("Powerful Tibet Earthquake Kills 18 People", hours_ago=0.5)
    market = make("China Shares Enter Bear Market as Fears Grow", hours_ago=6)
    china, _ = build_digest([quake, market], now=NOW, china_slots=2, headline_slots=0)
    assert china[0].title.startswith("China Shares")


# ---------------------------------------------------------------- 存储去重


def test_store_drops_already_pushed(tmp_path):
    store = Store(tmp_path / "news.db")
    a, b = make("A title long enough here", url="u1"), make("B title long enough here", url="u2")

    assert len(store.drop_pushed([a, b])) == 2
    store.record_push("2026-10-23:morning", [a], [b])
    assert store.drop_pushed([a, b]) == []
    assert store.slot_done("2026-10-23:morning")
    assert not store.slot_done("2026-10-23:evening")


def test_store_does_not_mark_unseen_as_pushed(tmp_path):
    """被挤出榜单的条目下次还应有资格——不能因为扫到过就永久屏蔽。"""
    store = Store(tmp_path / "news.db")
    a = make("Only one title here ok", url="u1")
    store.record_push("2026-10-23:morning", [], [])
    assert len(store.drop_pushed([a])) == 1


# ---------------------------------------------------------------- 飞书


def test_feishu_sign_matches_official_algorithm():
    ts, secret = 1700000000, "test-secret"
    string_to_sign = f"{ts}\n{secret}"
    expected = base64.b64encode(
        hmac.new(string_to_sign.encode(), b"", digestmod=hashlib.sha256).digest()
    ).decode()
    assert _gen_sign(ts, secret) == expected


def test_build_card_structure():
    china = [make("China Shares Enter Bear Market as Fears Grow")]
    headline = [make("Fed Holds Rates Steady as Inflation Cools")]
    digest = Digest(slot="morning", generated=NOW, china=china, headline=headline)
    card = build_card(digest)

    assert card["msg_type"] == "interactive"
    assert "早报" in card["card"]["header"]["title"]["content"]

    body = json.dumps(card, ensure_ascii=False)
    assert "中国市场" in body
    assert "全球头条" in body
    # 标题必须以 markdown 链接形式带上原文地址
    assert china[0].url in body


def test_build_card_handles_empty_section():
    digest = Digest(slot="evening", generated=NOW, china=[], headline=[])
    card = build_card(digest)
    body = json.dumps(card, ensure_ascii=False)
    assert "暂无" in body
    assert "晚报" in card["card"]["header"]["title"]["content"]
