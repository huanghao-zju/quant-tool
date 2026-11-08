"""飞书群机器人推送。

用标准库 urllib 发 POST，不引入 requests —— launchd 环境下少一个依赖就少一处失败点。
支持签名校验（群机器人开了「签名校验」时填 secret）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional

from .models import Digest, NewsItem

TEMPLATE_MORNING = "blue"
TEMPLATE_EVENING = "indigo"
TEMPLATE_ALERT = "red"

SLOT_TITLE = {
    "morning": "彭博社 · 中国市场早报",
    "evening": "彭博社 · 中国市场晚报",
}


class FeishuError(RuntimeError):
    pass


def _gen_sign(timestamp: int, secret: str) -> str:
    """飞书签名：以 "{timestamp}\\n{secret}" 为 HMAC key，消息体为空，再 base64。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _item_block(idx: int, item: NewsItem) -> str:
    """单条新闻的卡片文本。标题带链接，第二行是时间和板块。"""
    return f"**{idx}. [{item.title}]({item.url})**\n<font color='grey'>{item.published_str}</font>"


def _section(name: str, items: List[NewsItem], empty_hint: str) -> List[Dict]:
    elements: List[Dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**{name}**（{len(items)} 条）"}}
    ]
    if not items:
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": f"<font color='grey'>{empty_hint}</font>"}}
        )
        return elements

    for i, item in enumerate(items, 1):
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": _item_block(i, item)}})
    return elements


def build_card(digest: Digest) -> Dict:
    """组装飞书 interactive 卡片。"""
    title = SLOT_TITLE.get(digest.slot, "彭博社 · 市场速递")
    template = TEMPLATE_MORNING if digest.slot == "morning" else TEMPLATE_EVENING

    elements: List[Dict] = []
    elements += _section("中国市场", digest.china, "暂无符合条件的中文市场新闻")
    elements.append({"tag": "hr"})
    elements += _section("全球头条", digest.headline, "暂无")

    if digest.fallback_note:
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"<font color='red'>{digest.fallback_note}</font>"},
            }
        )

    elements.append(
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": (
                        f"数据源：Bloomberg sitemap（仅标题与链接，正文见付费墙） · "
                        f"生成于 {digest.generated.strftime('%Y-%m-%d %H:%M')}"
                    ),
                }
            ],
        }
    )

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True, "enable_forward": True},
            "header": {
                "template": template,
                "title": {
                    "tag": "plain_text",
                    "content": f"{title} · {digest.generated.strftime('%m-%d')}",
                },
            },
            "elements": elements,
        },
    }


def build_alert_card(text: str) -> Dict:
    """异常告警卡片，用于抓取失败且已无法补推时提醒用户。"""
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": TEMPLATE_ALERT,
                "title": {"tag": "plain_text", "content": "彭博简报推送失败"},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": text}},
            ],
        },
    }


def _post(webhook: str, payload: Dict, timeout: int = 15) -> Dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise FeishuError(f"飞书返回 HTTP {exc.code}：{detail}") from exc
    except Exception as exc:
        raise FeishuError(f"请求飞书失败（{type(exc).__name__}）：{exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FeishuError(f"飞书返回非 JSON：{raw[:200]}") from exc

    # 新版接口返回 code，老版返回 StatusCode，两种都认
    code = data.get("code", data.get("StatusCode", 0))
    if code != 0:
        msg = data.get("msg") or data.get("StatusMessage") or raw[:200]
        raise FeishuError(f"飞书拒收：{msg}")
    return data


def send(webhook: str, payload: Dict, secret: Optional[str] = None) -> Dict:
    """发送任意卡片/文本消息。secret 非空时自动附加签名。"""
    if not webhook:
        raise FeishuError("未配置飞书 webhook，请在 config.yaml 的 news.feishu.webhook 填入")

    if secret:
        ts = int(time.time())
        payload = dict(payload)
        payload["timestamp"] = str(ts)
        payload["sign"] = _gen_sign(ts, secret)

    return _post(webhook, payload)


def send_digest(webhook: str, digest: Digest, secret: Optional[str] = None) -> Dict:
    return send(webhook, build_card(digest), secret=secret)


def send_text(webhook: str, text: str, secret: Optional[str] = None) -> Dict:
    return send(webhook, {"msg_type": "text", "content": {"text": text}}, secret=secret)
