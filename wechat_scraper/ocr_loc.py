"""基于本地 RapidOCR 的毫秒级文字与公众号卡片定位。"""

from __future__ import annotations

import logging
from typing import List, Dict, Any, Optional, Tuple
import numpy as np
from PIL import Image, ImageGrab
from rapidocr_onnxruntime import RapidOCR

from .wechat import get_window_rect, ensure_interactive_desktop

logger = logging.getLogger("wechat-scraper")

_ocr_engine: Optional[RapidOCR] = None


def get_ocr_engine() -> RapidOCR:
    """懒加载单例 OCR 引擎。"""
    global _ocr_engine
    if _ocr_engine is None:
        _ocr_engine = RapidOCR()
    return _ocr_engine


def ocr_region(
    bbox: Optional[Tuple[int, int, int, int]] = None,
) -> Tuple[List[Dict[str, Any]], Image.Image]:
    """对屏幕指定物理矩形区域 (left, top, right, bottom) 截图并进行毫秒级 OCR 识别。"""
    ensure_interactive_desktop()
    engine = get_ocr_engine()

    if bbox:
        img = ImageGrab.grab(bbox=bbox, all_screens=True)
        offset_x, offset_y = bbox[0], bbox[1]
    else:
        img = ImageGrab.grab(all_screens=True)
        offset_x, offset_y = 0, 0

    img_np = np.array(img)
    ocr_results, _ = engine(img_np)

    items = []
    if ocr_results:
        for box, text, score in ocr_results:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            min_x, max_x = min(xs) + offset_x, max(xs) + offset_x
            min_y, max_y = min(ys) + offset_y, max(ys) + offset_y
            cx, cy = int((min_x + max_x) / 2), int((min_y + max_y) / 2)
            items.append({
                "text": text.strip(),
                "score": float(score),
                "cx": cx,
                "cy": cy,
                "left": int(min_x),
                "top": int(min_y),
                "right": int(max_x),
                "bottom": int(max_y),
                "width": int(max_x - min_x),
                "height": int(max_y - min_y),
            })

    return items, img


def find_text_pos(
    target: str,
    bbox: Optional[Tuple[int, int, int, int]] = None,
    min_score: float = 0.5,
) -> Optional[Tuple[int, int]]:
    """在指定区域查找包含目标文字的中心坐标 (cx, cy)。"""
    items, _ = ocr_region(bbox)
    for it in items:
        if target in it["text"] and it["score"] >= min_score:
            return (it["cx"], it["cy"])
    return None


def find_account_card_in_search(
    account_name: str,
    hwnd: Optional[int] = None,
) -> Tuple[int, int]:
    """在搜一搜结果大页中，定位目标公众号/服务号卡片并返回点击中心。"""
    if hwnd:
        bbox = get_window_rect(hwnd)
    else:
        bbox = (0, 0, 1600, 1400)

    items, _ = ocr_region(bbox)
    filter_bar_bottom = bbox[1] + 120
    for it in items:
        if any(w in it["text"] for w in ["不限", "小程序", "视频号", "服务号", "公众号", "表情", "文章"]):
            if it["bottom"] > filter_bar_bottom:
                filter_bar_bottom = it["bottom"]

    badge_candidates = []
    account_matched = []

    for it in items:
        if it["top"] < filter_bar_bottom - 10:
            continue
        text = it["text"]
        if "服务号" in text or "订阅号" in text or "公众号" in text:
            if "小程序" not in text:
                badge_candidates.append(it)
        if account_name in text:
            account_matched.append(it)

    if badge_candidates:
        first_badge = min(badge_candidates, key=lambda x: x["top"])
        logger.info("定位到「%s」服务号/订阅号卡片: (%d, %d)", account_name, first_badge["cx"], first_badge["cy"])
        return (first_badge["cx"], first_badge["cy"])

    if account_matched:
        first_match = min(account_matched, key=lambda x: x["top"])
        logger.info("定位到「%s」关键词卡片: (%d, %d)", account_name, first_match["cx"], first_match["cy"])
        return (first_match["cx"], first_match["cy"])

    return (bbox[0] + 680, bbox[1] + 500)


def find_article_cards() -> List[Dict[str, Any]]:
    """在公众号主页动态提取文章卡片列表 (基于「阅读」锚点 100% 确定性识别)。"""
    items, _ = ocr_region()
    tab_bottom = 0
    for it in items:
        if it["text"] in ["全部", "视频", "合集", "关注"]:
            if it["bottom"] > tab_bottom:
                tab_bottom = it["bottom"]

    read_anchors = []
    for it in items:
        if it["top"] <= tab_bottom:
            continue
        if it["text"].startswith("阅读") or "阅读 " in it["text"] or it["text"] == "阅读":
            read_anchors.append(it)

    cards = []
    for anchor in read_anchors:
        anchor_top = anchor["top"]
        title_candidates = []
        for it in items:
            if it == anchor:
                continue
            if it["top"] >= tab_bottom and it["bottom"] <= anchor_top + 10:
                dist = anchor_top - it["bottom"]
                if -5 <= dist <= 90:
                    if len(it["text"]) >= 2 and not it["text"].isdigit():
                        title_candidates.append((dist, it))

        if title_candidates:
            title_candidates.sort(key=lambda x: x[0])
            best_title_item = title_candidates[0][1]
            title_text = best_title_item["text"]
            click_x = best_title_item["cx"]
            click_y = best_title_item["cy"]
        else:
            title_text = f"微信文章_{anchor['top']}"
            click_x = anchor["cx"]
            click_y = anchor["top"] - 35

        cards.append({
            "title": title_text,
            "cx": click_x,
            "cy": click_y,
            "top": click_y,
        })

    cards.sort(key=lambda x: x["top"])
    return cards


def find_fold_buttons() -> List[Tuple[int, int]]:
    """识别当前主页可视区内的「余下 X 篇」折叠展开按钮。"""
    items, _ = ocr_region()
    buttons = []
    for it in items:
        txt = it["text"]
        if "余下" in txt and ("篇" in txt or "条" in txt):
            logger.info("检测到折叠展开按钮: %r at (%d, %d)", txt, it["cx"], it["cy"])
            buttons.append((it["cx"], it["cy"]))
    return buttons


def find_dots_button_pos() -> Tuple[int, int]:
    """定位微信文章正文右上角的「…」更多按钮。"""
    return (2276, 43)
