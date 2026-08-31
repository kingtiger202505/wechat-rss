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
