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

    def enum_proc(hwnd, lparam):
        rect = get_window_rect(hwnd)
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        title_buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title_buf, 512)
        class_buf = ctypes.create_unicode_buffer(512)
        user32.GetClassNameW(hwnd, class_buf, 512)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            pname = psutil.Process(pid.value).name().lower()
        except Exception:
            pname = ""

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
                "pid": pid.value,
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
    """微信主界面窗口 (Qt5 QWindowIcon 类 + 进程 Weixin.exe，精准排除微信迷你弹窗)。"""
    wins = find_windows("weixin.exe", class_match="Qt")
    q_wins = [w for w in wins if "QWindowIcon" in w["class"]]
    if not q_wins:
        return None

    # 排除标题为 'Weixin' 且尺寸为 300x300 的小浮窗
    for w in q_wins:
        if w["title"] != "Weixin" or (w["w"] > 500 and w["h"] > 400):
            return w["hwnd"]

    return q_wins[-1]["hwnd"]


def get_wechat_browser_hwnd() -> Optional[int]:
    """微信内置浏览器/搜一搜窗口 (Chrome_WidgetWin 类)。"""
    wins = find_windows("wechatappex.exe", class_match="Chrome_WidgetWin")
    for w in wins:
        if w["w"] > 400 and w["h"] > 300:
            return w["hwnd"]
    wins2 = find_windows("weixin.exe", class_match="Chrome_WidgetWin")
    for w in wins2:
        if w["w"] > 400 and w["h"] > 300:
            return w["hwnd"]
    return None


def close_wechat_browser_windows() -> None:
    """关闭所有在抓取过程中打开的微信内置浏览器/搜一搜/公众号主页窗口。"""
    WM_CLOSE = 0x0010
    wins = find_windows("wechatappex.exe", class_match="Chrome_WidgetWin")
    wins += find_windows("weixin.exe", class_match="Chrome_WidgetWin")
    for w in wins:
        if w["w"] > 300 and w["h"] > 200:
            try:
                user32.PostMessageW(w["hwnd"], WM_CLOSE, 0, 0)
            except Exception:
                pass
    time.sleep(0.4)


def activate_hwnd(hwnd: int) -> bool:
    """平滑激活窗口并置顶恢复（绕过 Windows 前台锁，严禁 SW_MAXIMIZE 避免白屏）。"""
    try:
        ensure_interactive_desktop()
        # 模拟按下 Alt 键绕过 Windows 前台锁定
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        user32.ShowWindow(hwnd, 5)  # SW_SHOW
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SwitchToThisWindow(hwnd, True)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.3)
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
