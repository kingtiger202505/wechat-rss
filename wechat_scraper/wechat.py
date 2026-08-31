"""微信桌面端控制: 稳定可靠的窗口管理与输入交互。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import time
from typing import Optional, List, Dict, Any

import pyautogui
import pyperclip
import psutil

# 1. 严格在所有 GUI 模块前设置 Per-Monitor v2 DPI 感知，保证物理像素 1:1 对齐
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

pyautogui.PAUSE = 0.15
pyautogui.FAILSAFE = True


def ensure_interactive_desktop() -> bool:
    """挂载到当前交互桌面 WinSta0\\Default，杜绝后台截图失败。"""
    try:
        h_input = user32.OpenInputDesktop(0, False, 0x01FF)
        if h_input:
            user32.SetThreadDesktop(h_input)
            return True
    except Exception:
        pass
    return False


# 初始化交互桌面
ensure_interactive_desktop()


def get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """获取窗口物理像素坐标矩形 (left, top, right, bottom)。"""
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return (rect.left, rect.top, rect.right, rect.bottom)


def find_windows(
    pname_match: str,
    class_match: Optional[str] = None,
    title_match: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """查找符合条件的顶层窗口列表。"""
    ensure_interactive_desktop()
    h_input = user32.OpenInputDesktop(0, False, 0x01FF)
    results = []
    pid_cache: Dict[int, str] = {}

    def enum_proc(hwnd, lparam):
        rect = get_window_rect(hwnd)
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        title_buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title_buf, 512)
        class_buf = ctypes.create_unicode_buffer(512)
        user32.GetClassNameW(hwnd, class_buf, 512)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        
        pid_val = pid.value
        if pid_val not in pid_cache:
            try:
                pid_cache[pid_val] = psutil.Process(pid_val).name().lower()
            except Exception:
                pid_cache[pid_val] = ""
        pname = pid_cache[pid_val]

        if pname_match.lower() in pname:
            if class_match and not class_buf.value.startswith(class_match):
                return True
            if title_match and title_match not in title_buf.value:
                return True
            # 过滤托盘消息窗口
            if "Tray" in class_buf.value or "Message" in class_buf.value:
                return True
            results.append({
                "hwnd": hwnd,
                "pid": pid_val,
                "pname": pname,
                "title": title_buf.value,
                "class": class_buf.value,
                "rect": rect,
                "w": w,
                "h": h,
            })
        return True

    EnumDesktopWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    if h_input:
        user32.EnumDesktopWindows(h_input, EnumDesktopWindowsProc(enum_proc), 0)
    else:
        user32.EnumWindows(EnumDesktopWindowsProc(enum_proc), 0)
    return results


def get_wechat_main_hwnd() -> Optional[int]:
    """微信主界面窗口 (Qt5 QWindowIcon 类 + 进程 Weixin.exe，精准排除微信迷你弹窗与托盘最小化适配)。"""
    wins = find_windows("weixin.exe", class_match="Qt")
    q_wins = [w for w in wins if "QWindowIcon" in w["class"]]
    if not q_wins:
        return None

    # 优先匹配标题为 '微信' 或 'WeChat' 的主窗口 (排除 300x300 的浮窗)
    for w in q_wins:
        hwnd = w["hwnd"]
        if w["title"] in ["微信", "WeChat"]:
            return hwnd
        if user32.IsIconic(hwnd) and w["title"] != "Weixin":
            return hwnd

    # 尺寸匹配：宽度 > 500
    for w in q_wins:
        if w["title"] != "Weixin" and (w["w"] > 500 and w["h"] > 400):
            return w["hwnd"]

    return q_wins[-1]["hwnd"]


def get_wechat_browser_hwnd(title_keyword: Optional[str] = None) -> Optional[int]:
    """微信内置浏览器/搜一搜/公众号主页窗口 (Chrome_WidgetWin 类，严格过滤右键菜单等微小弹窗)。"""
    wins = find_windows("wechatappex.exe", class_match="Chrome_WidgetWin")
    wins += find_windows("weixin.exe", class_match="Chrome_WidgetWin")
    
    # 严格过滤尺寸过小的浮动弹窗/右键菜单 (通常小于 500x450)，且确保窗口存在
    valid_wins = [
        w for w in wins 
        if w["w"] >= 500 and w["h"] >= 450 and user32.IsWindow(w["hwnd"])
    ]
    if not valid_wins:
        return None

    # 1. 若指定了关键词，优先匹配标题包含关键词的独立主页窗口
    if title_keyword:
        for w in valid_wins:
            if w["title"] and title_keyword in w["title"]:
                return w["hwnd"]

    # 2. 优先匹配当前可见且面积最大的有效窗口
    visible_wins = [w for w in valid_wins if user32.IsWindowVisible(w["hwnd"])]
    target_list = visible_wins if visible_wins else valid_wins
    target_list.sort(key=lambda w: w["w"] * w["h"], reverse=True)
    return target_list[0]["hwnd"]


def close_wechat_browser_windows() -> None:
    """优雅关闭抓取过程中打开的微信独立浏览器/搜一搜/公众号主页窗口 (保留微信主界面并保持 CEF 渲染正常)。"""
    WM_CLOSE = 0x0010
    main_hwnd = get_wechat_main_hwnd()
    
    # 查找独立的 AppEx / 搜一搜浏览器窗口，发送 WM_CLOSE 优雅退出
    wins = find_windows("wechatappex.exe", class_match="Chrome_WidgetWin")
    for w in wins:
        hwnd = w["hwnd"]
        if hwnd != main_hwnd and w["w"] > 300 and w["h"] > 200:
            try:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass
    time.sleep(0.3)


def activate_hwnd(hwnd: int) -> bool:
    """平滑激活窗口并置顶恢复 (针对系统托盘隐藏状态智能唤醒，杜绝白屏与二次最小化)。"""
    try:
        ensure_interactive_desktop()
        # 1. 如果窗口处于托盘隐藏状态 (IsWindowVisible 为 False)，使用快捷键从托盘唤醒
        if not user32.IsWindowVisible(hwnd):
            pyautogui.hotkey("ctrl", "alt", "w")
            time.sleep(0.6)

        # 2. 如果窗口处于最小化状态，调用 SW_RESTORE 恢复
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            time.sleep(0.2)
        else:
            user32.ShowWindow(hwnd, 5)  # SW_SHOW

        # 3. 绕过 Windows 前台锁定并置顶
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        user32.SwitchToThisWindow(hwnd, True)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.2)
        return True
    except Exception:
        return False


def type_text(text: str) -> None:
    """通过剪贴板安全键入文本。"""
    pyperclip.copy(text)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.15)


def clear_clipboard() -> None:
    """清空剪贴板。"""
    pyperclip.copy("")


def get_clipboard() -> str:
    """获取剪贴板文本。"""
    return (pyperclip.paste() or "").strip()

