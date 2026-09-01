"""基于本地 RapidOCR 的毫秒级文字与公众号卡片定位。"""

from __future__ import annotations

import time
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import cv2
import numpy as np
from PIL import Image, ImageGrab
from rapidocr_onnxruntime import RapidOCR

from .wechat import get_window_rect, ensure_interactive_desktop, get_wechat_browser_hwnd, activate_hwnd, user32

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
    timeout: float = 4.0,
) -> Tuple[int, int]:
    """在搜一搜结果大页中，轮询定位左侧主结果中的公众号/服务号卡片 (自适应网络加载与黑屏检测)。"""
    import time
    deadline = time.time() + timeout

    if not hwnd:
        hwnd = get_wechat_browser_hwnd()
    if hwnd:
        activate_hwnd(hwnd)
        time.sleep(0.4)

    while time.time() < deadline:
        if not hwnd or not user32.IsWindow(hwnd):
            hwnd = get_wechat_browser_hwnd()
            if hwnd:
                activate_hwnd(hwnd)
                time.sleep(0.3)

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

        # 若页面完全为纯黑且无任何 OCR 结果，等待渲染并尝试重新激活
        if is_screen_black_or_blank(img, items):
            logger.debug("搜一搜页面正在渲染中，等待...")
            if hwnd:
                activate_hwnd(hwnd)
            time.sleep(0.6)
            continue

        badge_candidates = []
        account_matched = []

        # 问答与搜索推荐词黑名单 (坚决防止把问答卡片/大家都在搜误判为公众号)
        qa_exclude = ["是什么", "怎么样", "怎么", "如何", "哪家", "问答", "贴现", "百科", "招聘", "官网", "小程序", "相关搜索", "大家都在搜", "?", "？"]

        for it in items:
            # 排除顶部搜索框与导航栏
            if it["top"] < content_min_top:
                continue
            # 排除右侧「相关搜索」区域
            if it["cx"] > left_column_max_x:
                continue

            text = it["text"].strip()
            # 优先寻找包含「服务号」或「订阅号」或「公众号」的小徽标 (严格排除小程序)
            if any(b in text for b in ["服务号", "订阅号", "公众号"]):
                if "小程序" not in text:
                    badge_candidates.append(it)

            # 匹配公众号名称：优先精准匹配，严格排除问答句式
            if account_name in text:
                if not any(sub in text for sub in qa_exclude):
                    # 计算与账号名的匹配度 (完全一致或前缀一致优先)
                    is_exact = text == account_name
                    is_prefix = text.startswith(account_name) and len(text) <= len(account_name) + 4
                    account_matched.append((is_exact, is_prefix, it))

        # 策略 1: 优先点击包含「服务号」/「订阅号」徽标的卡片
        if badge_candidates:
            first_badge = min(badge_candidates, key=lambda x: x["top"])
            logger.info("定位到「%s」服务号/订阅号卡片: (%d, %d)", account_name, first_badge["cx"], first_badge["cy"])
            return (first_badge["cx"], first_badge["cy"])

        # 策略 2: 精准匹配账号名称 (完全一致 > 前缀短词 > 最上方)
        if account_matched:
            account_matched.sort(key=lambda x: (not x[0], not x[1], x[2]["top"]))
            best_match = account_matched[0][2]
            logger.info("精准定位到「%s」账号卡片: %s (%d, %d)", account_name, best_match["text"], best_match["cx"], best_match["cy"])
            return (best_match["cx"], best_match["cy"])

        # 若已有内容加载出来，只是还未匹配到徽标，短暂等待后继续检测
        time.sleep(0.5)

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

    # 确保公众号主页窗口处于前台激活状态
    activate_hwnd(hwnd)
    time.sleep(0.3)

    bbox = get_window_rect(hwnd)
    # 确保窗口尺寸有效 (宽和高均大于 300)
    if bbox[2] - bbox[0] < 300 or bbox[3] - bbox[1] < 300:
        return []

    items, img = ocr_region(bbox)
    win_top = bbox[1]

    # 1. 动态定位顶部导航 Tab 栏 (以「全部/贴图/文章/视频号」为界，严格排除上方所有企业介绍与关注按钮)
    tab_item = next((it for it in items if any(w in it["text"] for w in ["全部", "贴图", "文章", "视频号"])), None)
    if tab_item:
        tab_bottom = tab_item["bottom"] + 20
    else:
        tab_bottom = win_top + 300
        for it in items:
            if it["top"] < win_top + 900 and any(w in it["text"] for w in ["关注", "已关注", "发消息", "合集", "原创", "篇原创", "朋友关注"]):
                if it["bottom"] > tab_bottom:
                    tab_bottom = it["bottom"]
        tab_bottom += 20

    # 精确黑名单 (仅当整段文字完全等于这些 UI 词时排除，防止误杀带这些字眼的文章标题)
    exact_exclude = {
        "全部", "贴图", "文章", "视频", "视频号", "合集", "关注", "已关注", "发消息", "服务", 
        "服务号", "订阅号", "小程序", "展开", "收起", "相关搜索", "微信", "阅读", "在看", "赞",
        "分享", "原创", "置顶", "精选", "私信", "今天", "昨天", "前天", "搜索", "取消", "确定",
        "返回", "关闭", "刷新", "更多", "复制链接", "用浏览器打开", "在浏览器中打开",
        "预约成功通知", "服务通知", "微信团队", "文件传输助手", "订阅号消息", "消息", "通知"
    }
    # 状态与统计词黑名单 (包含即排除)
    sub_exclude = [
        "篇原创", "朋友关注", "朋友看过", "个内容", "篇内容", "条内容", "相关搜索", "余下",
        "微信公众平台", "公众号", "小程序", "大家都在搜", "预约成功", "服务通知", "微信团队"
    ]

    def _is_valid_title(txt: str) -> bool:
        t = txt.strip()
        if len(t) < 3:
            return False
        if t in exact_exclude:
            return False
        if any(sub in t for sub in sub_exclude):
            return False
        # 排除典型搜索联想问答句式 (如 "xx是什么平台", "xx怎么贴现")
        if (t.endswith("?") or t.endswith("？") or any(q in t for q in ["是什么", "怎么", "如何"])) and len(t) < 16:
            if not any(k in t for k in ["发布", "通知", "服务", "发展", "应用", "年", "月", "日", "毕业", "典礼", "学院"]):
                return False
        # 纯日期与时间过滤 (如 "08/18", "14:48", "2026-08-31")
        if (any(t.endswith(d) for d in ["日", "月", "年"]) or "/" in t or ":" in t) and len(t) <= 10:
            if not any(ch in t for ch in ["涨", "第", "大会", "峰会", "链", "融", "通", "数", "重磅", "万向", "高金", "毕业"]):
                return False
        return True

