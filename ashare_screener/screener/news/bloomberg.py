"""彭博社新闻抓取。

只用官方 sitemap，不走页面抓取。原因：
  1. robots.txt 里显式声明了 /sitemaps/news/latest.xml，等于官方授权爬取入口；
  2. bloomberg.com 首页 / 板块页 / 文章页全部被 WAF 挡成 403，
     只有 /sitemaps/* 对爬虫放行（实测：curl 的 TLS 指纹 403，curl_cffi 也一样）；
  3. sitemap 里 news:title 是完整的新闻标题，正文在付费墙后，本来也不该搬运。

结论：能拿到的就是「标题 + 发布时间 + 链接」三件套，这正是一份日报需要的东西。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone, tzinfo
from typing import List, Optional

from .models import NewsItem

SITEMAP_LATEST = "https://www.bloomberg.com/sitemaps/news/latest.xml"

NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "news": "http://www.google.com/schemas/sitemap-news/0.9",
}

# 彭博 URL 里的板块，用于头条打分：analysis 型内容权重低于硬新闻
SECTION_PATTERNS = [
    (r"/news/newsletters/", "newsletters"),
    (r"/news/features/", "features"),
    (r"/opinion/", "opinion"),
    (r"/news/videos?/", "video"),
    (r"/news/audio/", "audio"),
    (r"/news/articles/", "articles"),
]


class BloombergError(RuntimeError):
    """抓取失败。区分 unreachable（VPN 没开）与 blocked（WAF 拦截）。"""

    def __init__(self, message: str, kind: str = "unreachable"):
        super().__init__(message)
        self.kind = kind


def _section_of(url: str) -> str:
    for pat, name in SECTION_PATTERNS:
        if re.search(pat, url):
            return name
    return "other"


def _parse_ts(raw: str, tz: Optional[tzinfo] = None) -> Optional[datetime]:
    """解析 sitemap 时间戳（形如 2026-10-22T17:16:07.202Z）为目标时区 datetime。

    tz 传 None 时用宿主机本地时区。部署到 UTC 的 VPS 上必须显式传 Asia/Shanghai，
    否则「08:30 早报」的窗口判断会整体偏 8 小时。
    """
    if not raw:
        return None
    txt = raw.strip().replace("Z", "+00:00")
    # 截断到秒，兼容带 3 位或 6 位毫秒的情况
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(\+00:00)?$", txt)
    if m:
        txt = m.group(1) + (m.group(3) or "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def parse_sitemap(xml_bytes: bytes, tz: Optional[tzinfo] = None) -> List[NewsItem]:
    """把 sitemap XML 解析成 NewsItem 列表。缺 title 的条目直接丢弃。"""
    root = ET.fromstring(xml_bytes)
    items: List[NewsItem] = []

    for node in root.findall("sm:url", NS):
        loc = node.findtext("sm:loc", default="", namespaces=NS).strip()
        if not loc:
            continue

        title = node.findtext("news:news/news:title", default="", namespaces=NS).strip()
        if not title:
            # sitemap 里极少数条目没有 news:title，没有标题的新闻没法进日报
            continue

        ts_raw = (
            node.findtext("news:news/news:publication_date", default="", namespaces=NS)
            or node.findtext("sm:lastmod", default="", namespaces=NS)
        )
        published = _parse_ts(ts_raw, tz)
        if published is None:
            continue

        items.append(
            NewsItem(
                url=loc,
                title=title,
                published=published,
                source="bloomberg",
                section=_section_of(loc),
            )
        )

    return items


def fetch_news(
    max_age_hours: int = 30,
    timeout: int = 30,
    proxy: Optional[str] = None,
    tz: Optional[tzinfo] = None,
) -> List[NewsItem]:
    """抓取彭博最新新闻，只保留 max_age_hours 内的条目。

    proxy 传 None 表示跟随系统路由（VPN 为 TUN 模式时天然生效）；
    传具体地址则显式走 HTTP 代理。直接部署在境外 VPS 上时两者都不用配。
    tz 为发布日期与新鲜度判断使用的时区，VPS 上应显式传 Asia/Shanghai。
    """
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给出可执行的提示
        raise BloombergError(
            "缺少 curl_cffi，执行：/usr/local/bin/python3 -m pip install --user curl_cffi",
            kind="dependency",
        ) from exc

    kwargs = {"impersonate": "chrome", "timeout": timeout}
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}

    try:
        resp = cffi_requests.get(SITEMAP_LATEST, **kwargs)
    except Exception as exc:
        raise BloombergError(
            f"连不上 bloomberg.com（{type(exc).__name__}），通常是 VPN 没开", kind="unreachable"
        ) from exc

    if resp.status_code == 403:
        raise BloombergError("bloomberg.com 返回 403，WAF 拦截或出口 IP 不受信任", kind="blocked")
    if resp.status_code != 200:
        raise BloombergError(f"彭博 sitemap 返回 HTTP {resp.status_code}", kind="http")

    items = parse_sitemap(resp.content, tz=tz)

    cutoff = datetime.now(tz) - timedelta(hours=max_age_hours)
    fresh = [it for it in items if it.published >= cutoff]
    # sitemap 本身按 lastmod 倒序，但不保证，统一再排一次
    fresh.sort(key=lambda x: x.published, reverse=True)
    return fresh
