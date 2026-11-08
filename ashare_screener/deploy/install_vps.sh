#!/bin/bash
# VPS 部署脚本（Linux + systemd）。在项目根目录执行：bash deploy/install_vps.sh
#
# 做四件事：建 venv、装依赖、装 systemd service/timer、启动定时器。
# 幂等，可以重复跑（改了代码后重跑只更新 unit 文件，不会重复建环境）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-python3}"
UNIT="ashare-news"

say() { printf '\033[36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31m!!  %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 前置检查

[ "$(uname -s)" = "Linux" ] || die "这个脚本只用于 Linux VPS（本机 macOS 请用 launchd 方案）"
[ -f "$ROOT/config.yaml" ] || die "找不到 $ROOT/config.yaml，确认是在项目目录里跑"
command -v "$PY" >/dev/null || die "找不到 $PY，先装 python3（apt install python3 python3-venv）"

say "项目目录：$ROOT"

# systemd 的 Python 需要 venv 模块，Debian/Ubuntu 上常要单独装
"$PY" -c 'import venv' 2>/dev/null || die "$PY 缺少 venv 模块，执行：sudo apt install python3-venv"

PYVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
say "Python 版本：$PYVER"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "需要 Python 3.9+（zoneinfo 依赖），当前 $PYVER"

# ---------------------------------------------------------------- 虚拟环境

if [ -d "$ROOT/.venv" ]; then
  say "复用已有 venv"
else
  say "创建 venv"
  "$PY" -m venv "$ROOT/.venv"
fi

say "安装依赖"
"$ROOT/.venv/bin/pip" install --quiet --upgrade pip
"$ROOT/.venv/bin/pip" install --quiet -r "$ROOT/requirements-news.txt"

# ---------------------------------------------------------------- systemd

say "安装 systemd 单元"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

sed "s|__ROOT__|$ROOT|g" "$ROOT/deploy/ashare-news.service" > "$TMP/$UNIT.service"
cp "$ROOT/deploy/ashare-news.timer" "$TMP/$UNIT.timer"

sudo cp "$TMP/$UNIT.service" "$TMP/$UNIT.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now "$UNIT.timer"

# ---------------------------------------------------------------- 自查

say "定时器状态"
systemctl list-timers "$UNIT.timer" --no-pager || true

echo
WEBHOOK="$("$ROOT/.venv/bin/python" - <<'EOF'
import yaml, pathlib
cfg = yaml.safe_load(pathlib.Path("config.yaml").read_text(encoding="utf-8")) or {}
print(((cfg.get("news") or {}).get("feishu") or {}).get("webhook") or "")
EOF
)"

if [ -z "$WEBHOOK" ]; then
  echo "⚠  config.yaml 里的 news.feishu.webhook 还是空的，现在什么都不会推。"
  echo "   填好之后验证一次："
  echo "     cd $ROOT && .venv/bin/python -m screener.news --test-webhook"
else
  say "webhook 已配置"
fi

cat <<EOF

装完了。常用命令：

  看下次触发时间     systemctl list-timers ashare-news.timer
  看日志             journalctl -u ashare-news.service -n 50 --no-pager
  实时跟日志         journalctl -u ashare-news.service -f
  手动立刻跑一次     cd $ROOT && .venv/bin/python -m screener.news --force
  只看会推什么       cd $ROOT && .venv/bin/python -m screener.news --preview
  停掉定时           sudo systemctl disable --now ashare-news.timer

注意：时区由 config.yaml 的 news.timezone 控制（默认 Asia/Shanghai），
与 VPS 系统时区无关。VPS 是 UTC 也没关系，别去改系统时区。

EOF
