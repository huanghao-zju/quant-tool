#!/bin/bash
# launchd 每日调用的数据更新入口。由 com.huanghao.ashare-screener-update.plist 触发。
# 单独写成脚本而不是塞进 plist 的 ProgramArguments，避免 shell 引号在 plist 里被转义破坏。
set -o pipefail

cd /Users/huanghao/project/quant-tool/ashare_screener || exit 1

echo "=== 开始 $(date '+%F %T') ==="

/usr/local/bin/python3 -m screener update
rc=$?

echo "=== 结束 $(date '+%F %T') 退出码 $rc ==="

# 更新成功时重启 UI 界面，让它丢掉 st.cache_data 的旧缓存
if [ $rc -eq 0 ]; then
  launchctl kickstart -k "gui/$(id -u)/com.huanghao.ashare-screener-ui" 2>/dev/null \
    && echo "已重启 UI 界面" || echo "UI 未注册，跳过重启"
fi

exit $rc