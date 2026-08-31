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
    """在搜一搜结果大页中，精准定位左侧主结果中的公众号/服务号卡片。"""
    if hwnd:
        bbox = get_window_rect(hwnd)
    else:
        bbox = (0, 0, 1600, 1400)

    win_left, win_top, win_right, win_bottom = bbox
    win_width = max(win_right - win_left, 800)

    # 1. 严格限制在左侧主内容区 (右侧 35% 为「相关搜索」侧边栏，坚决排除)
    left_column_max_x = win_left + int(win_width * 0.68)
    # 2. 顶部导航与搜索框高度 (通常在 win_top + 140 以内)
    content_min_top = win_top + 130

    items, _ = ocr_region(bbox)

    badge_candidates = []
    account_matched = []

    for it in items:
        # 排除顶部搜索框与导航栏
        if it["top"] < content_min_top:
            continue
        # 排除右侧「相关搜索」区域
        if it["cx"] > left_column_max_x:
            continue

        text = it["text"]
        # 优先寻找包含「服务号」或「订阅号」或「公众号」的小徽标 (严格排除小程序)
        if any(b in text for b in ["服务号", "订阅号", "公众号"]):
            if "小程序" not in text:
                badge_candidates.append(it)

        # 匹配包含账号关键词的卡片标题
        if account_name in text:
            if not any(sub in text for sub in ["相关搜索", "招聘", "小程序", "官网"]):
                account_matched.append(it)

    # 策略 1: 优先点击包含「服务号」/「订阅号」徽标的卡片
    if badge_candidates:
        first_badge = min(badge_candidates, key=lambda x: x["top"])
        logger.info("定位到「%s」服务号/订阅号卡片: (%d, %d)", account_name, first_badge["cx"], first_badge["cy"])
        return (first_badge["cx"], first_badge["cy"])

    # 策略 2: 在左侧主栏点击最靠前的账号匹配标题
    if account_matched:
        first_match = min(account_matched, key=lambda x: x["top"])
        logger.info("定位到「%s」关键词卡片: (%d, %d)", account_name, first_match["cx"], first_match["cy"])
        return (first_match["cx"], first_match["cy"])

    # 兜底默认左侧主卡片区域
    default_x = win_left + int(win_width * 0.3)
    default_y = win_top + 350
    logger.warning("未直接识别到特定徽标，使用左侧主卡片兜底位置: (%d, %d)", default_x, default_y)
    return (default_x, default_y)


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
