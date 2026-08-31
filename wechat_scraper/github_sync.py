"""GitHub 自动同步模块 (将生成的 RSS 与链接自动 push 到 GitHub Pages 免费托管仓库)。"""

from __future__ import annotations

import datetime
import logging
import subprocess
from pathlib import Path
from typing import Optional, List

logger = logging.getLogger("wechat-scraper")

DEFAULT_REPO_URL = "https://github.com/kingtiger202505/wechat-rss.git"


def run_git_cmd(args: List[str], cwd: Path) -> tuple[int, str]:
    """执行 git 命令并返回 (returncode, output)。"""
    try:
        res = subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=30,
        )
        return res.returncode, res.stdout.strip()
    except Exception as exc:
        return -1, str(exc)


def sync_to_github(
    repo_dir: str | Path = ".",
    remote_url: str = DEFAULT_REPO_URL,
    branch: str = "main",
    files_to_sync: Optional[List[str]] = None,
) -> bool:
    """自动将生成的 RSS 文件与文章列表提交并推送到 GitHub 仓库。"""
    p = Path(repo_dir).resolve()
    logger.info("=========================================")
    logger.info("开始同步 RSS 数据到 GitHub 仓库: %s", remote_url)

    # 1. 检查是否初始化 git
    if not (p / ".git").exists():
        logger.info("初始化本地 Git 仓库...")
        run_git_cmd(["init", "-b", branch], p)
        run_git_cmd(["remote", "add", "origin", remote_url], p)
    else:
        code, out = run_git_cmd(["remote", "get-url", "origin"], p)
        if code != 0:
            run_git_cmd(["remote", "add", "origin", remote_url], p)
        elif out != remote_url:
            run_git_cmd(["remote", "set-url", "origin", remote_url], p)

    # 2. 默认同步的关键 RSS 与链接文件
    if not files_to_sync:
        files_to_sync = [
            "feed.xml",
            "full_feed.xml",
            "article_links.txt",
            "all_links.txt",
            "history.json",
            "README.md",
            "index.html",
            ".nojekyll",
            "config.json",
            "requirements.txt",
            "wechat_scraper",
        ]

    added_any = False
    for f in files_to_sync:
        if (p / f).exists():
            code, _ = run_git_cmd(["add", f], p)
            if code == 0:
                added_any = True

    if not added_any:
        logger.warning("未找到待同步的 RSS / 数据文件，跳过 Git Push。")
        return False

    code, status_out = run_git_cmd(["status", "--porcelain"], p)
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not status_out:
        logger.info("文件内容无变化，无需提交。尝试直接推送...")
    else:
        commit_msg = f"Auto update WeChat RSS feeds: {now_str}"
        code, commit_out = run_git_cmd(["commit", "-m", commit_msg], p)
        logger.info("本地提交完成: %s", commit_msg)

    logger.info("正在推送到 GitHub 远程仓库 (git push origin %s)...", branch)
    code, push_out = run_git_cmd(["push", "-u", "origin", branch], p)
    if code != 0:
        logger.debug("直接 push 返回 %d (%s)，尝试 pull --rebase...", code, push_out)
        run_git_cmd(["pull", "--rebase", "origin", branch], p)
        code, push_out = run_git_cmd(["push", "-u", "origin", branch], p)

    if code == 0:
        logger.info("✓ [成功] RSS 数据已成功推送到 GitHub！")
        logger.info("GitHub 页面订阅地址: https://kingtiger202505.github.io/wechat-rss/feed.xml")
        logger.info("全量归档订阅地址: https://kingtiger202505.github.io/wechat-rss/full_feed.xml")
        logger.info("=========================================")
        return True
    else:
        logger.warning("Push 到 GitHub 失败: %s (请确认已配置 Git 认证/SSH Key 或 Token)", push_out)
        logger.info("=========================================")
        return False


if __name__ == "__main__":
    sync_to_github()
