# -*- coding: utf-8 -*-
"""决策层仿真测试（假交互层 + 真 k3）：验证 Proxy 进出全链路。

场景：
  A. 群聊普通消息 → 合法 XML 输出（reply 或 silent）
  B. 群聊 @我 → 必须回复
  C. 主人私聊 → 必须回复
  D. 任务请求 → <task> 块 → CLI 后端 → 任务回执 → 汇报回复
"""

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.shared.types import Message, LogUpdated                    # noqa: E402
from src.decision.proxy import Proxy                                # noqa: E402
from src.decision.provider import create_provider                   # noqa: E402
from src.shared.runtime import RuntimeConfig                        # noqa: E402


class FakeReader:
    """假交互层读取：内存消息库。"""

    def __init__(self, session, is_group, history, new_msgs):
        self._session = session
        self._is_group = is_group
        self._history = history
        self._new = new_msgs
        self.writes = []

    def get_new_since(self, session, last_seq):
        return [m for m in self._new if m.seq > last_seq]

    def get_context(self, session, n=200):
        return (self._history + self._new)[-n:]

    def last_is_group(self, session):
        return self._is_group

    def update_content(self, session, sender, content, new_content):
        self.writes.append((session, sender, new_content))
        return 1


class FakeResult:
    ok = True
    error = None
    retryable = True
    escalation_hint = None


def make_msg(session, sender, content, seq, is_group=True, **kw):
    return Message(session=session, is_group=is_group, sender=sender,
                   is_mine=False, content=content, content_type="text",
                   seq=seq, **kw)


def run_scenario(name, provider, session, is_group, new_msgs,
                 history=None, mention=False):
    tmp = tempfile.mkdtemp()
    submitted = []
    reader = FakeReader(session, is_group, history or [], new_msgs)
    proxy = Proxy(
        provider=provider, reader=reader,
        submit_bundle=lambda s, x: (submitted.append((s, x)),
                                    FakeResult())[-1],
        runtime=RuntimeConfig("config/runtime.json"),
        watermarks_path=os.path.join(tmp, "wm.json"),
        tasks_root=os.path.join(tmp, "tasks"))

    print(f"\n{'═' * 60}\n场景 {name} | 会话={session} 群={is_group}")
    print(f"新消息: {[m.content[:30] for m in new_msgs]}")

    t0 = time.time()
    proxy.notify_log_updated(LogUpdated(session=session, version=1,
                                        mention_hint=mention))
    proxy.run_once()
    dt = time.time() - t0

    print(f"耗时: {dt:.1f}s")
    print(f"发出 bundle 数: {len(submitted)}")
    for s, xml in submitted:
        print(f"  → [{s}] {xml[:200]}")
    return submitted


def main():
    provider = create_provider()
    print(f"provider: kimi/{provider.model}")

    # 历史（公共）
    history = [
        make_msg("特高课", "Leisure", "周末有人打球吗", 1),
        make_msg("特高课", "帽子女孩", "我可以来", 2),
    ]

    # A. 群聊普通闲聊
    run_scenario("A 群聊闲聊", provider, "特高课", True,
                 [make_msg("特高课", "Leisure", "今天天气真好啊", 3)])

    # B. 群聊 @我
    run_scenario("B 群聊@我", provider, "特高课", True,
                 [make_msg("特高课", "Leisure", "@陈曦 你觉得周末去哪儿", 3,
                           mentions=["陈曦"])],
                 mention=True)

    # C. 主人私聊
    run_scenario("C 主人私聊", provider, "风图", False,
                 [make_msg("风图", "风图", "在吗", 1, is_group=False)])

    # D. 任务请求（主人要一张图 → 期望 <task> 委派）
    run_scenario("D 任务请求", provider, "风图", False,
                 [make_msg("风图", "风图", "帮我总结一下刚才的聊天记录发给我", 1,
                           is_group=False)],
                 mention=True)

    print(f"\n{'═' * 60}\n仿真结束")


if __name__ == "__main__":
    main()
