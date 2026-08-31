"""微信公众号文章 RSS 2.0 生成模块 (包含高清配图、HTML富文本、Markdown与防盗链优化)。"""

from __future__ import annotations

import datetime
import html
import logging
from email.utils import format_datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

from .article_fetcher import fetch_article_details

logger = logging.getLogger("wechat-scraper")


def generate_rss_xml(
    records: List[Dict[str, Any]],
    channel_title: str = "微信公众号文章更新",
    channel_link: str = "https://mp.weixin.qq.com",
    channel_desc: str = "微信公众号自动化采集 RSS Feed，包含全文正文与高清配图，专供 AI Agent 订阅与批量抓取分析。",
    output_path: Optional[str | Path] = "feed.xml",
    fetch_full_content: bool = True,
) -> str:
    """将文章抓取记录转换为标准 RSS 2.0 XML 格式（保留完整图文内容与封面）。"""
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    build_date = format_datetime(now)

    items_xml = []
    for r in records:
        url = r.get("url", "").strip()
        title = r.get("title", "").strip()
        account = r.get("account", "").strip()
        cover_url = r.get("cover_url", "").strip()
        content_html = r.get("content_html", "")
        content_md = r.get("content_markdown", "") or r.get("content_text", "")
        pub_time = r.get("publish_time") or r.get("time") or build_date

        # 如果未包含完整图文，在线提取正文与全部配图
        if fetch_full_content and (not content_html or len(content_html) < 100) and url.startswith("http"):
            try:
                details = fetch_article_details(url)
                if details.get("title"):
                    title = details["title"]
                if details.get("account") and not account:
                    account = details["account"]
                if details.get("cover_url"):
                    cover_url = details["cover_url"]
                content_html = details.get("content_html", "")
                content_md = details.get("content_markdown", "") or details.get("content_text", "")
            except Exception as e:
                logger.debug("抓取正文与图片异常: %s", e)

        if not title:
            title = f"【{account}】文章" if account else "微信文章"
        if not content_md:
            content_md = f"来源公众号: {account}\n原文链接: {url}"
        if not content_html:
            content_html = f"<p>来源公众号: {html.escape(account)}</p><p><a href='{html.escape(url)}'>查看原文</a></p>"

        # 生成图片标签节点 (enclosure / media)
        media_tags = ""
        if cover_url:
            media_tags = f"""      <enclosure url="{html.escape(cover_url)}" type="image/jpeg" length="0" />
      <media:content url="{html.escape(cover_url)}" medium="image" />
      <media:thumbnail url="{html.escape(cover_url)}" />"""

        item = f"""    <item>
      <title><![CDATA[{title}]]></title>
      <link>{html.escape(url)}</link>
      <guid isPermaLink="true">{html.escape(url)}</guid>
      <author><![CDATA[{account}]]></author>
      <category><![CDATA[{account}]]></category>
      <pubDate>{pub_time}</pubDate>
{media_tags}
      <description><![CDATA[{content_md}]]></description>
      <content:encoded><![CDATA[{content_html}]]></content:encoded>
    </item>"""
        items_xml.append(item)

    items_str = "\n".join(items_xml)

    rss_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" 
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:media="http://search.yahoo.com/mrss/"
     xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title><![CDATA[{channel_title}]]></title>
    <link>{html.escape(channel_link)}</link>
    <description><![CDATA[{channel_desc}]]></description>
    <language>zh-cn</language>
    <lastBuildDate>{build_date}</lastBuildDate>
    <generator>wechat-auto-scraper</generator>
{items_str}
  </channel>
</rss>
"""

    if output_path:
        out_p = Path(output_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(rss_content, encoding="utf-8")
        logger.info("RSS 订阅源已生成: %s (共 %d 篇文章，含完整图文)", out_p.resolve(), len(records))

    return rss_content


def parse_links_txt_to_records(txt_path: str | Path) -> List[Dict[str, Any]]:
    """解析已有的 article_links.txt 文件，还原为带公众号归属的文章记录列表。"""
    p = Path(txt_path)
    if not p.exists():
        return []

    lines = p.read_text(encoding="utf-8").splitlines()
    records = []
    current_account = "微信公众号"

    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if "【" in line and "】" in line:
                current_account = line.split("【")[1].split("】")[0].strip()
            elif ":" in line:
                current_account = line.split(":")[-1].replace("]", "").strip()
            continue

        if line.startswith("http"):
            records.append({
                "account": current_account,
                "title": f"【{current_account}】文章分享",
                "url": line,
            })

    return records


def convert_txt_to_rss(
    txt_path: str | Path = "article_links.txt",
    output_rss: str | Path = "feed.xml",
    channel_title: str = "微信公众号文章精选",
) -> Path:
    """直接将现有的 article_links.txt 转换为含完整图文的 RSS 文件。"""
    records = parse_links_txt_to_records(txt_path)
    generate_rss_xml(
        records=records,
        channel_title=channel_title,
        output_path=output_rss,
        fetch_full_content=True,
    )
    return Path(output_rss)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="将微信文章链接文件转换为含完整图文的标准 RSS 2.0 XML 订阅源")
    parser.add_argument("--input", "-i", default="article_links.txt", help="输入的链接文件路径 (默认 article_links.txt)")
    parser.add_argument("--output", "-o", default="feed.xml", help="输出的 RSS XML 文件路径 (默认 feed.xml)")
    parser.add_argument("--title", "-t", default="微信公众号文章精选", help="RSS 订阅源 Channel 标题")
    args = parser.parse_args()

    out = convert_txt_to_rss(args.input, args.output, args.title)
    print(f"[OK] 完整图文 RSS 订阅源已成功生成: {out.resolve()}")
