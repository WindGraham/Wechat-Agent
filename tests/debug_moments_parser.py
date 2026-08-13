#!/usr/bin/env python3
"""debug_moments_parser.py — 用存档截图调试 moments_parser v3。
用法: python tests/debug_moments_parser.py [样本号...]
"""
import sys
import glob

import cv2

sys.path.insert(0, "src")
from interaction.ports.android.perception import ocr_engine, moments_parser


def show_entry(e):
    av = e["avatar"]
    print(f"  [条目{e['idx']}] avatar=({av['x']},{av['y']},{av['w']}x{av['h']})"
          f" partial_top={e['partial_top']} partial_bottom={e['partial_bottom']}")
    print(f"    nickname: {e['nickname']!r}")
    txt = e["text"].replace("\n", " | ")
    print(f"    text: {txt[:100]!r}")
    print(f"    time: {e['time']!r}  dots: {e['dots']}  "
          f"fulltext: {e['fulltext_btn']}")
    print(f"    likes: {e['likes']}")
    for c in e["comments"]:
        print(f"    comment: {c['from_user']!r} -> {c['reply_to']!r}: "
              f"{c['content']!r}  click=({c['click_x']},{c['click_y']}) "
              f"blue={c['blue_ranges']}")
    print(f"    text_complete={e.get('text_complete')} "
          f"complete={e.get('complete')}")


def main():
    pats = sys.argv[1:] or ["*"]
    files = []
    for p in pats:
        files.extend(glob.glob(f"assets/samples/moments/{p}.png"))
    for f in sorted(set(files)):
        print(f"\n{'='*70}\n{f}\n{'='*70}")
        img = cv2.imread(f)
        items = ocr_engine.run_ocr(img)
        entries, extra = moments_parser.parse_moments_entries(img, items)
        print(f"page_extra: comment_input={extra['comment_input']} "
              f"action_menu={extra['action_menu']} bg={extra['bg']}")
        for e in entries:
            show_entry(e)


if __name__ == "__main__":
    main()
