# -*- coding: utf-8 -*-
"""scripts/dedup_roster.py — 花名册去重（昵称部分匹配 + 头像相似）。

判据（用户规则）：昵称「部分字段」匹配 且 头像相同/高度相似 → 判重复，只保留第一条。
- 单独同名但头像不同 = 不同的人，保留（不误删同名不同人）；
- 单独头像相同但昵称不同 = 不清理（严格两条都满足才清）。

头像相似用 dHash（64 位差值哈希）汉明距离 ≤ 阈值。
「未知昵称」成员退化为用群昵称/头像文件名参与昵称匹配。
"""
import argparse
import json
import os
import sys

import cv2

sys.path.insert(0, ".")
from src.interaction.msglog.message_log import normalize

DEFAULT_HAMMING = 8   # 64 位 dHash 允许 ≤8 位差异 = 高度相似


def dhash(img, size=8):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    r = cv2.resize(g, (size + 1, size))
    diff = r[:, 1:] > r[:, :-1]
    return int("".join("1" if b else "0" for b in diff.flatten()), 2)


def hamming(a, b):
    return bin(a ^ b).count("1")


def dedup_name(m):
    nn = (m.get("main_nickname") or "").strip()
    if nn and nn != "未知昵称":
        return nn
    gn = (m.get("group_nickname") or "").strip()
    if gn:
        return gn
    av = (m.get("avatar_image_path") or "").replace("avatars/", "").replace(".png", "")
    return av


def nick_partial(a, b):
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    return na in nb or nb in na


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("group", nargs="?", default="被打信科2026游泳馆")
    ap.add_argument("--hamming", type=int, default=DEFAULT_HAMMING)
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写回")
    a = ap.parse_args()

    path = f"workspace/group_rosters/{a.group}/members_roster.json"
    if not os.path.exists(path):
        print("不存在:", path)
        return
    data = json.load(open(path))
    members = data.get("members", [])

    hashes = []
    for m in members:
        av = m.get("avatar_image_path", "")
        full = os.path.join("workspace/group_rosters", a.group, av) if av else ""
        h = None
        if full and os.path.exists(full):
            img = cv2.imread(full)
            if img is not None:
                h = dhash(img)
        hashes.append(h)

    kept, removed = [], []
    for i, m in enumerate(members):
        dup = False
        for j in kept:
            if nick_partial(dedup_name(m), dedup_name(members[j])):
                if hashes[i] is not None and hashes[j] is not None \
                        and hamming(hashes[i], hashes[j]) <= a.hamming:
                    dup = True
                    break
        if dup:
            removed.append(m)
        else:
            kept.append(i)

    print(f"{a.group}: 原 {len(members)} 条 → 去重后 {len(kept)} 条（移除 {len(removed)} 条）")
    if a.dry_run:
        for m in removed[:30]:
            print(f"  移除: {dedup_name(m)!r}  ({m.get('avatar_image_path')})")
        return

    if removed:
        bak = path + ".bak"
        if not os.path.exists(bak):
            json.dump(data, open(bak, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
        new_members = [members[j] for j in kept]
        data["members"] = new_members
        data["total_count"] = len(new_members)
        json.dump(data, open(path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"已写回（原文件备份在 {bak}）")


if __name__ == "__main__":
    main()
