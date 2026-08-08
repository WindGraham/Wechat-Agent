#!/bin/bash
# Wechat-Agent 启动脚本：锁定项目 venv（系统 python 缺依赖）。
# 用法：./run.sh            前台运行（Ctrl+C 退出）
#       ./run.sh --dry-run  只装配不启动（验证用）
#       ./run.sh --once     跑一轮就退出
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

exec "$PYTHON" -m src.main "$@"
