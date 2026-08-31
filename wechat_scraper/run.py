"""微信公众号文章链接抓取主流程 (影刀级确定性本地 RPA 引擎)。"""

from __future__ import annotations

import ctypes
import json
import logging
import time
from pathlib import Path
from typing import List, Optional, Dict, Any

import pyautogui

from .github_sync import sync_to_github, DEFAULT_REPO_URL
from .history import update_history_and_archives, get_history_sets
from .ocr_loc import (
    WeChatBlackScreenError,
    find_account_card_in_search,
    find_article_cards,
    find_dots_button_pos,
    find_fold_buttons,
    find_image_pos,
    find_text_pos,
    ocr_region,
)
from .rss import generate_rss_xml
from .wechat import (
    activate_hwnd,
    clear_clipboard,
    close_wechat_browser_windows,
    ensure_interactive_desktop,
    find_windows,
    get_clipboard,
    get_wechat_browser_hwnd,
    get_wechat_main_hwnd,
    get_window_rect,
    type_text,
    user32,
)

logger = logging.getLogger("wechat-scraper")

pyautogui.PAUSE = 0.15
pyautogui.FAILSAFE = True


def grab_multiple_accounts(
    accounts: List[str],
    count: int = 5,
    output_file: str | Path = "article_links.txt",
    output_format: str = "txt",
    rss_output: Optional[str | Path] = "feed.xml",
    full_links_file: Optional[str | Path] = "all_links.txt",
    full_rss_output: Optional[str | Path] = "full_feed.xml",
    incremental: bool = True,
    github_push: bool = False,
    github_repo: str = DEFAULT_REPO_URL,
    pause_between: float = 0.5,
) -> List[Dict[str, str]]:
    """批量抓取多个公众号的文章链接，并自动生成【本次最新】与【全量历史】双向输出。"""
    out_path = Path(output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    all_results: List[Dict[str, str]] = []
    total_accounts = len(accounts)

    logger.info("=========================================")
    logger.info("开始执行微信多公众号批量抓取任务")
    logger.info("目标公众号列表 (%d 个): %s", total_accounts, ", ".join(accounts))
    logger.info("每个账号抓取篇数: %d | 增量模式: %s", count, "开启" if incremental else "全量覆盖")
    logger.info("本次输出文件: %s | 本次 RSS: %s", out_path, rss_output)
    logger.info("全量归档文件: %s | 全量 RSS: %s", full_links_file, full_rss_output)
    logger.info("=========================================")

    # 启动前清理任何可能残留的搜一搜/浏览器窗口
    close_wechat_browser_windows()

    for i, acc in enumerate(accounts, start=1):
        logger.info("\n>>> 正在处理第 [%d/%d] 个公众号: 【%s】 <<<", i, total_accounts, acc)
        
        if output_format.lower() == "txt" and total_accounts > 1:
            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(f"\n# ==================== 【{acc}】 ====================\n")

        res = grab_account_articles(
            keyword=acc,
            account_name=acc,
            count=count,
            output_file=output_file,
            output_format=output_format,
            rss_output=None,
            incremental=incremental,
            github_push=False,
            pause_between=pause_between,
            clear_existing=False,
        )
        all_results.extend(res)

        logger.info("【%s】抓取完毕，准备进入下一个公众号...", acc)
        _close_current_tab()
        time.sleep(1.0)

    # 全部公众号抓取完毕，关闭所有浏览器/搜一搜窗口并释放焦点
    _clean_close_all_windows()

    # 1. 自动生成【本次运行】的 RSS 2.0 文件 (包含全文正文)
    if rss_output and all_results:
        generate_rss_xml(
            records=all_results,
            channel_title="微信公众号文章聚合订阅 (最新更新)",
            output_path=rss_output,
        )

    # 2. 自动更新【全量历史归档】数据库与全量 RSS
    full_history, new_added = update_history_and_archives(
        new_records=all_results,
        full_links_file=full_links_file,
    )
    if full_rss_output and full_history:
        generate_rss_xml(
            records=full_history,
            channel_title="微信公众号文章全量归档订阅",
            output_path=full_rss_output,
            fetch_full_content=False,  # 历史已有无需重复抓取
        )

    if github_push:
        sync_to_github(remote_url=github_repo)

    logger.info("=========================================")
    logger.info("全部 %d 个公众号抓取任务完成！本次抓取 %d 篇 (新入库 %d 篇，全量累计 %d 篇)", 
                total_accounts, len(all_results), new_added, len(full_history))
    logger.info("本次结果: %s | 本次 RSS: %s", out_path.resolve(), Path(rss_output).resolve() if rss_output else "无")
    if full_links_file:
        logger.info("全量归档: %s | 全量 RSS: %s", Path(full_links_file).resolve(), Path(full_rss_output).resolve() if full_rss_output else "无")
    logger.info("=========================================")
    return all_results


def grab_account_articles(
    *,
    keyword: str,
    account_name: str = "",
    count: int = 5,
    output_file: str | Path = "article_links.txt",
    output_format: str = "txt",
    rss_output: Optional[str | Path] = "feed.xml",
    incremental: bool = True,
    github_push: bool = False,
    github_repo: str = DEFAULT_REPO_URL,
    pause_between: float = 0.5,
    clear_existing: bool = True,
) -> List[Dict[str, str]]:
    """自动化抓取指定公众号的文章列表与链接。"""
    ensure_interactive_desktop()
    target_account = account_name or keyword
    out_path = Path(output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if clear_existing and out_path.exists():
        out_path.unlink()

    results: List[Dict[str, str]] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()

    history_urls, history_titles = set(), set()
    if incremental:
        history_urls, history_titles = get_history_sets()

    logger.info("=========================================")
    logger.info("开始执行公众号【%s】抓取任务", target_account)
    logger.info("目标篇数: %d | 输出格式: %s | 增量模式: %s", count, output_format, "开启" if incremental else "全量")
    logger.info("=========================================")

    if clear_existing:
        _clean_close_all_windows()

    # 1. 搜索并打开公众号主页
    _search_and_open_account(keyword=keyword, account_name=target_account)

    # 2. 循环提取文章
    consecutive_no_new = 0
    max_scroll_attempts = 20

    while len(results) < count and consecutive_no_new < max_scroll_attempts:
        # 1. 自动检测并点击展开当前可视区内的「余下 X 篇」折叠文章
        home_hwnd = get_wechat_browser_hwnd(title_keyword=target_account)
        fold_buttons = find_fold_buttons(hwnd=home_hwnd)
        if fold_buttons:
            for bx, by in fold_buttons:
                logger.info("检测到折叠文章，自动点击「展开」: (%d, %d)", bx, by)
                pyautogui.click(bx, by)
                time.sleep(0.5)

        # 2. 获取当前主页文章卡片 (动态定位「全部」Tab 过滤账号头部元数据)
        cards = find_article_cards(hwnd=home_hwnd)
        new_in_view = [c for c in cards if c["title"] not in seen_titles]

        if not new_in_view:
            logger.info("当前可视区域无新文章，向下滚动加载更多...")
            _scroll_article_list(account_name=target_account)
            consecutive_no_new += 1
            time.sleep(1.2)
            continue

        consecutive_no_new = 0
        for card in new_in_view:
            if len(results) >= count:
                break

            title = card["title"]
            seen_titles.add(title)

            # 增量检查：如果文章标题已经在历史库中，跳过
            if incremental and title in history_titles:
                logger.info("  ! 增量跳过 (历史已抓取): %s", title)
                continue

            idx = len(results) + 1
            logger.info("[%d/%d] 正在处理文章: %s (坐标: %d, %d)", idx, count, title, card["cx"], card["cy"])

            # 点击文章卡片进入正文详情页
            pyautogui.click(card["cx"], card["cy"])
            time.sleep(1.8)

            # 在详情页提取「复制链接」
            url = _copy_link_with_event_wait(article_title=title, home_hwnd=home_hwnd)
            _close_current_tab(home_hwnd=home_hwnd)

            if url and url.startswith("http") and url not in seen_urls:
                if incremental and url in history_urls:
                    logger.info("  ! 增量跳过 (URL 历史已存在): %s", url)
                    continue

                seen_urls.add(url)
                record = {"account": target_account, "title": title, "url": url}
                results.append(record)
                logger.info("  ✓ [成功 %d/%d] %s", len(results), count, url)
                _append_output(record, out_path, output_format)
            elif url in seen_urls:
                logger.warning("  ! 链接重复，跳过")
            else:
                logger.warning("  ✗ [失败] 未能复制到有效链接 (url=%r)", url)

            time.sleep(pause_between)

        # 本屏文章处理完后，滚动翻页
        if len(results) < count:
            _scroll_article_list(account_name=target_account)
            time.sleep(1.0)

    if rss_output and results:
        generate_rss_xml(
            records=results,
            channel_title=f"【{target_account}】微信公众号文章订阅",
            output_path=rss_output,
        )

    if clear_existing and results:
        update_history_and_archives(
            new_records=results,
            full_links_file="all_links.txt",
        )

    # 单账号抓取模式下，抓取完毕后关闭所有浏览器窗口并释放焦点
    if clear_existing:
        _clean_close_all_windows()

    if clear_existing and github_push:
        sync_to_github(remote_url=github_repo)

    logger.info("=========================================")
    logger.info("【%s】抓取完成: 共成功获取 %d 篇文章链接", target_account, len(results))
    logger.info("本次结果: %s", out_path.resolve())
    if rss_output:
        logger.info("本次 RSS: %s", Path(rss_output).resolve())
    logger.info("=========================================")
    return results


def _search_and_open_account(keyword: str, account_name: str, max_retries: int = 2) -> None:
    """搜索关键词并进入公众号主页 (具备黑屏熔断自愈与重试能力)。"""
    for attempt in range(1, max_retries + 1):
        try:
            logger.info("步骤 1: 查找并激活微信主界面 (尝试 %d/%d)...", attempt, max_retries)
            main_hwnd = get_wechat_main_hwnd()
            if not main_hwnd:
                raise RuntimeError("未找到微信主窗口，请确认微信客户端已登录并处于运行状态。")
            activate_hwnd(main_hwnd)
            time.sleep(0.4)

            logger.info("步骤 2: 搜索框输入 '%s' 并选择下拉放大镜「🔍 %s」...", keyword, keyword)
            # 使用微信原生快捷键 Ctrl+F 聚焦搜索框并全选清空
            pyautogui.hotkey("ctrl", "f")
            time.sleep(0.2)
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.1)
            pyautogui.press("backspace")
            time.sleep(0.1)
            type_text(keyword)
            time.sleep(0.6)

            # 扫描下拉菜单，定位排在「搜索网络结果」下方、放大镜旁边的第 1 个精准关键词条目
            m_rect = get_window_rect(main_hwnd)
            dropdown_bbox = (m_rect[0], m_rect[1], m_rect[0] + 500, m_rect[1] + 500)
            items, _ = ocr_region(dropdown_bbox)

            header_bottom = m_rect[1] + 130
            for it in items:
                if "搜索网络结果" in it["text"] or "网络结果" in it["text"]:
                    header_bottom = it["bottom"]
                    break

            target_pos = None
            for it in items:
                if it["top"] >= header_bottom - 5 and keyword in it["text"]:
                    if not any(sub in it["text"] for sub in ["注册", "认证", "公众号", "平台", "模式", "小程序"]):
                        target_pos = (it["cx"], it["cy"])
                        logger.info("定位到下拉放大镜条目「🔍 %s」: %s", it["text"], target_pos)
                        break

            if target_pos:
                pyautogui.click(target_pos[0], target_pos[1])
            else:
                # 兜底：按下方向键选中第 1 个放大镜条目并回车
                logger.info("按方向键选择第 1 个放大镜条目并回车...")
                pyautogui.press("down")
                time.sleep(0.1)
                pyautogui.press("enter")

            # 轮询查找并激活搜一搜结果浏览器窗口 (最多等待 6 秒)
            b_deadline = time.time() + 6.0
            browser_hwnd = None
            while time.time() < b_deadline:
                browser_hwnd = get_wechat_browser_hwnd()
                if browser_hwnd:
                    break
                time.sleep(0.5)

            if browser_hwnd:
                activate_hwnd(browser_hwnd)
                time.sleep(0.5)

            logger.info("步骤 3: 在搜索结果中定位「%s」服务号/公众号卡片...", keyword)
            pos = find_account_card_in_search(keyword, hwnd=browser_hwnd)

            logger.info("点击进入第 1 个公众号主页: %s...", pos)
            pyautogui.click(pos[0], pos[1])
            time.sleep(3.0)

            # 等待并激活新打开的公众号主页窗口
            h_deadline = time.time() + 5.0
            home_hwnd = None
            while time.time() < h_deadline:
                home_hwnd = get_wechat_browser_hwnd(title_keyword=account_name)
                if home_hwnd:
                    break
                time.sleep(0.5)

            if home_hwnd:
                activate_hwnd(home_hwnd)
                time.sleep(0.5)

            logger.info("成功进入公众号主页。")
            return
        except WeChatBlackScreenError as exc:
            logger.warning("搜一搜/主页触发黑屏或空白异常: %s (第 %d/%d 次尝试)", exc, attempt, max_retries)
            if attempt < max_retries:
                logger.info("正在执行自愈清理: 关闭异常窗口并重试搜索...")
                close_wechat_browser_windows()
                time.sleep(0.8)
                main_hwnd = get_wechat_main_hwnd()
                if main_hwnd:
                    activate_hwnd(main_hwnd)
                    pyautogui.press("esc")
                    time.sleep(0.3)
                time.sleep(0.5)
            else:
                raise


def _copy_link_with_event_wait(
    max_retries: int = 3,
    article_title: Optional[str] = None,
    home_hwnd: Optional[int] = None,
) -> str:
    """在文章详情页面定位并点击右上角「…」更多按钮，在下拉菜单中点击「复制链接」。"""
    # 查找文章窗口 (优先查找独立文章窗口，避免激活公众号主页窗口)
    browser_hwnd = None
    if article_title:
        browser_hwnd = get_wechat_browser_hwnd(title_keyword=article_title[:6])
    
    if not browser_hwnd and home_hwnd:
        wins = find_windows("wechatappex.exe", class_match="Chrome_WidgetWin")
        valid_wins = [w for w in wins if w["w"] >= 500 and w["h"] >= 450 and w["hwnd"] != home_hwnd]
        if valid_wins:
            browser_hwnd = valid_wins[0]["hwnd"]

    if not browser_hwnd:
        fg_hwnd = user32.GetForegroundWindow()
        class_buf = ctypes.create_unicode_buffer(512)
        user32.GetClassNameW(fg_hwnd, class_buf, 512)
        if "Chrome_WidgetWin" in class_buf.value or "Qt" in class_buf.value:
            browser_hwnd = fg_hwnd

    if not browser_hwnd:
        browser_hwnd = get_wechat_browser_hwnd()

    if not browser_hwnd:
        logger.warning("未找到微信浏览器/文章详情窗口")
        return ""

    activate_hwnd(browser_hwnd)
    time.sleep(0.3)
    rect = get_window_rect(browser_hwnd)
    win_left, win_top, win_right, win_bottom = rect
    actual_top = max(win_top, 0)
    logger.info("当前文章操作窗口 HWND=%s, 尺寸=(%d, %d, %d, %d)", browser_hwnd, win_left, win_top, win_right, win_bottom)

    # 1. 优先通过图色或 OCR 识别定位「…」更多按钮
    primary_dots_pos = find_dots_button_pos(hwnd=browser_hwnd)

    candidate_positions = [
        primary_dots_pos,
        (win_right - 170, actual_top + 22),
        (win_right - 240, actual_top + 40),
        (win_right - 160, actual_top + 22),
        (win_right - 180, actual_top + 22),
        (win_right - 150, actual_top + 22),
    ]
    seen_pos = set()
    unique_candidates = []
    for pos in candidate_positions:
        if pos not in seen_pos:
            seen_pos.add(pos)
            unique_candidates.append(pos)

    for attempt in range(1, max_retries + 1):
        clear_clipboard()

        for dots_x, dots_y in unique_candidates:
            logger.info("点击文章详情页右上角「…」按钮: (%d, %d)...", dots_x, dots_y)
            pyautogui.click(dots_x, dots_y)

            # 宽范围覆盖右上角下拉菜单区域 (杜绝任何边缘裁切)
            menu_bbox = (
                max(0, win_right - 650),
                actual_top,
                win_right,
                min(win_bottom, actual_top + 800),
            )
            copy_pos = None

            # 轮询识别下拉菜单中的「复制链接」/「复制」(等待动画展开，最长 1.5 秒)
            poll_deadline = time.time() + 1.5
            while time.time() < poll_deadline:
                items, _ = ocr_region(menu_bbox)
                for it in items:
                    txt = it["text"].replace(" ", "")
                    # 匹配「复制链接」、「复制」、「链接地址」等，坚决排除「转发给朋友」与「发送」
                    if ("复制" in txt or "链接" in txt) and not any(
                        w in txt for w in ["转", "发", "朋友", "圈", "收藏", "浏览器", "浮窗", "投诉", "搜索", "在看", "赞"]
                    ):
                        copy_pos = (it["cx"], it["cy"])
                        logger.info("在「…」下拉菜单中 OCR 定位到「%s」: (%d, %d)", it["text"], it["cx"], it["cy"])
                        break
                if copy_pos:
                    break
                time.sleep(0.1)

            # 次选图色匹配「复制链接」
            if not copy_pos:
                copy_pos = (
                    find_image_pos("copy_link.png", bbox=menu_bbox, threshold=0.75)
                    or find_image_pos("copy.png", bbox=menu_bbox, threshold=0.75)
                )

            if copy_pos:
                logger.info("点击下拉菜单中的「复制链接」: (%d, %d)...", copy_pos[0], copy_pos[1])
                pyautogui.click(copy_pos[0], copy_pos[1])

                # 事件驱动轮询剪贴板 (最长等待 2.5 秒)
                deadline = time.time() + 2.5
                while time.time() < deadline:
                    text = get_clipboard()
                    if text.startswith("http"):
                        return text
                    time.sleep(0.05)

                logger.warning("点击了「复制链接」但剪贴板未获取到有效 URL，重试...")
            else:
                logger.debug("当前点击位置 (%d, %d) 未能弹出包含「复制链接」的菜单", dots_x, dots_y)
                pyautogui.press("esc")
                time.sleep(0.2)

        # 3. 兜底策略：在文章正文区右键弹出上下文菜单
        body_x = (win_left + win_right) // 2
        body_y = actual_top + int((win_bottom - win_top) * 0.45)
        logger.info("尝试在正文区右键提取复制链接: (%d, %d)...", body_x, body_y)
        pyautogui.rightClick(body_x, body_y)
        time.sleep(0.5)
        context_bbox = (max(0, body_x - 50), max(0, body_y - 50), min(win_right, body_x + 350), min(win_bottom, body_y + 450))
        c_items, _ = ocr_region(context_bbox)
        if c_items:
            logger.info("正文右键菜单 OCR 识别条目: %s", [it["text"] for it in c_items])
        for it in c_items:
            txt = it["text"].replace(" ", "")
            if ("复制链接" in txt or "链接地址" in txt or ("复制" in txt and "链接" in txt)) and not any(w in txt for w in ["转", "发", "朋友", "圈", "收藏", "浮窗"]):
                logger.info("右键菜单中定位到「%s」: (%d, %d)", it["text"], it["cx"], it["cy"])
                pyautogui.click(it["cx"], it["cy"])
                deadline = time.time() + 2.5
                while time.time() < deadline:
                    text = get_clipboard()
                    if text.startswith("http"):
                        return text
                    time.sleep(0.05)
                break
        pyautogui.press("esc")
        time.sleep(0.3)

        logger.debug("第 %d 次提取文章链接未成功，短暂等待后重试...", attempt)
        time.sleep(0.4)

    return ""


def _close_current_tab(article_hwnd: Optional[int] = None, home_hwnd: Optional[int] = None) -> None:
    """安全关闭当前文章标签页/独立窗口，并重新激活主页窗口。"""
    WM_CLOSE = 0x0010
    if article_hwnd and home_hwnd and article_hwnd != home_hwnd:
        try:
            user32.PostMessageW(article_hwnd, WM_CLOSE, 0, 0)
            time.sleep(0.3)
        except Exception:
            pass
    else:
        # 单窗口标签模式：发送 Ctrl+W
        browser_hwnd = article_hwnd or get_wechat_browser_hwnd()
        if browser_hwnd:
            activate_hwnd(browser_hwnd)
            time.sleep(0.15)
            pyautogui.hotkey("ctrl", "w")
            time.sleep(0.3)

    if home_hwnd:
        activate_hwnd(home_hwnd)
        time.sleep(0.2)


def _clean_close_all_windows() -> None:
    """最后全部关闭：优雅关闭微信内置浏览器/搜一搜独立窗口，安全保留微信主界面。"""
    logger.info("正在清理并关闭所有抓取相关独立浏览器窗口...")
    close_wechat_browser_windows()


def _close_if_article_open() -> None:
    """安全关闭当前可能残留的标签页。"""
    _close_current_tab()


def _scroll_article_list(scroll_amount: int = 600, account_name: Optional[str] = None) -> None:
    """平滑向下滚动公众号文章列表。"""
    browser_hwnd = get_wechat_browser_hwnd(title_keyword=account_name)
    if browser_hwnd:
        rect = get_window_rect(browser_hwnd)
        scroll_x = (rect[0] + rect[2]) // 2
        scroll_y = rect[1] + int((rect[3] - rect[1]) * 0.6)
    else:
        scroll_x, scroll_y = 1200, 1100
    pyautogui.moveTo(scroll_x, scroll_y)
    time.sleep(0.1)
    pyautogui.scroll(-scroll_amount)
    time.sleep(0.8)


def _append_output(record: Dict[str, str], out_path: Path, output_format: str) -> None:
    """追加写入抓取结果。"""
    if output_format.lower() == "json":
        existing = []
        if out_path.exists() and out_path.stat().st_size > 0:
            try:
                existing = json.loads(out_path.read_text(encoding="utf-8"))
            except Exception:
                existing = []
        existing.append(record)
        out_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    elif output_format.lower() == "csv":
        is_new = not out_path.exists() or out_path.stat().st_size == 0
        with out_path.open("a", encoding="utf-8-sig") as fh:
            if is_new:
                fh.write("account,title,url\n")
            acc_escaped = record.get("account", "").replace('"', '""')
            title_escaped = record["title"].replace('"', '""')
            fh.write(f'"{acc_escaped}","{title_escaped}","{record["url"]}"\n')
    else:  # txt
        with out_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{record['url']}\n")
