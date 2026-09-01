"""历史记录与全量/增量管理模块 (用于 AI Agent 增量更新与全量归档)。"""

from __future__ import annotations

import json
import logging
import datetime
from pathlib import Path
from typing import List, Dict, Any, Set, Tuple, Optional

logger = logging.getLogger("wechat-scraper")

DEFAULT_HISTORY_FILE = "history.json"


def load_history(history_file: str | Path = DEFAULT_HISTORY_FILE) -> List[Dict[str, Any]]:
    """加载历史已抓取的文章列表。"""
    p = Path(history_file)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception as exc:
        logger.warning("读取历史记录文件失败，将重建: %s", exc)
    return []


def get_history_sets(history_file: str | Path = DEFAULT_HISTORY_FILE) -> Tuple[Set[str], Set[str]]:
    """获取历史已有的 URL 集合与标题集合 (用于增量去重)。"""
    history = load_history(history_file)
    urls = {r.get("url", "").strip() for r in history if r.get("url")}
    titles = {r.get("title", "").strip() for r in history if r.get("title")}
    return urls, titles


def update_history_and_archives(
    new_records: List[Dict[str, Any]],
    history_file: str | Path = DEFAULT_HISTORY_FILE,
    full_links_file: Optional[str | Path] = "all_links.txt",
) -> Tuple[List[Dict[str, Any]], int]:
    """将本次新增记录合并到历史全量数据库与全量链接文件中。

    返回: (合并后的全量记录列表, 实际新增篇数)
    """
    history = load_history(history_file)
    existing_urls = {r.get("url", "").strip() for r in history if r.get("url")}

    added_count = 0
    for r in new_records:
        url = r.get("url", "").strip()
        if url and url not in existing_urls:
            if "time" not in r:
                r["time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            history.append(r)
            existing_urls.add(url)
            added_count += 1

    # 写回全量 JSON 数据库
    hp = Path(history_file)
    hp.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

    # 写回全量 TXT 文件 (带公众号分组)
    if full_links_file:
        flp = Path(full_links_file)
        flp.parent.mkdir(parents=True, exist_ok=True)

        grouped: Dict[str, List[str]] = {}
        for r in history:
            acc = r.get("account", "未分类公众号")
            grouped.setdefault(acc, []).append(r.get("url", ""))

        lines = []
        for acc, urls in grouped.items():
            lines.append(f"\n# ==================== 【{acc}】 ====================")
            for u in urls:
                if u:
                    lines.append(u)

        flp.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")

    return history, added_count


def enrich_history_records(
    history_file: str | Path = DEFAULT_HISTORY_FILE,
    max_workers: int = 8,
) -> int:
    """自动为历史记录中缺失正文、Markdown和配图的文章补全完整内容。"""
    from concurrent.futures import ThreadPoolExecutor
    from .article_fetcher import fetch_article_details

    history = load_history(history_file)
    if not history:
        return 0

    to_enrich_indices = [
        i for i, r in enumerate(history)
        if not r.get("content_html") or len(r.get("content_html", "")) < 100
    ]

    if not to_enrich_indices:
        return 0

    logger.info("检测到历史库有 %d 篇文章未包含图文正文，正在并发补全...", len(to_enrich_indices))

    def _fetch(idx: int) -> Tuple[int, Dict[str, Any]]:
        rec = history[idx]
        url = rec.get("url", "").strip()
        if url.startswith("http"):
            try:
                details = fetch_article_details(url)
                return idx, details
            except Exception as e:
                logger.debug("补全文章内容失败 (%s): %s", url, e)
        return idx, {}

    updated = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for idx, details in executor.map(_fetch, to_enrich_indices):
            if details:
                if details.get("title") and (not history[idx].get("title") or "微信文章_" in history[idx]["title"]):
                    history[idx]["title"] = details["title"]
                if details.get("cover_url"):
                    history[idx]["cover_url"] = details["cover_url"]
                if details.get("content_html"):
                    history[idx]["content_html"] = details["content_html"]
                if details.get("content_markdown"):
                    history[idx]["content_markdown"] = details["content_markdown"]
                updated += 1

    hp = Path(history_file)
    hp.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("已成功为历史库补全 %d 篇图文全文！", updated)
    return updated
