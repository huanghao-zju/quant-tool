#!/usr/bin/env python3
"""探测本机可用的科学上网出口，并验证彭博社是否可达。

用法：
    /usr/local/bin/python3 scripts/probe_proxy.py

干三件事：
1. 读 macOS 系统代理设置（scutil --proxy）
2. 扫常见代理客户端端口（Clash / Surge / V2Ray / Shadowsocks）
3. 对每个候选出口实测 bloomberg.com，验证能不能真拿到内容

最后打印一段可直接粘贴进 config.yaml 的配置。
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

# 常见代理客户端默认端口 -> 客户端名
CANDIDATE_PORTS = {
    7890: "Clash / ClashX (http)",
    7897: "Clash Verge (http)",
    7891: "Clash (socks)",
    1080: "Shadowsocks / V2Ray (socks)",
    1087: "Surge / V2RayN (http)",
    1086: "Surge (socks)",
    6152: "Surge (http)",
    6153: "Surge (socks)",
    8888: "Charles / mitmproxy",
    8080: "通用 http 代理",
    9090: "Clash 外部控制",
    10808: "v2rayN (socks)",
    10809: "v2rayN (http)",
    33210: "Quantumult X",
}

TEST_URLS = [
    ("彭博社", "https://www.bloomberg.com/"),
    ("Google", "https://www.google.com/generate_204"),
]

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def system_proxy() -> dict:
    """解析 scutil --proxy，返回已启用的系统代理。"""
    try:
        out = subprocess.run(
            ["scutil", "--proxy"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}

    info: dict = {}
    for enable_key, port_key, scheme in (
        ("HTTPSEnable", "HTTPSPort", "https"),
        ("HTTPEnable", "HTTPPort", "http"),
        ("SOCKSEnable", "SOCKSPort", "socks5"),
    ):
        if re.search(rf"{enable_key}\s*:\s*1", out):
            port_match = re.search(rf"{port_key}\s*:\s*(\d+)", out)
            host_match = re.search(
                rf"{scheme.upper()}Proxy\s*:\s*([\d.]+)", out, re.IGNORECASE
            )
            if port_match:
                info[scheme] = {
                    "host": host_match.group(1) if host_match else "127.0.0.1",
                    "port": int(port_match.group(1)),
                }
    if re.search(r"ProxyAutoConfigEnable\s*:\s*1", out):
        info["pac"] = True
    return info


def probe(url: str, proxy: str | None, timeout: float = 12.0) -> tuple[bool, str]:
    """返回 (是否成功, 说明)。成功定义为 HTTP 200。"""
    handler = (
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        if proxy
        else urllib.request.ProxyHandler({})  # 显式禁用环境变量代理
    )
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    started = time.time()
    try:
        with opener.open(req, timeout=timeout) as resp:
            elapsed = int((time.time() - started) * 1000)
            return resp.status == 200, f"HTTP {resp.status}  {elapsed}ms"
    except urllib.error.HTTPError as exc:
        elapsed = int((time.time() - started) * 1000)
        # 403/401 说明网络通了，只是被站点拒绝 —— 出口是有效的
        reachable = exc.code in (401, 403, 429)
        note = f"HTTP {exc.code}  {elapsed}ms"
        if reachable:
            note += "（网络已通，站点拦截爬虫）"
        return reachable, note
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    print("=" * 66)
    print("1. 系统代理设置")
    print("=" * 66)
    sys_proxy = system_proxy()
    if sys_proxy:
        for key, val in sys_proxy.items():
            print(f"  {key:<8} {val}")
    else:
        print("  未启用任何系统代理")

    print()
    print("=" * 66)
    print("2. 扫描本地代理端口")
    print("=" * 66)
    open_ports = []
    for port, name in CANDIDATE_PORTS.items():
        if port_open(port):
            open_ports.append(port)
            print(f"  ✓ {port:<6} {name}")
    if not open_ports:
        print("  没有发现监听的代理端口")
        print("  → VPN 客户端没开，或者用的是 TUN/全局模式（不占本地端口）")

    print()
    print("=" * 66)
    print("3. 实测出口能否访问彭博社")
    print("=" * 66)

    candidates: list[tuple[str, str | None]] = [("直连（TUN/全局模式检测）", None)]
    for port in open_ports:
        candidates.append((f"http://127.0.0.1:{port}", f"http://127.0.0.1:{port}"))
    for scheme in ("https", "http", "socks5"):
        cfg = sys_proxy.get(scheme)
        if cfg:
            url = f"{scheme}://{cfg['host']}:{cfg['port']}"
            if not any(c[0] == url for c in candidates):
                candidates.append((f"系统代理 {url}", url))

    winners: list[str] = []
    for label, proxy in candidates:
        print(f"\n  [{label}]")
        ok_any = False
        for site, url in TEST_URLS:
            ok, note = probe(url, proxy)
            mark = "✓" if ok else "✗"
            print(f"    {mark} {site:<8} {note}")
            if ok and site == "彭博社":
                ok_any = True
        if ok_any:
            winners.append(label)

    print()
    print("=" * 66)
    print("结论")
    print("=" * 66)
    if not winners:
        print("  ✗ 当前没有任何出口能访问彭博社。")
        print()
        print("  下一步：")
        print("    1. 打开你的 VPN 客户端，确认是「全局/增强模式」或已开启 TUN")
        print("    2. 如果是「规则模式」，把 bloomberg.com 加进代理规则")
        print("    3. 重跑本脚本")
        return 1

    print(f"  ✓ 可用出口：{', '.join(winners)}")
    print()
    print("  建议写入 config.yaml 的 news 段：")
    print()
    if winners[0].startswith("直连"):
        print("  news:")
        print("    proxy: null          # TUN/全局模式，无需显式代理")
    else:
        proxy_url = winners[0].split("系统代理 ")[-1]
        print("  news:")
        print(f"    proxy: \"{proxy_url}\"")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
