#!/bin/bash
# 彭博简报推送入口，由 com.huanghao.ashare-news.plist 每 20 分钟唤醒一次。
#
# 这里不做时间判断 —— 该不该推、推早报还是晚报，全由 python 那边按 config.yaml
# 的 windows 决定。脚本只负责：切目录、拿锁、跑、记日志。
set -o pipefail

cd /Users/huanghao/project/quant-tool/ashare_screener || exit 1

LOCK="/tmp/ashare-news-push.lock"

# 防重入：StartInterval 在机器休眠唤醒后可能补触发，两次执行撞在一起会重复推送。
# mkdir 是原子的，比判断 pid 文件可靠。
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "[$(date '+%F %T')] 上一轮还在跑，跳过"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

/usr/local/bin/python3 -m screener.news
rc=$?

exit $rc
