#!/usr/bin/env python3
"""icon_templates.py - v2 图标模板库加载器

维护 `samples/ui_inventory/templates/` 下的 PNG 模板，按语义名称聚合变体，
供 `icon_detector.py` 做多尺度模板匹配。
"""

import os
import cv2
import numpy as np
from typing import Dict, List, Tuple

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_TEMPLATE_DIR = os.path.join(BASE_DIR, "samples", "ui_inventory", "templates")


def _name_and_variant(filename: str) -> Tuple[str, str]:
    """从文件名解析语义名和变体，例如 `tab_wechat_active.png` -> ('tab_wechat', 'active')。"""
    stem, _ = os.path.splitext(filename)
    parts = stem.split("_")
    if len(parts) < 2:
        return stem, "default"
    return "_".join(parts[:-1]), parts[-1]


def load_template_variants(template_dir: str = DEFAULT_TEMPLATE_DIR) -> Dict[str, List[Tuple[str, np.ndarray]]]:
    """加载所有模板 PNG，返回 name -> list[(variant, cv2 image)] 的映射。

    variant 来自文件名最后一段（如 `tab_wechat_active.png` 的 variant 为 `active`）。
    """
    templates: Dict[str, List[Tuple[str, np.ndarray]]] = {}
    if not os.path.isdir(template_dir):
        return templates

    for filename in sorted(os.listdir(template_dir)):
        if not filename.lower().endswith(".png"):
            continue
        path = os.path.join(template_dir, filename)
        img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if img is None:
            continue
        name, variant = _name_and_variant(filename)
        templates.setdefault(name, []).append((variant, img))
    return templates


def load_templates(template_dir: str = DEFAULT_TEMPLATE_DIR) -> Dict[str, List]:
    """加载所有模板 PNG，返回 name -> list[cv2 image] 的映射。

    同一语义名的多个变体（如 tab_wechat_active / tab_wechat_inactive）会归入同一列表。
    这是 icon_detector 消费的主要接口。
    """
    out: Dict[str, List] = {}
    for name, variants in load_template_variants(template_dir).items():
        out[name] = [img for _, img in variants]
    return out


def list_templates(template_dir: str = DEFAULT_TEMPLATE_DIR) -> List[str]:
    """返回当前模板库中所有语义名称（去重、排序）。"""
    return sorted(load_template_variants(template_dir).keys())


if __name__ == "__main__":
    for name, variants in load_templates().items():
        sizes = [f"{v.shape[1]}x{v.shape[0]}" for v in variants]
        print(f"{name}: {len(variants)} variant(s) - {', '.join(sizes)}")
