"""统一 HTTP 与容错基础设施（SPEC §6 编码约定）。"""
from __future__ import annotations

import io
import json
import logging
import os
import time
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger("fetchers")

FAULT_FILE = Path("fault_counts.json")


def _cfg():
    import yaml
    with open(Path(__file__).parents[2] / "config" / "thresholds.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def http_get(url: str, *, params=None, headers=None, timeout=30, retries=None) -> requests.Response | None:
    """带重试与代理支持的 GET。失败返回 None，不抛异常。"""
    retries = retries if retries is not None else _cfg()["fetch"]["max_retries"]
    proxies = None
    if os.environ.get("HTTPS_PROXY"):
        proxies = {"https": os.environ["HTTPS_PROXY"], "http": os.environ["HTTPS_PROXY"]}
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout, proxies=proxies)
            if r.status_code == 200:
                return r
            last_err = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last_err = repr(e)
        time.sleep(2 ** attempt)
    log.warning("GET failed after %d tries: %s (%s)", retries, url, last_err)
    return None


def csv_to_series(text: str, date_col: str, value_col: str) -> pd.Series | None:
    try:
        df = pd.read_csv(io.StringIO(text))
        df[date_col] = pd.to_datetime(df[date_col])
        s = pd.to_numeric(df.set_index(date_col)[value_col], errors="coerce").dropna()
        s.index.name = None
        return s.sort_index()
    except Exception as e:
        log.warning("csv parse failed: %r", e)
        return None


# ── 故障计数（SPEC §5.5 心跳）─────────────────────────

def load_faults() -> dict:
    if FAULT_FILE.exists():
        return json.loads(FAULT_FILE.read_text())
    return {}


def record_fetch(name: str, ok: bool) -> None:
    faults = load_faults()
    faults[name] = 0 if ok else faults.get(name, 0) + 1
    FAULT_FILE.write_text(json.dumps(faults, indent=1))


def broken_sources(threshold: int) -> list[str]:
    return [k for k, v in load_faults().items() if v >= threshold]
