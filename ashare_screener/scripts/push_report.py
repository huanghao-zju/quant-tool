"""把已生成的 Markdown 简报推送到飞书群机器人。

用法：
    python3 scripts/push_report.py --md reports/daily_2026-11-08.md

webhook 从环境变量 PUSH_WEBHOOK_URL 读（CI 里由 secrets 注入），
签名校验用 PUSH_WEBHOOK_SECRET（可选）。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def sign(secret: str, timestamp: int) -> str:
    digest = hmac.new(f"{timestamp}\n{secret}".encode(), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", required=True, help="Markdown 简报路径")
    ap.add_argument("--webhook", default=os.environ.get("PUSH_WEBHOOK_URL", ""))
    ap.add_argument("--secret", default=os.environ.get("PUSH_WEBHOOK_SECRET", ""))
    args = ap.parse_args()

    md_path = Path(args.md)
    if not md_path.is_absolute():
        md_path = ROOT / md_path
    if not md_path.exists():
        print(f"报告不存在：{md_path}")
        return 1
    markdown = md_path.read_text(encoding="utf-8")

    if not args.webhook:
        print("未配置 PUSH_WEBHOOK_URL，跳过推送")
        return 0

    payload: dict = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "elements": [{"tag": "div",
                          "text": {"tag": "lark_md", "content": markdown[:4000]}}],
        },
    }
    if args.secret:
        ts = int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"] = sign(args.secret, ts)

    req = urllib.request.Request(
        args.webhook, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", "ignore")
        print(f"飞书推送结果：{body[:300]}")
        return 0 if ("code\":0" in body or "StatusCode\":0" in body) else 1
    except Exception as e:
        print(f"飞书推送失败：{e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
