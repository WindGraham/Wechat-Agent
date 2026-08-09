#!/bin/bash
# restart.sh — 轮转日志后重启 agent（保留 agent.log.1 供事后排查，
# 直接 > logs/agent.log 会把现场冲掉，2026-08-09 教训）
set -e
cd "$(dirname "$0")"

pkill -f "[s]rc.main" 2>/dev/null || true
sleep 2
[ -f logs/agent.log ] && mv logs/agent.log "logs/agent.log.$(date +%m%d-%H%M%S)"
# 只保留最近 5 个历史日志
ls -t logs/agent.log.* 2>/dev/null | tail -n +6 | xargs -r rm -f
setsid nohup ./run.sh > logs/agent.log 2>&1 < /dev/null &
sleep 8
ps aux | grep "[s]rc.main" | awk '{print "restarted, PID", $2}' | head -1
