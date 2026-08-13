#!/usr/bin/env python3
"""
calibrate_nicknames.py — 用 Gemini 2.5 Flash Lite 校准群成员昵称。

读取：
  workspace/group_rosters/交流一下？/members_roster.json
  workspace/group_rosters/交流一下？/profile_shots/profile_*.png

对每个成员：
  - 把资料页截图 + 当前 OCR 结果一起发给 Gemini
  - 让 Gemini 返回最准确的主昵称（去掉性别图标、emoji 误识别等噪声）
  - 回写到 JSON 的 main_nickname 字段

并发：默认 50，可通过 --concurrency 调整。
"""

import argparse
import asyncio
import base64
import json
import os
import re
from pathlib import Path

import aiohttp

GROUP_NAME = "交流一下？"
OUTPUT_DIR = Path(f"workspace/group_rosters/{GROUP_NAME}")
JSON_PATH = OUTPUT_DIR / "members_roster.json"
PROFILE_SHOTS_DIR = OUTPUT_DIR / "profile_shots"

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_CONCURRENCY = 50
MAX_RETRIES = 3
RETRY_DELAY = 2.0


def api_url(model):
    return f"https://api.aixhan.com/v1beta/models/{model}:generateContent"


def load_api_key():
    """优先读环境变量 AIXHAN_API_KEY，否则读 workspace/.env。"""
    key = os.environ.get("AIXHAN_API_KEY")
    if key:
        return key
    env_file = Path("workspace/.env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("AIXHAN_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError("找不到 AIXHAN_API_KEY，请设置环境变量或在 workspace/.env 中配置")


def parse_json(raw):
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3]
    if text.lower().startswith("json"):
        text = text[4:]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return None


async def ask_gemini(session, api_key, model, image_path, ocr_nickname, wechat_id, group_nickname):
    """并发调用 Gemini，返回解析后的 dict。"""
    image_bytes = image_path.read_bytes()
    b64 = base64.b64encode(image_bytes).decode()
    mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"

    prompt = (
        "这是一张微信个人资料页的截图。"
        "请根据截图内容，提取最准确的【主昵称】（头像右侧最大的那一行名字）。\n"
        "注意：\n"
        "1. 忽略头像右侧的性别图标（蓝色男性/红色女性图标）。\n"
        "2. 忽略 emoji 表情，如果 emoji 无法识别，直接去掉。\n"
        "3. 当前 OCR 结果可能含有噪声，仅作为参考。\n"
        "4. 如果名字折成多行，请按正确顺序拼接。\n\n"
        f"当前 OCR 主昵称：{ocr_nickname or '无'}\n"
        f"当前 OCR 微信号：{wechat_id or '无'}\n"
        f"当前 OCR 群昵称：{group_nickname or '无'}\n\n"
        "请只输出 JSON，格式如下（不要任何额外说明）：\n"
        '{"main_nickname":"提取到的主昵称"}'
    )

    parts = [
        {"text": prompt},
        {"inlineData": {"mimeType": mime, "data": b64}},
    ]
    payload = {"contents": [{"parts": parts}]}
    headers = {"x-goog-api-key": api_key}

    for attempt in range(MAX_RETRIES):
        try:
            async with session.post(api_url(model), json=payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=60)) as r:
                if r.status == 200:
                    data = await r.json()
                    cand = data.get("candidates", [])
                    if cand:
                        txt = "".join(
                            p.get("text", "") for p in
                            cand[0].get("content", {}).get("parts", [])
                        )
                        return parse_json(txt)
                elif r.status == 429:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                    continue
                else:
                    body = await r.text()
                    print(f"  API {r.status}: {body[:150]}")
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
        except Exception as e:
            print(f"  网络错(尝试{attempt+1}): {e}")
            await asyncio.sleep(RETRY_DELAY * (attempt + 1))
    return None


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help="并发数")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL,
                        help="Gemini 模型名，例如 gemini-2.5-flash")
    parser.add_argument("--test", type=int, default=0, help="只校准前 N 条")
    args = parser.parse_args()

    if not JSON_PATH.exists():
        print(f"JSON 不存在: {JSON_PATH}")
        return

    api_key = load_api_key()
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    members = data.get("members", [])
    if args.test > 0:
        members = members[:args.test]

    print(f"待校准: {len(members)} 条 | 模型: {args.model} | 并发: {args.concurrency}")

    sem = asyncio.Semaphore(args.concurrency)
    updated = 0
    failed = 0

    async def worker(idx, member):
        nonlocal updated, failed
        shot = member.get("profile_screenshot")
        if not shot:
            return
        image_path = OUTPUT_DIR / shot
        if not image_path.exists():
            print(f"  [{idx}] 截图不存在: {image_path}")
            failed += 1
            return

        async with sem:
            async with aiohttp.ClientSession() as session:
                result = await ask_gemini(
                    session, api_key, args.model, image_path,
                    member.get("main_nickname", ""),
                    member.get("wechat_id", ""),
                    member.get("group_nickname", ""),
                )

        if result and "main_nickname" in result:
            old = member.get("main_nickname", "")
            new = result["main_nickname"].strip()
            if new and new != old:
                member["main_nickname"] = new
                updated += 1
                print(f"  [{idx}] {old!r} -> {new!r}")
            else:
                print(f"  [{idx}] 无需修改: {old!r}")
        else:
            print(f"  [{idx}] Gemini 无有效返回")
            failed += 1

    tasks = [worker(i + 1, m) for i, m in enumerate(members)]
    await asyncio.gather(*tasks)

    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\n完成！更新 {updated} 条，失败 {failed} 条")


if __name__ == "__main__":
    asyncio.run(main())
