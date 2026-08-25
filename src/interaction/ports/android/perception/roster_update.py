# -*- coding: utf-8 -*-
"""roster_update.py — 花名册调和与更新（纯函数，可单测）。

实现用户 spec 的「改名/换头像」调和规则，以及私信联系人/好友通过的存档。
本模块只负责花名册数据的判定与写回，不碰设备；头像裁剪沿用现有实现
（run_full_group_spider.crop_exact_rounded_avatar / find_profile_avatar_box）。

调和规则（用户 2026-08-13 spec）：
  1. 改了群昵称、头像+实际昵称匹配 → 更新 group_nickname，加「曾用群昵称」。
  2. 群昵称+昵称匹配、头像变了 → 加一个「曾用头像」。
  3. 无群昵称、直接改昵称 → 看是否与花名册他人冲突，不冲突则更新 main_nickname。
  4. 无群昵称、直接改头像 → 看是否与花名册他人冲突，不冲突则加一个头像。
后续识别：曾用群昵称 + 曾用头像 都纳入双因子匹配（roster_matcher 已支持）。
"""

import json
import os
import time

from ....msglog.message_log import normalize

# 花名册根目录（与 roster_matcher 一致）
PROJECT_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", ".."))
ROSTERS_DIR = os.path.join(PROJECT_ROOT, "workspace", "group_rosters")


def _nick_eq(a, b):
    na, nb = normalize(a or ""), normalize(b or "")
    return bool(na) and bool(nb) and (na in nb or nb in na)


def _member_uses_avatar(m, avatar_path):
    if not avatar_path:
        return False
    if (m.get("avatar_image_path") or "") == avatar_path:
        return True
    return avatar_path in (m.get("former_avatars") or [])


def _nick_in_use(members, exclude_member, value):
    """value 是否被其它成员的主昵称/群昵称占用（冲突检查）。"""
    for m in members:
        if m is exclude_member:
            continue
        if _nick_eq(m.get("main_nickname", ""), value):
            return True
        if _nick_eq(m.get("group_nickname", ""), value):
            return True
    return False


def _avatar_in_use(members, exclude_member, avatar_path):
    for m in members:
        if m is exclude_member:
            continue
        if _member_uses_avatar(m, avatar_path):
            return True
    return False


def reconcile_member(members, member, observed):
    """按规则调和一个改名/换头像的成员。原地修改 member，返回动作列表。

    member: 定位到的花名册成员 dict（None = 未定位到，不做调和）。
    observed: 资料页实测 {"main_nickname", "group_nickname", "avatar_path"}。
    """
    if not member:
        return []
    actions = []
    old_group = (member.get("group_nickname") or "").strip()
    new_group = (observed.get("group_nickname") or "").strip()
    old_main = (member.get("main_nickname") or "").strip()
    new_main = (observed.get("main_nickname") or "").strip()
    new_avatar = (observed.get("avatar_path") or "").strip()

    has_group = bool(old_group) or bool(new_group)
    group_changed = bool(old_group) and bool(new_group) and \
        not _nick_eq(old_group, new_group)
    main_same = _nick_eq(old_main, new_main)
    group_same = (not old_group and not new_group) or _nick_eq(old_group, new_group)

    if has_group:
        # 有群昵称：靠「头像+实际昵称」定位身份，改动判定见规则1/2。
        if group_changed and main_same:
            # 规则1：改了群昵称，头像+实际昵称匹配 → 更新群昵称 + 曾用群昵称
            former = member.setdefault("former_group_nicknames", [])
            if old_group not in former:
                former.append(old_group)
            member["group_nickname"] = new_group
            actions.append("update_group_nickname")
        elif new_avatar and (member.get("avatar_image_path") or "") and \
                new_avatar != member["avatar_image_path"] and \
                main_same and group_same:
            # 规则2：群昵称+昵称匹配、头像变了 → 加曾用头像
            former_av = member.setdefault("former_avatars", [])
            if new_avatar not in former_av:
                former_av.append(new_avatar)
            actions.append("add_former_avatar")
        # 有群昵称但未命中 1/2 → 不动（罕见，避免误改）
    else:
        # 无群昵称：显示的就是实际昵称，直接改昵称/头像 → 不冲突才更新。
        if new_main and not _nick_eq(old_main, new_main) and \
                not _nick_in_use(members, member, new_main):
            member["main_nickname"] = new_main
            actions.append("update_main_nickname")
        if new_avatar and not _member_uses_avatar(member, new_avatar) and \
                not _avatar_in_use(members, member, new_avatar):
            member.setdefault("former_avatars", []).append(new_avatar)
            actions.append("add_former_avatar")

    if actions:
        member["update_ts"] = time.time()
    return actions


