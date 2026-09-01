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

    # 并发预提取未包含图文正文的文章
    if fetch_full_content:
        from concurrent.futures import ThreadPoolExecutor

        def _enrich_record(rec: Dict[str, Any]) -> Dict[str, Any]:
            url = rec.get("url", "").strip()
            content_html = rec.get("content_html", "")
            if (not content_html or len(content_html) < 100) and url.startswith("http"):
                try:
                    details = fetch_article_details(url)
                    if details.get("title"):
                        rec["title"] = details["title"]
                    if details.get("account"):
                        rec["account"] = details["account"]
                    if details.get("cover_url"):
                        rec["cover_url"] = details["cover_url"]
                    if details.get("publish_time"):
                        rec["publish_time"] = details["publish_time"]
                        rec["time"] = details["publish_time"]
                    rec["content_html"] = details.get("content_html", "")
                    rec["content_markdown"] = details.get("content_markdown", "") or details.get("content_text", "")
                except Exception as e:
                    logger.debug("抓取正文与图片异常: %s", e)
            return rec

        with ThreadPoolExecutor(max_workers=8) as executor:
            records = list(executor.map(_enrich_record, records))

    items_xml = []
    for r in records:
        url = r.get("url", "").strip()
        title = r.get("title", "").strip()
        account = r.get("account", "").strip()
        cover_url = r.get("cover_url", "").strip()
        content_html = r.get("content_html", "")
        content_md = r.get("content_markdown", "") or r.get("content_text", "")
        pub_time = r.get("publish_time") or r.get("time") or build_date

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
    current_account = ""

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
                "title": f"【{current_account}】文章" if current_account else "",
                "url": line,
            })

    return records


def convert_txt_to_rss(
    txt_path: str | Path = "article_links.txt",
    output_rss: str | Path = "feed.xml",
    channel_title: str = "微信公众号文章精选",
    sync_history: bool = True,
    full_links_file: Optional[str | Path] = "all_links.txt",
    full_rss_output: Optional[str | Path] = "full_feed.xml",
) -> Path:
    """直接将现有的 article_links.txt 转换为含完整图文的 RSS 文件，并同步更新历史归档。"""
    records = parse_links_txt_to_records(txt_path)
    generate_rss_xml(
        records=records,
        channel_title=channel_title,
        output_path=output_rss,
        fetch_full_content=True,
    )
    if sync_history and records:
        from .history import update_history_and_archives
        full_history, _ = update_history_and_archives(
            new_records=records,
            full_links_file=full_links_file,
        )
        if full_rss_output and full_history:
            generate_rss_xml(
                records=full_history,
                channel_title="微信公众号文章全量归档订阅",
                output_path=full_rss_output,
                fetch_full_content=False,
            )
    return Path(output_rss)


