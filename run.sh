#!/bin/bash
# Wechat-Agent 启动脚本：用户唯一入口——启动网关（常驻控制平面）。
#
# 用法：
#   ./run.sh              前台运行网关（Ctrl+C 退出）
#   ./run.sh -d           后台运行网关（日志 logs/gateway.log）
#   ./run.sh stop         停止后台网关
#   ./install.sh          一键安装为 systemd 服务（开机自启 + 崩溃自愈，推荐）
#
# 网关起来后，浏览器打开 http://127.0.0.1:13014/ ，
# agent 的启动/停止/重启/日志全在网页"控制台"页完成。
# 注意：agent 本身由网关管理，本脚本不直接启动 agent。
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

# 检测是否已有网关在运行，避免双进程抢 13014 端口（启动入口不唯一的坑）。
# 若网关已由 systemd 服务（wechat-agent-gateway）托管，请用 systemctl，不要用本脚本。
if pgrep -f "[s]rc.gateway" >/dev/null 2>&1; then
    echo "检测到已有网关在运行：" >&2
    pgrep -af "[s]rc.gateway" >&2
    echo "若它是 systemd 服务，请用 systemctl --user restart wechat-agent-gateway 管理；" >&2
    echo "否则先 ./run.sh stop 再启动。" >&2
    exit 1
fi

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
