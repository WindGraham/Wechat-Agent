# -*- coding: utf-8 -*-
"""python -m src.gateway — 启动网关管理面。

默认只绑 127.0.0.1:13014；端口/地址可用环境变量覆盖：
    WECHAT_AGENT_GATEWAY_PORT   端口（默认 13014）
    WECHAT_AGENT_GATEWAY_HOST   绑定地址（默认 127.0.0.1）
    WECHAT_AGENT_GATEWAY_TOKEN  设置后要求 Authorization: Bearer <token>
"""

import logging
import os
import sys

from .app import create_app

DEFAULT_PORT = 13014


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    host = os.environ.get("WECHAT_AGENT_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("WECHAT_AGENT_GATEWAY_PORT", DEFAULT_PORT))
    app = create_app()
    print(f"[gateway] 网关管理面 http://{host}:{port}/ "
          f"(token={'on' if os.environ.get('WECHAT_AGENT_GATEWAY_TOKEN') else 'off'})",
          flush=True)
    app.run(host=host, port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
