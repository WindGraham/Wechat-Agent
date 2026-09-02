#!/bin/bash
# install.sh — 一键安装网关为系统服务：开机自启 + 崩溃自愈 + 常驻。
#
# 用法：
#   ./install.sh              安装并启动网关服务（默认 systemd user 服务）
#   ./install.sh --system     安装为系统服务（需要 sudo；开机自启更彻底）
#   ./install.sh uninstall    卸载服务
#   ./install.sh status       查看服务状态
#
# 设计要点：
#   - 网关是常驻控制平面：只托管一个网页，资源占用极小
#   - agent 的启动/停止/重启全部在网关网页"控制台"页完成，与网关服务无关
#   - KillMode=process：重启/崩溃拉起网关时**不杀 agent 子进程**——
#     agent 是独立进程组，网关重启后从 agent.pid 认领继续跟踪
#   - Restart=always：网关进程崩溃自动拉起
#
# 安装后：
#   1. 浏览器打开 http://127.0.0.1:13014/
#   2. 控制台页 → 启动 agent
#   3. 日常：改 prompt/人格/配置在网页完成；重启 agent 也在网页完成

set -e
cd "$(dirname "$0")"

PYTHON="$(pwd)/.venv/bin/python"
[ -x "$PYTHON" ] || { echo "venv 不存在: $PYTHON（项目内 .venv，见 docs/GATEWAY.md）" >&2; exit 1; }

SERVICE_NAME="wechat-agent-gateway"
ROOT="$(pwd)"

# 任务 CLI(kimi/opencode)所在目录，systemd 拉起时 shell PATH 不会带进来，
# 必须显式注入，否则 agent 任务子进程找不到 kimi（2026-08-10 实测）
CLI_BIN_DIR="$HOME/.kimi-code/bin:$HOME/.opencode/bin:$HOME/.local/bin"

install_user() {
    echo "==> 安装 systemd user 服务: $SERVICE_NAME"
    mkdir -p ~/.config/systemd/user
    cat > ~/.config/systemd/user/$SERVICE_NAME.service <<EOF
[Unit]
Description=Wechat-Agent Gateway (control plane, keep-alive)
After=network.target

[Service]
Type=simple
WorkingDirectory=$ROOT
ExecStart=$PYTHON -m src.gateway
Restart=always
RestartSec=3
# 任务 CLI(kimi)目录：systemd 不加载 shell PATH，显式注入（2026-08-10 实测）
Environment=PATH=$CLI_BIN_DIR:/usr/local/bin:/usr/bin
Environment=WECHAT_AGENT_GATEWAY_HOST=127.0.0.1
Environment=WECHAT_AGENT_GATEWAY_PORT=13014
# CV/BLAS 线程上限（默认 6 核；agent 入口 main.py 里还有一层保险）
Environment=OMP_NUM_THREADS=6
Environment=OPENBLAS_NUM_THREADS=6
Environment=MKL_NUM_THREADS=6
Environment=NUMEXPR_NUM_THREADS=6
Environment=VECLIB_MAXIMUM_THREADS=6
# 只杀网关主进程，不连带 agent 子进程（agent 由网关控制台管理，独立存活）
KillMode=process
# 可选：网关鉴权 token（设置后网页需 Authorization）
# Environment=WECHAT_AGENT_GATEWAY_TOKEN=your_token_here

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable $SERVICE_NAME
    systemctl --user restart $SERVICE_NAME
    # 允许未登录时也运行（headless 常驻关键）
    loginctl enable-linger "$USER" 2>/dev/null || true
    echo "==> 已启用开机自启（user 服务 + linger）"
}

install_system() {
    echo "==> 安装 systemd 系统服务: $SERVICE_NAME（需要 sudo）"
    sudo tee /etc/systemd/system/$SERVICE_NAME.service > /dev/null <<EOF
[Unit]
Description=Wechat-Agent Gateway (control plane, keep-alive)
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$ROOT
ExecStart=$PYTHON -m src.gateway
Restart=always
RestartSec=3
# 任务 CLI(kimi)目录：systemd 不加载 shell PATH，显式注入（2026-08-10 实测）
Environment=PATH=$CLI_BIN_DIR:/usr/local/bin:/usr/bin
Environment=WECHAT_AGENT_GATEWAY_HOST=127.0.0.1
Environment=WECHAT_AGENT_GATEWAY_PORT=13014
# CV/BLAS 线程上限（默认 6 核；agent 入口 main.py 里还有一层保险）
Environment=OMP_NUM_THREADS=6
Environment=OPENBLAS_NUM_THREADS=6
Environment=MKL_NUM_THREADS=6
Environment=NUMEXPR_NUM_THREADS=6
Environment=VECLIB_MAXIMUM_THREADS=6
KillMode=process
# Environment=WECHAT_AGENT_GATEWAY_TOKEN=your_token_here

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable $SERVICE_NAME
    sudo systemctl restart $SERVICE_NAME
    echo "==> 已启用开机自启（系统服务）"
}

uninstall() {
    echo "==> 卸载服务: $SERVICE_NAME"
    systemctl --user stop $SERVICE_NAME 2>/dev/null || true
    systemctl --user disable $SERVICE_NAME 2>/dev/null || true
    rm -f ~/.config/systemd/user/$SERVICE_NAME.service
    systemctl --user daemon-reload 2>/dev/null || true
    echo "==> 已卸载（网关进程已停止；agent 若在跑不受影响，可从 agent.pid 找回）"
}

case "${1:-}" in
  uninstall) uninstall ;;
  status)
    systemctl --user status $SERVICE_NAME --no-pager 2>&1 | head -15
    echo "----"
    echo "网页: http://127.0.0.1:13014/（控制台页管理 agent）"
    ;;
  --system) install_system ;;
  *) install_user ;;
esac
