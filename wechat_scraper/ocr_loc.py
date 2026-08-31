"""基于本地 RapidOCR 的毫秒级文字与公众号卡片定位。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import cv2
import numpy as np
from PIL import Image, ImageGrab
from rapidocr_onnxruntime import RapidOCR

from .wechat import get_window_rect, ensure_interactive_desktop, get_wechat_browser_hwnd

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
        l, t, r, b = bbox
        if r - l < 20 or b - t < 20:
            bbox = None

    if bbox:
        img = ImageGrab.grab(bbox=bbox, all_screens=True)
        offset_x, offset_y = bbox[0], bbox[1]
    else:
        img = ImageGrab.grab(all_screens=True)
        offset_x, offset_y = 0, 0

    if img.width < 20 or img.height < 20:
        return [], img

    img_np = np.array(img)
    try:
        ocr_results, _ = engine(img_np)
    except Exception as exc:
        logger.debug("OCR 引擎执行异常: %s", exc)
        ocr_results = None

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


class WeChatBlackScreenError(Exception):
    """微信搜一搜或内置浏览器出现黑屏/无渲染异常。"""
    pass


def is_screen_black_or_blank(
    img: Image.Image,
    items: List[Dict[str, Any]],
    brightness_threshold: float = 25.0,
) -> bool:
    """检测当前截图区域是否发生黑屏或全白无响应挂起。"""
    if img.width < 50 or img.height < 50:
        return False

    # 1. 检查有效 OCR 识别条目数
    if len(items) == 0:
        # 当没有任何文字识别出时，计算图片平均灰度亮度与方差
        img_gray = img.convert("L")
        stat = np.array(img_gray)
        mean_val = float(np.mean(stat))
        std_val = float(np.std(stat))
        # 纯黑（平均亮度 < 25）或纯色无内容（标准差 < 5.0）
        if mean_val < brightness_threshold or std_val < 5.0:
            return True

    # 2. 或者虽然有极少量噪点，但图像绝大部分区域 (>88%) 像素为近黑色
    img_gray = img.convert("L")
    stat = np.array(img_gray)
    black_ratio = float(np.count_nonzero(stat < 20) / stat.size)
    if black_ratio > 0.88 and len(items) <= 1:
        return True

    return False


def find_account_card_in_search(
    account_name: str,
    hwnd: Optional[int] = None,
    timeout: float = 6.0,
) -> Tuple[int, int]:
    """在搜一搜结果大页中，轮询定位左侧主结果中的公众号/服务号卡片 (自适应网络加载与黑屏检测)。"""
    import time
    deadline = time.time() + timeout

    while time.time() < deadline:
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

        items, img = ocr_region(bbox)

        # 若页面完全为纯黑且无任何 OCR 结果，等待渲染
        if is_screen_black_or_blank(img, items):
            logger.debug("搜一搜页面正在渲染中，等待...")
            time.sleep(0.6)
            continue

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

        # 若已有内容加载出来，只是还未匹配到徽标，短暂等待后继续检测
        time.sleep(0.6)

    # 超时后最终检查：若依然完全没有任何内容项，判定为渲染挂死
    if hwnd:
        bbox = get_window_rect(hwnd)
        items, _ = ocr_region(bbox)
        content_items = [it for it in items if it["top"] >= bbox[1] + 130 and it["cx"] <= bbox[0] + int((bbox[2] - bbox[0]) * 0.68)]
        if len(content_items) == 0:
            logger.error("搜一搜结果内容区域超时无内容 (0 条目)，判定为渲染挂起！")
            raise WeChatBlackScreenError("搜一搜内容区空白或未渲染")

        default_x = bbox[0] + int((bbox[2] - bbox[0]) * 0.25)
        default_y = bbox[1] + int((bbox[3] - bbox[1]) * 0.35)
    else:
        default_x, default_y = 629, 550

    logger.warning("未直接识别到特定徽标，使用左侧主卡片兜底位置: (%d, %d)", default_x, default_y)
    return (default_x, default_y)


def find_article_cards(hwnd: Optional[int] = None) -> List[Dict[str, Any]]:
    """在公众号主页提取文章卡片列表 (精准锚定阅读量与日期行，杜绝把企业介绍、置顶、合集标签误判为文章)。"""
    if hwnd is None:
        hwnd = get_wechat_browser_hwnd()
    if not hwnd:
        logger.warning("未定位到公众号主页窗口，跳过本轮卡片提取。")
        return []

    bbox = get_window_rect(hwnd)
    # 确保窗口尺寸有效 (宽和高均大于 300)
    if bbox[2] - bbox[0] < 300 or bbox[3] - bbox[1] < 300:
        return []

    items, img = ocr_region(bbox)
    win_top = bbox[1]

    # 1. 动态定位顶部导航 Tab 栏 (以「全部/贴图/文章/视频号」为界，严格排除上方所有企业介绍与关注按钮)
    tab_item = next((it for it in items if any(w in it["text"] for w in ["全部", "贴图", "文章", "视频号"])), None)
    if tab_item:
        tab_bottom = tab_item["bottom"] + 40
    else:
        tab_bottom = win_top + 300
        for it in items:
            if it["top"] < win_top + 900 and any(w in it["text"] for w in ["关注", "已关注", "发消息", "合集", "原创", "篇原创", "朋友关注"]):
                if it["bottom"] > tab_bottom:
                    tab_bottom = it["bottom"]
        tab_bottom += 40

    # 系统标签与统计干扰词黑名单 (坚决不能作为文章标题)
    exclude_words = {
        "全部", "贴图", "文章", "视频", "视频号", "合集", "关注", "已关注", "发消息", "服务", 
        "服务号", "订阅号", "小程序", "展开", "收起", "相关搜索", "微信", "阅读", "在看", "赞",
        "分享", "原创", "篇原创", "置顶", "个内容", "篇内容", "条内容", "朋友关注", "朋友看过",
        "余下", "条", "篇", "精选", "私信"
    }

    cards = []
    seen_y_bands = []

    def _is_in_existing_band(cy: int, threshold: int = 40) -> bool:
        return any(abs(cy - y) < threshold for y in seen_y_bands)

    # 2. 策略 A: 互动数据与日期锚点关联法 (微信文章卡片下方均有「阅读/赞」或日期)
    anchors = [
        it for it in items 
        if it["top"] > tab_bottom and any(w in it["text"] for w in ["阅读", "赞", "在看", "分享", "次阅读"])
    ]
    # 补充日期类锚点 (针对无阅读数标识的文章卡片)
    date_anchors = [
        it for it in items
        if it["top"] > tab_bottom and (any(it["text"].endswith(d) for d in ["日", "月", "年"]) or any(w in it["text"] for w in ["昨天", "前天"]))
        and not _is_in_existing_band(it["cy"], threshold=30)
    ]
    all_anchors = anchors + date_anchors

    for anchor in all_anchors:
        anchor_top = anchor["top"]
        title_candidates = []
        for it in items:
            if it == anchor:
                continue
            if it["top"] >= tab_bottom and it["bottom"] <= anchor_top + 15:
                dist = anchor_top - it["bottom"]
                if -5 <= dist <= 130:
                    txt = it["text"].strip()
                    if len(txt) >= 2 and not any(w in txt for w in exclude_words):
                        title_candidates.append((dist, it))

        if title_candidates:
            # 优先选择包含中文的候选词，并按距离锚点由近到远排序 (距离最近的才是紧挨着的主标题)
            title_candidates.sort(
                key=lambda item_dist: (
                    not any("\u4e00" <= ch <= "\u9fff" for ch in item_dist[1]["text"]),
                    item_dist[0],
                )
            )
            best_item = title_candidates[0][1]
            title_text = best_item["text"]
            click_x, click_y = best_item["cx"], best_item["cy"]
        else:
            title_text = f"微信文章_{anchor['top']}"
            click_x, click_y = anchor["cx"], anchor["top"] - 40

        if not _is_in_existing_band(click_y, threshold=40):
            seen_y_bands.append(click_y)
            cards.append({
                "title": title_text,
                "cx": click_x,
                "cy": click_y,
                "top": click_y,
            })

    cards.sort(key=lambda x: x["top"])
    return cards


def find_fold_buttons(hwnd: Optional[int] = None) -> List[Tuple[int, int]]:
    """识别当前公众号主页内的折叠展开按钮 (包括「余下 X 篇」、「X 个内容」、「展开」等)。"""
    if hwnd is None:
        hwnd = get_wechat_browser_hwnd()
    bbox = get_window_rect(hwnd) if hwnd else None

    items, _ = ocr_region(bbox)
    buttons = []
    for it in items:
        txt = it["text"].replace(" ", "")
        if (
            ("余下" in txt and any(w in txt for w in ["篇", "条", "内容"]))
            or any(w in txt for w in ["个内容", "篇内容", "条内容"])
            or txt == "展开"
        ):
            logger.info("检测到折叠展开按钮: %r at (%d, %d)", it["text"], it["cx"], it["cy"])
            buttons.append((it["cx"], it["cy"]))
    return buttons


def resolve_res_image_path(name: str | Path) -> Optional[Path]:
    """多路径解析 res 目录中的模板图片文件。"""
    p = Path(name)
    if p.is_file():
        return p

    search_dirs = [
        Path(__file__).parent / "res",
        Path(__file__).parent.parent / "res",
        Path.cwd() / "res",
        Path.cwd() / "wechat_scraper" / "res",
    ]
    name_str = str(name)
    for d in search_dirs:
        for candidate in [d / name_str, d / f"{name_str}.png", d / f"{name_str}.jpg"]:
            if candidate.is_file():
                return candidate
    return None


def find_image_pos(
    image_name: str | Path,
    bbox: Optional[Tuple[int, int, int, int]] = None,
    threshold: float = 0.75,
) -> Optional[Tuple[int, int]]:
    """使用 OpenCV 模板匹配在屏幕指定区域 (left, top, right, bottom) 或全屏查找图片中心坐标 (cx, cy)。"""
    img_path = resolve_res_image_path(image_name)
    if not img_path:
        return None

    tpl = cv2.imread(str(img_path))
    if tpl is None:
        return None

    ensure_interactive_desktop()
    if bbox:
        l, t, r, b = bbox
        if r - l < tpl.shape[1] or b - t < tpl.shape[0]:
            return None
        screenshot = ImageGrab.grab(bbox=bbox, all_screens=True)
        offset_x, offset_y = bbox[0], bbox[1]
    else:
        screenshot = ImageGrab.grab(all_screens=True)
        offset_x, offset_y = 0, 0

    screen_bgr = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

    if screen_bgr.shape[0] < tpl.shape[0] or screen_bgr.shape[1] < tpl.shape[1]:
        return None

    res = cv2.matchTemplate(screen_bgr, tpl, cv2.TM_CCOEFF_NORMED)
    min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)

    if max_val >= threshold:
        th, tw = tpl.shape[:2]
        cx = max_loc[0] + tw // 2 + offset_x
        cy = max_loc[1] + th // 2 + offset_y
        logger.info("图色定位成功 [%s] (相似度: %.2f): (%d, %d)", img_path.name, max_val, cx, cy)
        return (cx, cy)

    return None


def find_dots_button_pos(hwnd: Optional[int] = None) -> Tuple[int, int]:
    """定位微信文章正文右上角的「…」更多按钮 (优先图色匹配，其次 OCR 锚点，最后几何推算)。"""
    if hwnd is None:
        hwnd = get_wechat_browser_hwnd()
    
    if hwnd:
        rect = get_window_rect(hwnd)
        win_left, win_top, win_right, win_bottom = rect
    else:
        win_left, win_top, win_right, win_bottom = 0, 0, 2560, 1600

    actual_top = max(win_top, 0)
    top_bar_bbox = (max(0, win_right - 600), actual_top, win_right, actual_top + 120)

    # 1. 优先使用 res/ 目录下的模板图片进行高精度图色匹配
    for tpl_name in ["threee_dot.png", "three_dot.png", "three_dots.png", "dots.png", "more.png"]:
        img_pos = find_image_pos(tpl_name, bbox=top_bar_bbox, threshold=0.75)
        if img_pos:
            logger.info("通过图片 [%s] 精确定位到「…」按钮: (%d, %d)", tpl_name, img_pos[0], img_pos[1])
            return img_pos

    # 2. 次选：通过 OCR 识别顶部栏的「由元宝提供」/「总结」等标识
    items, _ = ocr_region(top_bar_bbox)
    summary_item = None
    for it in items:
        if any(w in it["text"] for w in ["元宝", "总结", "提供"]):
            summary_item = it
            break

    # 最小化按钮「—」的左边缘通常在 win_right - 145 左右
    min_btn_left = win_right - 145

    if summary_item:
        capsule_right = summary_item["right"] + 35
        if capsule_right < min_btn_left - 10:
            dots_x = int((capsule_right + min_btn_left) / 2)
        else:
            dots_x = win_right - 170
        dots_y = summary_item["cy"] if (actual_top <= summary_item["cy"] <= actual_top + 50) else actual_top + 22
        logger.info("通过顶部栏「%s」精确定位到「…」按钮: (%d, %d)", summary_item["text"], dots_x, dots_y)
        return (dots_x, dots_y)

    # 3. 几何推算兜底：Windows 标准标题栏右侧控件排布 (避开关闭/最大化/最小化)
    dots_x = win_right - 170
    dots_y = actual_top + 22
    logger.info("使用几何推算定位「…」按钮: (%d, %d)", dots_x, dots_y)
    return (dots_x, dots_y)