def update_temp_index_html(
    temp_dir: str | Path = "temp",
    base_url: str = "https://kingtiger202505.github.io/wechat-rss/temp",
) -> Path:
    """扫描 temp 目录下的所有 XML 文件，自动生成极具视觉质感的文件目录浏览与一键订阅页面 index.html。"""
    import xml.etree.ElementTree as ET
    t_dir = Path(temp_dir)
    t_dir.mkdir(parents=True, exist_ok=True)

    xml_files = sorted(t_dir.glob("*.xml"), key=lambda p: p.stat().st_mtime, reverse=True)
    cards_html = []

    for f in xml_files:
        try:
            tree = ET.parse(f)
            root = tree.getroot()
            ch_title = root.findtext("./channel/title") or f.stem
            it_title = root.findtext("./channel/item/title") or ch_title
            author = root.findtext("./channel/item/author") or root.findtext("./channel/item/category") or "微信公众号"
            pub_date = root.findtext("./channel/item/pubDate") or root.findtext("./channel/lastBuildDate") or ""
        except Exception:
            ch_title = f.stem
            it_title = f.stem
            author = "未分类"
            pub_date = ""

        size_kb = round(f.stat().st_size / 1024, 1)
        online_url = f"{base_url.rstrip('/')}/{f.name}"

        card = f"""      <div class="feed-card" data-title="{html.escape(it_title.lower())}" data-file="{html.escape(f.name.lower())}" data-author="{html.escape(author.lower())}">
        <div class="feed-header">
          <div class="feed-title-wrap">
            <span class="file-tag">RSS 2.0</span>
            <h3 class="feed-title">{html.escape(it_title)}</h3>
          </div>
          <a class="action-link" href="{html.escape(f.name)}" target="_blank" title="查看原始 XML">查看 XML ↗</a>
        </div>
        <div class="meta-row">
          <span class="meta-item"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg>{html.escape(author)}</span>
          <span class="meta-item"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect><line x1="16" y1="2" x2="16" y2="6"></line><line x1="8" y1="2" x2="8" y2="6"></line><line x1="3" y1="10" x2="21" y2="10"></line></svg>{html.escape(pub_date)}</span>
          <span class="meta-item"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>{size_kb} KB</span>
          <span class="meta-item file-name">{html.escape(f.name)}</span>
        </div>
        <div class="url-box">
          <span class="url-text" id="url-{html.escape(f.stem)}">{online_url}</span>
          <button class="copy-btn" onclick="copyLink('url-{html.escape(f.stem)}', this)">复制订阅链接</button>
        </div>
      </div>"""
        cards_html.append(card)

    cards_str = "\n".join(cards_html) if cards_html else """      <div class="empty-state">
        <p>暂无临时生成的 RSS 订阅文件</p>
        <span class="subtitle">可通过命令快速生成单篇或专题 RSS 源</span>
      </div>"""

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>临时专题订阅与文件目录 - wechat-rss</title>
  <style>
    :root {{
      --bg: #0f172a;
      --card-bg: rgba(30, 41, 59, 0.7);
      --border: rgba(255, 255, 255, 0.1);
      --primary: #38bdf8;
      --primary-hover: #0ea5e9;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --tag-bg: rgba(56, 189, 248, 0.15);
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
    body {{
      background: linear-gradient(135deg, #0f172a 0%, #020617 100%);
      color: var(--text);
      min-height: 100vh;
      padding: 40px 20px;
      display: flex;
      justify-content: center;
    }}
    .container {{
      max-width: 780px;
      width: 100%;
    }}
    .top-nav {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 24px;
    }}
    .back-btn {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--primary);
      text-decoration: none;
      font-size: 14px;
      font-weight: 500;
      padding: 8px 14px;
      background: rgba(15, 23, 42, 0.6);
      border: 1px solid var(--border);
      border-radius: 8px;
      transition: all 0.2s;
    }}
    .back-btn:hover {{
      background: rgba(56, 189, 248, 0.15);
      border-color: var(--primary);
    }}
    .header {{
      background: var(--card-bg);
      backdrop-filter: blur(16px);
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 28px;
      margin-bottom: 24px;
      box-shadow: 0 20px 40px -15px rgba(0, 0, 0, 0.5);
    }}
    .badge {{
      display: inline-block;
      padding: 4px 12px;
      background: var(--tag-bg);
      color: var(--primary);
      border-radius: 20px;
      font-size: 12px;
      font-weight: 600;
      margin-bottom: 10px;
    }}
    h1 {{ font-size: 24px; font-weight: 700; margin-bottom: 8px; }}
    p.subtitle {{ color: var(--text-muted); font-size: 14px; line-height: 1.5; }}
    
    .search-box {{
      margin-top: 18px;
      position: relative;
    }}
    .search-input {{
      width: 100%;
      background: rgba(15, 23, 42, 0.8);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px 16px;
      color: var(--text);
      font-size: 14px;
      outline: none;
      transition: border 0.2s;
    }}
    .search-input:focus {{ border-color: var(--primary); }}
    .search-input::placeholder {{ color: var(--text-muted); }}

    .file-count {{
      font-size: 13px;
      color: var(--text-muted);
      margin-bottom: 16px;
      padding-left: 4px;
    }}

    .card-list {{ display: flex; flex-direction: column; gap: 16px; }}
    .feed-card {{
      background: var(--card-bg);
      backdrop-filter: blur(16px);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 20px 24px;
      transition: all 0.2s ease;
    }}
    .feed-card:hover {{ border-color: rgba(56, 189, 248, 0.4); transform: translateY(-2px); }}
    .feed-header {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
      margin-bottom: 12px;
    }}
    .feed-title-wrap {{ display: flex; flex-direction: column; gap: 6px; }}
    .file-tag {{
      display: inline-block;
      width: fit-content;
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #38bdf8;
      background: rgba(56, 189, 248, 0.12);
      padding: 2px 8px;
      border-radius: 4px;
    }}
    .feed-title {{ font-size: 16px; font-weight: 600; line-height: 1.4; color: var(--text); }}
    .action-link {{
      color: var(--primary);
      font-size: 13px;
      text-decoration: none;
      white-space: nowrap;
      padding: 4px 8px;
      border-radius: 6px;
      background: rgba(56, 189, 248, 0.1);
      transition: background 0.2s;
    }}
    .action-link:hover {{ background: rgba(56, 189, 248, 0.2); }}
    .meta-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 14px;
      font-size: 12px;
      color: var(--text-muted);
      margin-bottom: 14px;
    }}
    .meta-item {{ display: flex; align-items: center; gap: 4px; }}
    .meta-item.file-name {{ font-family: monospace; color: #a5b4fc; }}
    .url-box {{
      display: flex;
      gap: 8px;
      background: rgba(0, 0, 0, 0.35);
      padding: 8px 12px;
      border-radius: 8px;
      border: 1px solid var(--border);
      align-items: center;
    }}
    .url-text {{
      flex: 1;
      font-family: monospace;
      font-size: 12px;
      color: var(--primary);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    button.copy-btn {{
      background: var(--primary);
      color: #0f172a;
      border: none;
      border-radius: 6px;
      padding: 6px 12px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      white-space: nowrap;
      transition: all 0.2s;
    }}
    button.copy-btn:hover {{ background: var(--primary-hover); }}
    .empty-state {{
      text-align: center;
      padding: 48px 20px;
      background: var(--card-bg);
      border-radius: 14px;
      border: 1px dashed var(--border);
    }}
    .footer {{ text-align: center; font-size: 13px; color: var(--text-muted); margin-top: 36px; }}
    .footer a {{ color: var(--primary); text-decoration: none; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="top-nav">
      <a href="../index.html" class="back-btn">← 返回全量订阅主页</a>
      <span style="font-size: 12px; color: var(--text-muted);">更新时间: {now_str}</span>
    </div>

    <div class="header">
      <span class="badge">Directory Listing</span>
      <h1>📁 临时专题文件目录 (/temp/)</h1>
      <p class="subtitle">存放针对特定单篇文章、突发专题或快速分发的临时 RSS 2.0 XML 订阅源，支持一键复制与在线消费。</p>
      
      <div class="search-box">
        <input type="text" id="filterInput" class="search-input" placeholder="🔍 实时过滤文章标题、文件名或作者..." oninput="filterCards()">
      </div>
    </div>

    <div class="file-count">共发现 <span id="visibleCount" style="color: var(--primary); font-weight: 600;">{len(xml_files)}</span> 个临时订阅源</div>

    <div class="card-list" id="cardList">
{cards_str}
    </div>

    <div class="footer">
      Powered by <a href="https://github.com/kingtiger202505/wechat-rss" target="_blank">wechat-rss</a> · 自动化托管于 GitHub Pages
    </div>
  </div>

  <script>
    function copyLink(id, btn) {{
      const text = document.getElementById(id).innerText;
      navigator.clipboard.writeText(text).then(() => {{
        const orig = btn.innerText;
        btn.innerText = "已复制！";
        btn.style.background = "#4ade80";
        setTimeout(() => {{
          btn.innerText = orig;
          btn.style.background = "";
        }}, 1500);
      }});
    }}

    function filterCards() {{
      const q = document.getElementById('filterInput').value.trim().toLowerCase();
      const cards = document.querySelectorAll('#cardList .feed-card');
      let visible = 0;
      cards.forEach(card => {{
        const t = card.getAttribute('data-title') || '';
        const f = card.getAttribute('data-file') || '';
        const a = card.getAttribute('data-author') || '';
        if (!q || t.includes(q) || f.includes(q) || a.includes(q)) {{
          card.style.display = '';
          visible++;
        }} else {{
          card.style.display = 'none';
        }}
      }});
      const countEl = document.getElementById('visibleCount');
      if (countEl) countEl.innerText = visible;
    }}
  </script>
</body>
</html>
"""

    index_path = t_dir / "index.html"
    index_path.write_text(html_content, encoding="utf-8")
    logger.info("已生成 temp 目录浏览页面: %s (共 %d 个文件)", index_path.resolve(), len(xml_files))
    return index_path


def create_single_article_feed(
    url: str,
    output_path: Optional[str | Path] = None,
    channel_title: Optional[str] = None,
) -> Path:
    """针对单篇微信文章直接生成包含全文与配图的独立专题/临时 RSS 订阅源文件。"""
    details = fetch_article_details(url)
    title = details.get("title", "微信精选文章")
    if not output_path:
        import re
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        safe_kw = re.sub(r"[^\w\u4e00-\u9fa5]+", "_", title)[:25].strip("_")
        output_path = Path("temp") / f"{today_str}_{safe_kw}.xml"

    out_p = Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    ch_title = channel_title or f"专题订阅：{title}"
    generate_rss_xml(
        records=[details],
        channel_title=ch_title,
        output_path=out_p,
        fetch_full_content=False,
    )
    # 自动刷新 temp/index.html 目录浏览页
    if out_p.parent.name == "temp" or "temp" in out_p.parts:
        update_temp_index_html(temp_dir=out_p.parent)

    return out_p


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="将微信文章链接文件转换为含完整图文的标准 RSS 2.0 XML 订阅源")
    parser.add_argument("--input", "-i", default="article_links.txt", help="输入的链接文件路径 (默认 article_links.txt)")
    parser.add_argument("--url", "-u", help="单篇微信文章 URL，直接生成独立专题 RSS 源")
    parser.add_argument("--output", "-o", help="输出的 RSS XML 文件路径 (默认 feed.xml 或 temp/日期_关键字.xml)")
    parser.add_argument("--title", "-t", help="RSS 订阅源 Channel 标题")
    parser.add_argument("--no-history", action="store_true", help="不更新全量历史库")
    parser.add_argument("--update-temp-index", action="store_true", help="仅更新 temp 目录下的浏览索引页")
    args = parser.parse_args()

    if args.update_temp_index:
        out = update_temp_index_html()
        print(f"[OK] temp 目录浏览页已生成: {out.resolve()}")
    elif args.url:
        out = create_single_article_feed(args.url, output_path=args.output, channel_title=args.title)
        print(f"[OK] 完整图文 RSS 订阅源已成功生成: {out.resolve()}")
    else:
        out = convert_txt_to_rss(args.input, args.output or "feed.xml", args.title or "微信公众号文章精选", sync_history=not args.no_history)
        print(f"[OK] 完整图文 RSS 订阅源已成功生成: {out.resolve()}")
