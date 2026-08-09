# -*- coding: utf-8 -*-
"""python -m src.gateway — 独立运行网关管理面（常驻控制平面）。

用户唯一入口（配合 run.sh）：只启动网关，agent 的一切操作
（启动/停止/重启/看日志）都在网关网页的"控制台"页完成。

特性：
  - 独立常驻进程：agent 退出不影响网关（agent 是其子进程，supervisor 管理）
  - 热重启：改 src/gateway/*.py 自动 reload 重建 server，无需重启网关进程
  - 默认**不自动拉起 agent**——用户进入网关后手动点"启动"

环境变量：
    WECHAT_AGENT_GATEWAY_HOST      绑定地址（默认 127.0.0.1）
    WECHAT_AGENT_GATEWAY_PORT      端口（默认 13014）
    WECHAT_AGENT_GATEWAY_TOKEN     设置后要求 Authorization: Bearer <token>
    WECHAT_AGENT_PYTHON            覆盖 agent 解释器路径（默认 ~/.venvs/...）
    WECHAT_AGENT_WORKSPACE         传给 agent 的 --workspace（默认不传）
    WECHAT_AGENT_CONFIG            传给 agent 的 --config（默认不传）
"""

import logging
import os
import sys

from .hot_reload import HotReloadServer
from .supervisor import AgentSupervisor, DEFAULT_PYTHON

DEFAULT_PORT = 13014
DEFAULT_HOST = "127.0.0.1"


def _create_supervisor(root):
    """按环境变量构造 AgentSupervisor（agent 解释器/工作区/配置可覆盖）。"""
    python = os.environ.get("WECHAT_AGENT_PYTHON", DEFAULT_PYTHON)
    return AgentSupervisor(
        python=python,
        workspace=os.environ.get("WECHAT_AGENT_WORKSPACE") or None,
        config=os.environ.get("WECHAT_AGENT_CONFIG") or None,
        logs_dir=os.path.join(root, "logs"),
    )


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    host = os.environ.get("WECHAT_AGENT_GATEWAY_HOST", DEFAULT_HOST)
    port = int(os.environ.get("WECHAT_AGENT_GATEWAY_PORT", DEFAULT_PORT))

    # 仓库根：本文件在 src/gateway/ 下，向上三级
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))

    supervisor = _create_supervisor(root)
    agent_port = 13015  # agent 侧 task_done 回调端口（约定）
    callback = f"http://127.0.0.1:{agent_port}"

    def create_factory():
        """每次（含热重启）重建 app；supervisor 常驻不随 reload 变化。"""
        from .app import create_app
        return create_app(project_root=root, supervisor=supervisor,
                          agent_callback_url=callback)

    server = HotReloadServer(create_factory, host=host, port=port)
    print(f"[gateway] 网关管理面 http://{host}:{port}/ "
          f"(token={'on' if os.environ.get('WECHAT_AGENT_GATEWAY_TOKEN') else 'off'})",
          flush=True)
    print("[gateway] agent 由网关控制台管理：进入网页 → 控制台 → 启动/停止/重启",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
