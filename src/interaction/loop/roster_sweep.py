# -*- coding: utf-8 -*-
"""roster_sweep.py — 凌晨3点休眠后的花名册批量扫描。

理想态：夜里3点猫猫休眠后，检测所有会话列表，对「信息尚未获取」的会话
（新进群 / 新加好友）逐个自动进入并爬取，爬完打标；无新增则什么都不做。

与主循环互斥：扫描期间占用手机，用 shared `maintenance` Event 通知
InteractionLoop 暂停正常分发/乱逛（=「休眠」）；扫描结束释放 Event。
"""

import logging
import threading
import time

log = logging.getLogger("interaction.roster_sweep")

ROSTER_SWEEP_HOUR = 3     # 每天凌晨3点
CHECK_INTERVAL = 300      # 每5分钟查一次是否到点


def _today_key(ts=None):
    return time.strftime("%Y-%m-%d", time.localtime(ts or time.time()))


def should_sweep(now_ts, last_sweep_day, hour=ROSTER_SWEEP_HOUR):
    """是否到了应扫描的时点：已过今天的 hour 点 且 今天还没扫过。

    用于 RosterSweep 内部轮询，也暴露为纯函数便于单测。
    """
    lt = time.localtime(now_ts)
    if lt.tm_hour < hour:
        return False
    return _today_key(now_ts) != last_sweep_day


class RosterSweep:
    """凌晨3点花名册扫描线程（daemon）。"""

    def __init__(self, db_path, maintenance: threading.Event = None,
                 sweep_hour=ROSTER_SWEEP_HOUR, check_interval=CHECK_INTERVAL):
        self._db_path = db_path
        self._maintenance = maintenance or threading.Event()
        self._sweep_hour = sweep_hour
        self._check_interval = check_interval
        self._last_sweep_day = None
        self._stop = threading.Event()
        self._sleep = time.sleep
        self._clock = time.time

    # ------------------------------------------------------------------ 线程主体
    def run_forever(self):
        log.info("roster sweep thread start (hour=%d, interval=%ds)",
                 self._sweep_hour, self._check_interval)
        while not self._stop.is_set():
            try:
                if should_sweep(self._clock(), self._last_sweep_day,
                                self._sweep_hour):
                    summary = self.sweep_once()
                    log.info("roster sweep done: %s", summary)
            except Exception:
                log.exception("roster sweep 周期异常")
            self._stop.wait(self._check_interval)

    def sweep_once(self):
        """执行一次扫描：休眠（占用设备）→ 逐个爬取 → 打标 → 释放。返回摘要。

        独立成方法便于手工触发 / 单测（不依赖线程循环）。
        """
        if self._maintenance.is_set():
            return {"ok": False, "error": "上一次扫描仍在进行（maintenance 未释放）"}
        self._maintenance.set()
        try:
            summary = self._do_sweep()
        finally:
            self._maintenance.clear()
            self._last_sweep_day = _today_key()
        return summary

    # ------------------------------------------------------------------ 扫描本体
    def _do_sweep(self):
        from ..msglog import message_log
        conn = message_log.connect(self._db_path)
        try:
            pending = message_log.list_sessions_needing_roster(conn)
        finally:
            conn.close()

        if not pending:
            log.info("roster sweep: 无未获取会话，跳过")
            return {"ok": True, "scanned": 0, "crawled": [], "errors": []}

        import sys
        sys.path.insert(0, "scripts")
        import run_full_group_spider as spider_mod

        crawled, errors = [], []
        for s in pending:
            name, is_group = s["name"], bool(s["is_group"])
            log.info("roster sweep: 爬取 %s (is_group=%s)", name, is_group)
            try:
                r = (spider_mod.crawl_group_roster(name) if is_group
                     else spider_mod.crawl_private_roster(name))
                (crawled if r.get("ok") else errors).append(
                    {"name": name, "is_group": is_group, "result": r})
            except Exception as e:  # noqa: BLE001
                log.exception("roster sweep: %s 爬取异常", name)
                errors.append({"name": name, "is_group": is_group,
                               "result": {"ok": False, "error": str(e)}})
        return {"ok": True, "scanned": len(pending),
                "crawled": crawled, "errors": errors}

    def stop(self):
        self._stop.set()


# ------------------------------------------------------------------ 手动触发 CLI
if __name__ == "__main__":
    import argparse
    import sys

    p = argparse.ArgumentParser(description="花名册扫描：列出未获取会话 / 手动触发")
    p.add_argument("--db", default="workspace/chatlogs/chatlog.db",
                   help="chatlog.db 路径")
    p.add_argument("--list", action="store_true", help="只列出「信息未获取」的会话，不爬取")
    p.add_argument("--now", action="store_true", help="立即执行一次扫描（不等到凌晨3点）")
    a = p.parse_args()

    if a.list:
        sys.path.insert(0, ".")
        from ..msglog import message_log
        conn = message_log.connect(a.db)
        try:
            pending = message_log.list_sessions_needing_roster(conn)
            print(f"未获取会话共 {len(pending)} 个：")
            for s in pending:
                print(f"  [{'群' if s['is_group'] else '私'}] {s['name']}")
        finally:
            conn.close()
    elif a.now:
        rs = RosterSweep(db_path=a.db)
        print(rs.sweep_once())
    else:
        p.print_help()
