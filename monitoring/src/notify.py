"""推送模块（SPEC §5）。飞书群自定义机器人 webhook，失败重试3次并留痕。

环境变量：
- PUSH_WEBHOOK_URL     飞书机器人 webhook 地址
- PUSH_WEBHOOK_SECRET  可选；机器人开启"签名校验"时填写
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time

import requests

log = logging.getLogger("notify")

MAX_RETRIES = 3


def _sign(secret: str, timestamp: int) -> str:
    """飞书签名：HMAC-SHA256("{timestamp}\\n{secret}" 为 key，空串为 msg) → base64。"""
    key = f"{timestamp}\n{secret}".encode()
    return base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()


def push(markdown: str, dry_run: bool = False) -> bool:
    if dry_run:
        print("── DRY RUN 推送内容 ──\n" + markdown + "\n──────────────────")
        return True
    url = os.environ.get("PUSH_WEBHOOK_URL")
    if not url:
        log.error("PUSH_WEBHOOK_URL 未设置，推送跳过")
        return False
    # 飞书 interactive 卡片，lark_md 支持 **加粗** 等标记
    payload: dict = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "elements": [{"tag": "div",
                          "text": {"tag": "lark_md", "content": markdown[:4000]}}],
        },
    }
    secret = os.environ.get("PUSH_WEBHOOK_SECRET")
    if secret:
        ts = int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"] = _sign(secret, ts)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(url, json=payload, timeout=15)
            body = r.json() if r.status_code == 200 else {}
            # 新版返回 {"code":0}，旧版 {"StatusCode":0}
            if r.status_code == 200 and (body.get("code") == 0 or body.get("StatusCode") == 0):
                log.info("推送成功（第%d次尝试）", attempt)
                return True
            log.warning("推送失败 attempt=%d status=%s body=%s", attempt, r.status_code, r.text[:200])
        except (requests.RequestException, ValueError) as e:
            log.warning("推送异常 attempt=%d err=%r", attempt, e)
        time.sleep(2 ** attempt)
    log.error("推送最终失败（已重试%d次）", MAX_RETRIES)
    return False


# ── 文案模板（SPEC §5.3）──────────────────────────────

def transition_msg(tr, ev, actions: dict) -> str:
    key = {(0, 1): "green_to_yellow", (1, 2): "yellow_to_orange", (2, 3): "orange_to_red"}
    if tr.kind == "downgrade":
        action = actions.get("downgrade", "")
        head = f"**[阶段迁移·降级] {tr.label}**"
        cond = f"连续静默期满，降级（{tr.date}）"
    else:
        action = actions.get(key.get((tr.from_stage, tr.to_stage),
                                     "orange_to_red" if tr.to_stage == 3 else "yellow_to_orange"), "")
        head = f"**[阶段迁移] {tr.label}**"
        cond = "；".join(tr.conditions)
    lines = [head,
             f"触发条件：{cond}",
             "当前读数：" + " | ".join(f"{k} {v}" for k, v in list(ev.readings.items())[:8]),
             f"➤ 你的既定动作：{action.strip()}",
             f"慢变量看板分：{ev.dashboard_score}",
             f"数据时间戳：{ev.date.date()}"]
    return "\n".join(lines)


def weekly_msg(ev, stage: int, stage_name: str, broken: list[str]) -> str:
    lines = [f"**[周报] 当前阶段：{stage_name}（{stage}）** | 看板分 {ev.dashboard_score}",
             "指标现值："]
    lines += [f"- {k}：{v}" for k, v in ev.readings.items()]
    hit = [k for k, v in ev.dashboard.items() if v]
    lines.append("看板命中：" + ("、".join(hit) if hit else "无"))
    if broken:
        lines.append(f"⚠️ 数据源故障：{'、'.join(broken)}")
    lines.append(f"数据时间戳：{ev.date.date()}")
    return "\n".join(lines)


def fault_msg(broken: list[str]) -> str:
    return "**[数据源故障]** 以下数据源连续3天拉取失败：" + "、".join(broken)