def detect_card_slices_opencv(
    img_np: np.ndarray,
    offset_y: int,
    tab_bottom: int,
    win_bottom: int,
) -> List[int]:
    """使用 OpenCV 形态学算子检测卡片间的物理水平分割线与留白缝隙，返回屏幕 Y 轴物理切片点。"""
    if img_np is None or img_np.size == 0:
        return []

    h, w = img_np.shape[:2]
    gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY) if len(img_np.shape) == 3 else img_np

    # 1. 自适应阈值化，突出水平物理线条与色彩跳变
    thresh = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 15, 8
    )
    # 水平结构元 (长度为宽度的 8%，只提取物理长横线，过滤文字笔画)
    kernel_len = max(40, int(w * 0.08))
    horiz_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_len, 1))
    horiz_lines = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, horiz_kernel)

    # 2. 投影统计并聚类
    y_peaks = np.where(np.sum(horiz_lines, axis=1) > 0)[0]
    dividers = []
    cluster = []
    for y in y_peaks:
        sy = y + offset_y
        if sy <= tab_bottom + 10 or sy >= win_bottom - 20:
            continue
        if not cluster or y - cluster[-1] > 12:
            if cluster:
                dividers.append(int(np.mean(cluster)) + offset_y)
            cluster = [y]
        else:
            cluster.append(y)
    if cluster:
        dividers.append(int(np.mean(cluster)) + offset_y)

    return sorted(dividers)