# ------------------------------------------------------------------ 写回
def save_member_roster(group_name, roster_data):
    """原子写 members_roster.json，返回路径。"""
    group_dir = os.path.join(ROSTERS_DIR, group_name)
    os.makedirs(group_dir, exist_ok=True)
    path = os.path.join(group_dir, "members_roster.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(roster_data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def save_contact_after_accept(group_name, record, db_path=None):
    """好友通过后存档：把资料页提取的 record 写入该联系人花名册 + 打标。

    record 形如 {"main_nickname", "group_nickname", "wechat_id", ...,
                 "avatar_image_path", ...}（由资料页提取逻辑产出）。
    返回写入的 JSON 路径。
    """
    from ....msglog import message_log
    roster = {"group_name": group_name, "total_count": 1,
              "members": [record]}
    path = save_member_roster(group_name, roster)
    if db_path is None:
        db_path = os.path.join(PROJECT_ROOT, "workspace", "chatlogs", "chatlog.db")
    conn = message_log.connect(db_path)
    try:
        message_log.set_roster_status(conn, group_name, message_log.ROSTER_DONE)
    finally:
        conn.close()
    return path


# ------------------------------------------------------------------ 定位与调和编排
def load_members(group_name):
    """读某群花名册的 members 列表（无文件返回 []）。"""
    path = os.path.join(ROSTERS_DIR, group_name, "members_roster.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("members", [])
    except Exception:
        log.exception("读取花名册失败: %s", path)
        return []


def find_member_by_nickname(members, nick):
    """按昵称（主/群/曾用群昵称）定位成员，返回 member 或 None。"""
    if not nick:
        return None
    for m in members:
        for field in ("main_nickname", "group_nickname"):
            if _nick_eq(m.get(field, ""), nick):
                return m
        for fn in (m.get("former_group_nicknames") or []):
            if _nick_eq(fn, nick):
                return m
    return None


def reconcile_on_mismatch(dev, group_name, msg, rm=None, auto_back=False,
                          allow_new_member=True):
    """识别双因子失配 → 点头像进资料页 → 提取 → 定位成员 → 调和 → 存盘。

    供 Phase 4 在识别链路里调用：当 match_dual_factor 返回未匹配时，
    用本条消息的头像位置点进资料页，提取实测昵称/群昵称/头像，按规则调和。

    msg: slice_chat 消息 dict（含 avatar{x,y,w,h}、nickname / matched_user_name）。
    auto_back=True：调和（或确认未进资料页）后自动退回聊天页——
      只有确认在资料页/提取成功时才 back，避免 tap 未生效时误退聊天。
    allow_new_member=True：花名册定位不到成员时（进群晚于爬册的新成员），
      把资料页提取结果作为新成员入库（动态学习）。
    返回执行的动作列表；未失配 / 不在资料页 / 未定位到成员时返回 []。
    """
    from .profile_extractor import extract_profile
    avatar = msg.get("avatar") or {}
    ax, ay = avatar.get("x"), avatar.get("y")
    aw, ah = avatar.get("w"), avatar.get("h")
    if ax is None or ay is None:
        return []

    # 1. 点头像中心（聊天页点头像进对方资料页）
    try:
        from ..device.random_touch import Rect
        cx, cy = int(ax + aw / 2), int(ay + ah / 2)
        dev.tap_rect(Rect(cx - 30, cy - 30, 60, 60))
    except Exception as e:  # noqa: BLE001
        log.warning("点头像失败: %s", e)
        return []
    time.sleep(1.5)

    # 2. 提取资料页
    group_dir = os.path.join(ROSTERS_DIR, group_name)
    observed = extract_profile(dev, os.path.join(group_dir, "avatars"),
                               profile_shots_dir=os.path.join(group_dir, "profile_shots"),
                               session_name=group_name)

    # auto_back：提取成功说明确实在资料页 → 直接 back；
    # 失败时先确认页面状态，仅在资料页（提取失败的边缘情况）才 back，
    # tap 未生效（仍在聊天页）时绝不能 back（会退出聊天）。
    if auto_back:
        if observed is not None:
            dev.back()
            time.sleep(0.8)
        elif _on_profile_page(dev):
            dev.back()
            time.sleep(0.8)

    if observed is None:
        return []

    # 3. 定位成员：先按实测昵称，再按识别时的 OCR 昵称
    members = load_members(group_name)
    member = find_member_by_nickname(
        members, observed.get("main_nickname") or observed.get("group_nickname"))
    if member is None:
        member = find_member_by_nickname(
            members, (msg.get("nickname") or msg.get("matched_user_name") or "").strip())
    if member is None:
        # 新成员动态学习：爬册后才进群的成员，资料页提取结果直接入库
        if allow_new_member and observed.get("main_nickname") \
                and observed["main_nickname"] != "未知昵称" \
                and observed.get("avatar_image_path"):
            members.append(observed)
            save_member_roster(group_name, {
                "group_name": group_name, "total_count": len(members),
                "members": members})
            log.info("[%s] 新成员动态学习入库: %s",
                     group_name, observed["main_nickname"])
            return ["add_new_member"]
        log.info("[%s] 未在花名册中定位到该成员，跳过调和", group_name)
        return []

    # 4. 调和 + 存盘
    actions = reconcile_member(members, member, observed)
    if actions:
        save_member_roster(group_name, {
            "group_name": group_name, "total_count": len(members),
            "members": members})
    return actions


def _on_profile_page(dev):
    """当前是否停在个人资料页（OCR 判定，用于 auto_back 安全返回）。"""
    try:
        from .ocr_engine import run_ocr
        from .profile_extractor import is_profile_page
        img = dev.capture_bytes()
        return is_profile_page(run_ocr(img))
    except Exception:  # noqa: BLE001
        log.exception("资料页判定失败")
        return False


def reconcile_uncertain_messages(dev, group_name, messages, rm=None):
    """对 slice_chat 结果里 uncertain_entity=True 的消息逐个调和。

    slice_chat 在双因子失配时已把消息标 uncertain_entity=True + uncertain_avatar_pos；
    本函数负责「点头像→进资料页→提取→调和→存盘」的动作编排（批量），
    供识别/采集链路在合适时机调用（建议在滚动告一段落后批量做，避免打断滚动）。

    返回 {"attempted": n, "actions": [...], "errors": [...]}。
    """
    out = {"attempted": 0, "actions": [], "errors": []}
    for m in messages:
        if not m.get("uncertain_entity"):
            continue
        out["attempted"] += 1
        try:
            actions = reconcile_on_mismatch(dev, group_name, m, rm=rm)
            if actions:
                out["actions"].append({"nickname": m.get("nickname"),
                                       "actions": actions})
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] 调和失败", group_name)
            out["errors"].append({"nickname": m.get("nickname"),
                                  "error": f"{type(e).__name__}: {e}"})
    return out
