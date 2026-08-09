#!/bin/bash
# restart.sh — 重启网关进程（轮转 logs/gateway.log 后重新拉起）。
#
# 注意：agent（python -m src.main）的重启不再由本脚本负责——
# 它由网关网页"控制台"页管理（启动/停止/重启/看日志）。
set -e
cd "$(dirname "$0")"

pkill -f "[s]rc.gateway" 2>/dev/null || true
sleep 2
[ -f logs/gateway.log ] && mv logs/gateway.log "logs/gateway.log.$(date +%m%d-%H%M%S)"
# 只保留最近 5 个历史日志
ls -t logs/gateway.log.* 2>/dev/null | tail -n +6 | xargs -r rm -f
setsid nohup ./run.sh > logs/gateway.log 2>&1 < /dev/null &
sleep 2
ps aux | grep "[s]rc.gateway" | awk '{print "gateway restarted, PID", $2}' | head -1