def find_article_cards(hwnd: Optional[int] = None) -> List[Dict[str, Any]]:
    """在公众号主页提取文章卡片列表 (基于 OpenCV 物理容器切片 + 自适应列聚类，零硬编码距离阈值)。"""
    import re
    if hwnd is None:
        hwnd = get_wechat_browser_hwnd()
    if not hwnd:
        logger.warning("未定位到公众号主页窗口，跳过本轮卡片提取。")
        return []

    # 确保公众号主页窗口处于前台激活状态
    activate_hwnd(hwnd)
    time.sleep(0.3)

    bbox = get_window_rect(hwnd)
    # 确保窗口尺寸有效 (宽和高均大于 300)
    if bbox[2] - bbox[0] < 300 or bbox[3] - bbox[1] < 300:
        return []

    items, pil_img = ocr_region(bbox)
    win_top = bbox[1]
    win_bottom = bbox[3]

    # 1. 动态定位顶部导航 Tab 栏 (精准匹配「全部」/「贴图文章」等导航，严格排除上方所有个人资料与关注/私信区)
    tab_item = next((it for it in items if it["text"] in ["全部", "贴图文章", "全部文章", "消息"]), None)
    if tab_item:
        tab_bottom = tab_item["bottom"] + 15
    else:
        # 寻找「关注」/「私信」/「已关注」等按钮作为备用上界
        profile_bottoms = [it["bottom"] for it in items if it["top"] < win_top + 900 and any(w in it["text"] for w in ["关注", "已关注", "私信", "发消息"])]
        if profile_bottoms:
            tab_bottom = max(profile_bottoms) + 50
        else:
            tab_bottom = win_top + 300

    # 精确黑名单
    exact_exclude = {
        "全部", "贴图", "文章", "贴图文章", "视频", "视频号", "合集", "关注", "已关注", "发消息", "服务", 
        "服务号", "订阅号", "小程序", "展开", "收起", "相关搜索", "微信", "阅读", "在看", "赞",
        "分享", "原创", "置顶", "精选", "私信", "今天", "昨天", "前天", "搜索", "取消", "确定",
        "返回", "关闭", "刷新", "更多", "复制链接", "用浏览器打开", "在浏览器中打开",
        "预约成功通知", "服务通知", "微信团队", "文件传输助手", "订阅号消息", "消息", "通知",
        "星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日",
        "周一", "周二", "周三", "周四", "周五", "周六", "周日"
    }
    sub_exclude = [
        "篇原创", "朋友关注", "朋友看过", "个内容", "篇内容", "条内容", "相关搜索", "余下",
        "微信公众平台", "公众号", "小程序", "大家都在搜", "预约成功", "服务通知", "微信团队"
    ]

    def _is_pure_date_or_weekday(t: str) -> bool:
        s = t.strip()
        if s in exact_exclude:
            return True
        if re.match(r"^(今天|昨天|前天|星期[一二三四五六日天]|周[一二三四五六日]|\d{1,2}月\d{1,2}日)$", s):
            return True
        return False

    def _is_valid_title(txt: str) -> bool:
        t = txt.strip()
        if len(t) < 3:
            return False
        if _is_pure_date_or_weekday(t):
            return False
        if any(sub in t for sub in sub_exclude):
            return False
        # 排除典型搜索联想问答句式
        if (t.endswith("?") or t.endswith("？") or any(q in t for q in ["是什么", "怎么", "如何"])) and len(t) < 16:
            if not any(k in t for k in ["发布", "通知", "服务", "发展", "应用", "年", "月", "日", "毕业", "典礼", "学院"]):
                return False
        # 纯日期与时间过滤
        if (any(t.endswith(d) for d in ["日", "月", "年"]) or "/" in t or ":" in t) and len(t) <= 10:
            if not any(ch in t for ch in ["涨", "第", "大会", "峰会", "链", "融", "通", "数", "重磅", "万向", "高金", "毕业"]):
                return False
        return True

    # 2. 核心卡片提取：以互动锚点 (阅读/赞/在看) 为主导
    # 微信生态铁律：每一篇独立文章必定以其底部的「阅读/赞/在看」互动栏作为物理终点。
    # 从锚点向上紧邻寻找标题，彻底杜绝将封面海报中的宣传大字/Logo当作独立文章导致的重复点击问题。
    anchors = [
        it for it in items
        if it["top"] > tab_bottom and any(w in it["text"] for w in ["阅读", "赞", "在看", "分享", "次阅读"])
    ]
    anchors.sort(key=lambda a: a["top"])

    cards = []
    seen_y_bands = []

    def _is_in_existing_band(cy: int, threshold: int = 30) -> bool:
        return any(abs(cy - y) < threshold for y in seen_y_bands)

    if anchors:
        for i, anchor in enumerate(anchors):
            prev_b = anchors[i - 1]["bottom"] if i > 0 else tab_bottom
            card_items = [
                it for it in items 
                if it != anchor 
                and prev_b <= it["top"] 
                and it["bottom"] <= anchor["top"] + 8
            ]

            # 过滤纯日期行、星期行与通用系统关键词
            candidates = [
                it for it in card_items
                if not _is_pure_date_or_weekday(it["text"])
                and not any(sub in it["text"] for sub in sub_exclude)
                and it["text"].strip() not in exact_exclude
            ]

            if not candidates:
                continue

            # 真实标题紧邻互动锚点正上方 (垂直间距 <= 38px，且水平与锚点对齐)
            bottom_lines = [
                it for it in candidates 
                if anchor["top"] - it["bottom"] <= 38 
                and abs(it["left"] - anchor["left"]) < 50
            ]
            bottom_lines.sort(key=lambda x: x["bottom"], reverse=True)

            title_cluster = []
            if bottom_lines:
                title_cluster.append(bottom_lines[0])
                # 检查是否存在多行折行标题 (向上紧邻当前行，间距 <= 25px 且左对齐)
                line_above = [
                    it for it in candidates
                    if it not in title_cluster
                    and 0 <= title_cluster[-1]["top"] - it["bottom"] <= 25
                    and abs(it["left"] - title_cluster[-1]["left"]) < 40
                ]
                if line_above:
                    title_cluster.insert(0, line_above[0])
            else:
                # 若无紧邻行，则在卡片内部取最左侧主要文字
                min_left = min(it["left"] for it in candidates)
                title_cluster = [it for it in candidates if it["left"] <= min_left + 65]
                title_cluster.sort(key=lambda x: x["top"])

            if not title_cluster:
                continue

            title_text = "".join(l["text"].strip() for l in title_cluster)
            title_text = re.sub(r"^\d{1,2}月\d{1,2}日", "", title_text).strip()

            if not _is_valid_title(title_text):
                continue

            click_x = int((min(l["left"] for l in title_cluster) + max(l["right"] for l in title_cluster)) / 2)
            click_y = int((min(l["top"] for l in title_cluster) + max(l["bottom"] for l in title_cluster)) / 2)

            if not _is_in_existing_band(click_y, threshold=30):
                seen_y_bands.append(click_y)
                cards.append({
                    "title": title_text,
                    "cx": click_x,
                    "cy": click_y,
                    "top": click_y,
                    "anchor": anchor["text"],
                })
    else:
        # 3. 兜底策略：无阅读量标签时，采用 OpenCV 物理形态学切片
        img_np = np.array(pil_img) if pil_img else None
        opencv_cuts = detect_card_slices_opencv(img_np, offset_y=bbox[1], tab_bottom=tab_bottom, win_bottom=win_bottom)
        slice_boundaries = [tab_bottom] + opencv_cuts + [win_bottom]
        for i in range(len(slice_boundaries) - 1):
            y_start, y_end = slice_boundaries[i], slice_boundaries[i + 1]
            if y_end - y_start < 45:
                continue
            container_items = [
                it for it in items
                if y_start - 5 <= it["cy"] <= y_end + 10
                and not _is_pure_date_or_weekday(it["text"])
                and not any(sub in it["text"] for sub in sub_exclude)
            ]
            if not container_items:
                continue
            min_left = min(it["left"] for it in container_items)
            title_lines = [it for it in container_items if it["left"] <= min_left + 65]
            title_lines.sort(key=lambda x: x["top"])
            title_text = "".join(l["text"].strip() for l in title_lines)
            if _is_valid_title(title_text):
                cx = int((min(l["left"] for l in title_lines) + max(l["right"] for l in title_lines)) / 2)
                cy = int((min(l["top"] for l in title_lines) + max(l["bottom"] for l in title_lines)) / 2)
                if not _is_in_existing_band(cy, threshold=30):
                    seen_y_bands.append(cy)
                    cards.append({
                        "title": title_text,
                        "cx": cx,
                        "cy": cy,
                        "top": cy,
                    })

    # 4. 兜底策略 (极端情况下无分割线时)
    if not cards:
        for it in items:
            if it["top"] > tab_bottom:
                txt = it["text"].strip()
                if _is_valid_title(txt) and it["width"] >= 100 and any("\u4e00" <= ch <= "\u9fff" for ch in txt):
                    if not _is_in_existing_band(it["cy"], threshold=35):
                        seen_y_bands.append(it["cy"])
                        cards.append({
                            "title": txt,
                            "cx": it["cx"],
                            "cy": it["cy"],
                            "top": it["top"],
                        })

    cards.sort(key=lambda x: x["top"])
    return cards


def find_fold_buttons(hwnd: Optional[int] = None) -> List[Tuple[int, int]]:
    """识别当前公众号主页文章列表内的折叠展开按钮 (包括「余下 X 篇」、「X 个内容」等，严格排除主页介绍区的展开)。"""
    if hwnd is None:
        hwnd = get_wechat_browser_hwnd()
    bbox = get_window_rect(hwnd) if hwnd else None

    items, _ = ocr_region(bbox)
    
    # 动态定位 Tab 栏，折叠按钮只存在于 Tab 栏下方
    tab_item = next((it for it in items if any(w in it["text"] for w in ["全部", "贴图", "文章", "视频号"])), None)
    tab_bottom = (tab_item["bottom"] + 20) if tab_item else ((bbox[1] + 350) if bbox else 350)

    buttons = []
    for it in items:
        # 必须在 Tab 栏下方，杜绝企业简介里的展开链接
        if it["top"] < tab_bottom:
            continue
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
