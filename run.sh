#!/bin/bash
# Wechat-Agent 启动脚本：用户唯一入口——只启动网关（常驻控制平面）。
#
# 用法：
#   ./run.sh              前台运行网关（Ctrl+C 退出）
#   ./run.sh -d           后台运行网关（日志 logs/gateway.log）
#   ./run.sh stop         停止后台网关
#
# 网关起来后，浏览器打开 http://127.0.0.1:13014/ ，
# agent 的启动/停止/重启/日志全在网页"控制台"页完成。
set -e
cd "$(dirname "$0")"

PYTHON=~/.venvs/wechat-agent/bin/python
[ -x "$PYTHON" ] || { echo "venv 不存在: $PYTHON" >&2; exit 1; }

# 依赖快速自检（缺啥报啥，不要等 import 崩溃）
"$PYTHON" - <<'EOF'
import importlib.util, sys
missing = [m for m in ("jieba", "cv2", "numpy", "requests", "yaml", "flask")
           if not importlib.util.find_spec(m)]
if missing:
    sys.exit(f"缺少依赖: {', '.join(missing)}\n"
             f"安装: ~/.venvs/wechat-agent/bin/pip install {' '.join(missing)}")
EOF

mkdir -p logs

if [ "$1" = "-d" ]; then
    setsid nohup "$PYTHON" -m src.gateway > logs/gateway.log 2>&1 < /dev/null &
    sleep 1
    echo "网关已后台启动：日志 logs/gateway.log"
    echo "浏览器打开 http://127.0.0.1:13014/ → 控制台页启动 agent"
elif [ "$1" = "stop" ]; then
    pkill -f "[s]rc.gateway" 2>/dev/null && echo "网关已停止" || echo "网关未在运行"
else
    exec "$PYTHON" -m src.gateway "$@"
fi
