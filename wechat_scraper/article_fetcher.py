"""微信公众号文章内容抓取与清洗模块 (完整图文、Markdown转换与反盗链处理)。"""

from __future__ import annotations

import html
import re
import urllib.request
import logging
from typing import Dict, Any, List, Optional
from bs4 import BeautifulSoup, Tag, NavigableString

logger = logging.getLogger("wechat-scraper")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _element_to_markdown(el: Tag | NavigableString, seen_images: set[str]) -> str:
    """将 HTML 节点转换为包含内嵌图片 Markdown 的格式化文本。"""
    if isinstance(el, NavigableString):
        txt = str(el).strip()
        return txt if txt else ""

    if not isinstance(el, Tag):
        return ""

    if el.name in ["script", "style", "svg"]:
        return ""

    if el.name == "img":
        img_url = el.get("data-src") or el.get("src") or ""
        if img_url and img_url.startswith("http") and img_url not in seen_images:
            seen_images.add(img_url)
            return f"\n\n![文章配图]({img_url})\n\n"
        return ""

    # 递归处理子节点
    parts = []
    for child in el.children:
        part = _element_to_markdown(child, seen_images)
        if part:
            parts.append(part)

    res = "".join(parts).strip()
    if el.name in ["p", "section", "div", "h1", "h2", "h3", "h4", "h5", "li"]:
        if res:
            return f"\n\n{res}\n\n"
    return res


def fetch_article_details(url: str, timeout: int = 10) -> Dict[str, Any]:
    """通过 URL 抓取微信公众号文章的完整元数据、全部高清配图与正文内容。"""
    result = {
        "url": url,
        "title": "",
        "account": "",
        "cover_url": "",
        "publish_time": "",
        "images": [],
        "content_markdown": "",
        "content_html": "",
        "content_text": "",
        "digest": "",
    }

    if not url or not url.startswith("http"):
        return result

    try:
        req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html_raw = resp.read().decode("utf-8", errors="ignore")

        soup = BeautifulSoup(html_raw, "html.parser")

        # 1. 提取真实标题
        title_el = soup.find("h1", id="activity-name") or soup.find("meta", property="og:title")
        if title_el:
            if hasattr(title_el, "get_text"):
                result["title"] = title_el.get_text().strip()
            else:
                result["title"] = str(title_el.get("content", "")).strip()

        # 2. 提取公众号名称
        account_el = (
            soup.find("a", id="js_name")
            or soup.find("span", class_="rich_media_meta_nickname")
            or soup.find("meta", property="og:article:author")
        )
        if account_el:
            if hasattr(account_el, "get_text"):
                result["account"] = account_el.get_text().strip()
            else:
                result["account"] = str(account_el.get("content", "")).strip()

        # 3. 提取文章封面大图
        cover_meta = soup.find("meta", property="og:image")
        if cover_meta and cover_meta.get("content"):
            result["cover_url"] = str(cover_meta["content"]).strip()

        # 4. 提取文章描述/摘要
        desc_meta = soup.find("meta", property="og:description")
        if desc_meta:
            result["digest"] = str(desc_meta.get("content", "")).strip()

        # 5. 提取发布时间
        time_match = re.search(r'var\s+createTime\s*=\s*["\']([^"\']+)["\']', html_raw)
        if not time_match:
            time_match = re.search(r'var\s+createTime\s*=\s*(\d+)', html_raw)
        if not time_match:
            time_match = re.search(r'var\s+ct\s*=\s*["\']?(\d+)["\']?', html_raw)

        if time_match:
            try:
                import datetime
                val = time_match.group(1).strip()
                if val.isdigit():
                    ts = int(val)
                    if len(val) == 13:
                        ts = ts // 1000
                    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone(datetime.timedelta(hours=8)))
                    result["publish_time"] = dt.strftime("%Y-%m-%d %H:%M:%S")
                elif "-" in val:
                    result["publish_time"] = val
            except Exception:
                pass

        if not result["publish_time"]:
            time_el = soup.find("em", id="publish_time") or soup.find("span", id="publish_time")
            if time_el and time_el.get_text().strip():
                result["publish_time"] = time_el.get_text().strip()

        # 6. 提取与清洗正文
        content_div = soup.find("div", id="js_content")
        if content_div:
            # 清理隐藏元素与无用脚本
            for s in content_div(["script", "style", "noscript"]):
                s.decompose()

            # 处理所有图片：修复 data-src 为真实 src，并注入反防盗链 referrerpolicy
            article_images: List[str] = []
            for img in content_div.find_all("img"):
                real_src = img.get("data-src") or img.get("src")
                if real_src and real_src.startswith("http"):
                    img["src"] = real_src
                    img["referrerpolicy"] = "no-referrer"
                    if real_src not in article_images:
                        article_images.append(real_src)

            result["images"] = article_images
            result["content_html"] = str(content_div)

            # 生成含图片的 Markdown 格式 (专供 AI Agent 与多模态大模型消费)
            seen_imgs: set[str] = set()
            raw_md = _element_to_markdown(content_div, seen_imgs)
            clean_md = re.sub(r"\n{3,}", "\n\n", raw_md).strip()
            result["content_markdown"] = clean_md
            result["content_text"] = content_div.get_text(separator="\n").strip()

    except Exception as exc:
        logger.debug("抓取正文与图片失败 %s: %s", url, exc)

    return result
