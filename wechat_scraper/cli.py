"""命令行入口: 解析参数并调度公众号文章抓取流程。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List

from .run import grab_account_articles, grab_multiple_accounts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("wechat-scraper")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="微信公众号文章链接自动化提取工具 (影刀级高可靠本地 RPA 版)"
    )
    parser.add_argument("--keyword", "-k", type=str, help="搜索公众号关键词")
    parser.add_argument("--account", "-a", type=str, help="公众号名称 (支持多个，逗号分隔)")
    parser.add_argument("--count", "-n", type=int, default=5, help="每个账号抓取的文章篇数 (默认 5)")
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default="article_links.txt",
        help="【本次最新】链接输出文件路径 (默认 article_links.txt)",
    )
    parser.add_argument(
        "--format",
        "-f",
        choices=["txt", "json", "csv"],
        default="txt",
        help="输出格式: txt(默认仅URL), json(标题+URL), csv(Excel表格格式)",
    )
    parser.add_argument(
        "--rss",
        "-r",
        type=str,
        default="feed.xml",
        help="输出【本次最新】RSS 2.0 订阅源路径 (默认 feed.xml，包含正文全文，设为 none 可禁用)",
    )
    parser.add_argument(
        "--full-links",
        type=str,
        default="all_links.txt",
        help="【全量历史】文章链接归档路径 (默认 all_links.txt)",
    )
    parser.add_argument(
        "--full-rss",
        type=str,
        default="full_feed.xml",
        help="【全量历史】RSS 2.0 订阅源路径 (默认 full_feed.xml)",
    )
    parser.add_argument(
        "--incremental",
        dest="incremental",
        action="store_true",
        default=True,
        help="开启增量抓取 (默认开启，自动跳过历史已抓取的文章)",
    )
    parser.add_argument(
        "--no-incremental",
        dest="incremental",
        action="store_false",
        help="关闭增量抓取 (强制全量重新抓取)",
    )
    parser.add_argument(
        "--push",
        "--github-push",
        dest="github_push",
        action="store_true",
        default=False,
        help="抓取完成后自动 Push 同步到 GitHub Pages 仓库",
    )
    parser.add_argument(
        "--repo",
        "--github-repo",
        type=str,
        default="https://github.com/kingtiger202505/wechat-rss.git",
        help="GitHub 仓库远程 Git 地址",
    )
    parser.add_argument("--config", "-c", type=str, help="从 JSON 配置文件加载参数")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试级别日志")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    keyword = args.keyword
    account = args.account
    count = args.count
    output = args.output
    out_format = args.format
    rss_output = args.rss
    full_links = args.full_links
    full_rss = args.full_rss
    incremental = args.incremental
    github_push = args.github_push
    github_repo = args.repo

    raw_target = account or keyword

    if args.config:
        cfg_path = Path(args.config)
        if not cfg_path.exists():
            logger.error("配置文件不存在: %s", args.config)
            return 1
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            raw_target = cfg.get("account") or cfg.get("keyword") or raw_target
            count = cfg.get("count", count)
            output = cfg.get("output", output)
            out_format = cfg.get("format", out_format)
            rss_output = cfg.get("rss", rss_output)
            full_links = cfg.get("full_output", cfg.get("full_links", full_links))
            full_rss = cfg.get("full_rss", full_rss)
            incremental = cfg.get("incremental", incremental)
            github_push = cfg.get("github_push", github_push)
            github_repo = cfg.get("github_repo", github_repo)
        except Exception as exc:
            logger.error("解析配置文件失败: %s", exc)
            return 1

    # 解析账号列表 (支持 JSON 数组、逗号分隔字符串或单个字符串)
    accounts: List[str] = []
    if isinstance(raw_target, list):
        accounts = [str(x).strip() for x in raw_target if str(x).strip()]
    elif isinstance(raw_target, str) and raw_target.strip():
        accounts = [
            s.strip() for s in raw_target.replace("，", ",").split(",") if s.strip()
        ]

    if not accounts:
        logger.error("缺少必要参数: 请在配置文件或命令行中指定至少一个公众号账号 (account/keyword)")
        parser.print_help()
        return 1

    rss_final = None if str(rss_output).lower() in ["none", "false", ""] else rss_output
    full_rss_final = None if str(full_rss).lower() in ["none", "false", ""] else full_rss

    try:
        if len(accounts) == 1:
            grab_account_articles(
                keyword=accounts[0],
                account_name=accounts[0],
                count=count,
                output_file=output,
                output_format=out_format,
                rss_output=rss_final,
                incremental=incremental,
                github_push=github_push,
                github_repo=github_repo,
            )
        else:
            grab_multiple_accounts(
                accounts=accounts,
                count=count,
                output_file=output,
                output_format=out_format,
                rss_output=rss_final,
                full_links_file=full_links,
                full_rss_output=full_rss_final,
                incremental=incremental,
                github_push=github_push,
                github_repo=github_repo,
            )
        return 0
    except KeyboardInterrupt:
        logger.warning("\n用户手动中断任务。")
        return 130
    except Exception as exc:
        logger.exception("抓取过程发生未捕获异常: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
