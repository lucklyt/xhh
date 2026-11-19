"""小黑盒社区 CLI 子命令入口."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import webbrowser
from typing import Any, Sequence

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from xhh import __version__
from xhh.client import HeyboxAPIError, HeyboxClient
from xhh.config import SessionData
from xhh.models import PostSummary, format_count, format_time, parse_floor_range
from xhh.qr import render_qr_half_blocks, render_qr_rich, save_qr_html

console = Console()

COMMENT_SORT_API_MAP: dict[str, str] = {
    "hot": "hot",
    "asc": "time_aes",
    "desc": "time_desc",
}


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _print_posts_table(posts: list[PostSummary], title: str) -> None:
    if not posts:
        console.print("[dim]暂无帖子数据[/dim]")
        return

    table = Table(title=title, show_lines=False, header_style="bold cyan")
    table.add_column("#", style="dim", width=2, justify="right")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("板块", style="magenta", max_width=10)
    table.add_column("标题", style="bold white", min_width=24, ratio=3, overflow="fold")
    table.add_column("作者", style="yellow", max_width=10)
    table.add_column("赞/充/评", style="green", justify="right", no_wrap=True)
    table.add_column("时间", style="dim", justify="right", no_wrap=True)

    for idx, p in enumerate(posts, start=1):
        badge = "[red][热][/red] " if p.is_hot_post else ""
        media = " 🎬" if p.has_video else (f" 🖼{p.image_count}" if p.image_count else "")
        stats = f"👍{format_count(p.up)} ⚡{format_count(p.award_num)} 💬{format_count(p.comment_num)}"
        tm = p.create_str or format_time(p.create_at)
        table.add_row(
            str(idx),
            p.linkid,
            p.topic_name or "-",
            f"{badge}{p.title}{media}",
            p.author or "-",
            stats,
            tm,
        )

    console.print(table)
    console.print("[dim]提示: 运行 `xhh post <linkid>` 可阅读完整正文与评论区[/dim]")


async def _cmd_login(dry_run: bool = False, large: bool = False, open_browser: bool = False) -> int:
    async with HeyboxClient() as client:
        qr_info = await client.get_qrcode()
        qr_url = qr_info["qr_url"]
        if console.is_terminal:
            qr_renderable = render_qr_rich(qr_url, border=1, compact=not large)
        else:
            qr_renderable = Text(render_qr_half_blocks(qr_url, border=1), no_wrap=True)
        console.print(
            Panel(
                qr_renderable,
                title="[bold cyan]📱 请使用小黑盒 App 扫描下方二维码登录[/bold cyan]",
                subtitle=f"[dim]有效期约 {qr_info['expire']}s | 按 Ctrl+C 取消[/dim]",
                expand=False,
                border_style="cyan",
                padding=(1, 2),
            )
        )
        console.print(f"[dim]二维码链接: {qr_url}[/dim]")
        if open_browser:
            html_path = save_qr_html(qr_url)
            webbrowser.open(html_path.as_uri())
            console.print(f"[cyan]🌐 已在浏览器打开高清二维码: {html_path}[/cyan]")

        if dry_run:
            state = await client.poll_qr_state(qr_info["qr_token"])
            console.print(f"[cyan]Dry-run 校验完成，当前二维码状态:[/cyan] {state.get('state')} ({state.get('msg')})")
            return 0

        last_state = ""
        try:
            while True:
                res = await client.poll_qr_state(qr_info["qr_token"])
                st = res.get("state")
                if st != last_state:
                    last_state = str(st)
                    if st == "waiting":
                        console.print("⏳ 等待小黑盒 App 扫码...")
                    elif st == "scanned":
                        console.print("[bold yellow]📱 已扫码！请在手机端点击「确认登录」...[/bold yellow]")
                if st == "confirmed":
                    s: SessionData = res["session"]
                    lvl = f" (Lv.{s.level})" if s.level is not None else ""
                    console.print(f"[bold green]✅ 登录成功！欢迎 @{s.nickname}{lvl} (id: {s.heybox_id})[/bold green]")
                    return 0
                if st in ("expired", "requires_2fa", "error"):
                    console.print(f"[bold red]❌ {res.get('msg')}[/bold red]")
                    return 1
                await asyncio.sleep(1.5)
        except KeyboardInterrupt:
            console.print("\n[yellow]已取消扫码登录[/yellow]")
            return 130


async def _cmd_whoami() -> int:
    async with HeyboxClient() as client:
        if not client.session.is_logged_in:
            console.print("[yellow]⚪ 当前未登录。请运行 `xhh login` 进行扫码登录。[/yellow]")
            return 0
        sess = await client.restore_login()
        if sess is None or not sess.is_logged_in:
            console.print("[red]⚠️ 本地登录凭证已过期，请运行 `xhh login` 重新扫码登录。[/red]")
            return 1
        lvl = f" (Lv.{sess.level})" if sess.level is not None else ""
        console.print(f"[bold green]🟢 已登录:[/bold green] @{sess.nickname}{lvl}  |  heybox_id: [cyan]{sess.heybox_id}[/cyan]")
        return 0


async def _cmd_logout() -> int:
    async with HeyboxClient() as client:
        await client.logout()
        console.print("[green]🚪 已退出登录并清除本地会话。[/green]")
        return 0


async def _cmd_feed(limit: int, offset: int, as_json: bool = False) -> int:
    async with HeyboxClient() as client:
        posts = (await client.get_recommended_feeds(offset=offset))[:limit]
        if as_json:
            _print_json({"offset": offset, "count": len(posts), "posts": [p.to_dict() for p in posts]})
            return 0
        _print_posts_table(posts, title=f"🔥 小黑盒推荐流 (offset={offset})")
        return 0


async def _cmd_hot(tab: str, as_json: bool = False) -> int:
    tab_titles = {
        "news": "📈 黑盒热议榜",
        "fusion": "🔍 热搜总榜",
        "gamev2": "🏆 热门游戏榜",
    }
    async with HeyboxClient() as client:
        grouped = await client.get_hot_rankings()
        tabs = list(tab_titles.keys()) if tab == "all" else [tab]
        if as_json:
            _print_json({k: [item.to_dict() for item in (grouped.get(k) or [])] for k in tabs})
            return 0
        for key in tabs:
            items = grouped.get(key) or []
            table = Table(title=tab_titles.get(key, key), header_style="bold cyan")
            table.add_column("排名", style="bold yellow", width=5, justify="right")
            table.add_column("内容 / 游戏", style="bold white")
            table.add_column("标签", style="magenta", width=10)
            table.add_column("类型", style="dim", width=8)
            for item in items:
                table.add_row(str(item.rank), item.title, item.tag or "-", item.item_type or "-")
            console.print(table)
        return 0


async def _cmd_topics(query: str, as_json: bool = False) -> int:
    async with HeyboxClient() as client:
        cats = await client.get_topic_categories()
        hot_topics = cats.get("latest_hot_topics") or []
        sub_topics = cats.get("subscribed_topics") or []
        q_lower = query.strip().lower()

        sub_filtered = [
            t for t in sub_topics if not q_lower or q_lower in t.name.lower() or q_lower == str(t.topic_id)
        ]
        hot_filtered = [
            t for t in hot_topics if not q_lower or q_lower in t.name.lower() or q_lower == str(t.topic_id)
        ]

        if as_json:
            _print_json(
                {
                    "subscribed_topics": [t.to_dict() for t in sub_filtered],
                    "latest_hot_topics": [t.to_dict() for t in hot_filtered],
                }
            )
            return 0

        if sub_filtered:
            t_sub = Table(title="⭐ 已关注板块", header_style="bold yellow")
            t_sub.add_column("topic_id", style="cyan", width=10)
            t_sub.add_column("板块名称", style="bold white")
            for t in sub_filtered:
                t_sub.add_row(str(t.topic_id), t.name)
            console.print(t_sub)

        table = Table(
            title=f"🎮 热门游戏 / 社区板块{f' (匹配: {query})' if query else ''}",
            header_style="bold cyan",
        )
        table.add_column("topic_id", style="cyan", width=10)
        table.add_column("板块名称", style="bold white")
        table.add_column("关联 app_id", style="dim", width=12)
        for t in hot_filtered:
            app_str = str(t.app_id) if str(t.app_id) not in ("-1", "") else "-"
            table.add_row(str(t.topic_id), t.name, app_str)
        console.print(table)
        return 0


async def _cmd_topic(
    name_or_id: str, is_hot: bool, sort: str, limit: int, as_json: bool = False
) -> int:
    async with HeyboxClient() as client:
        topic = await client.resolve_topic(name_or_id)
        if topic is None:
            if as_json:
                _print_json({"error": f"未找到匹配的板块: {name_or_id}"})
            else:
                console.print(f"[red]未找到匹配的板块「{name_or_id}」，请先用 `xhh topics -q <关键词>` 查询 topic_id[/red]")
            return 1
        res = await client.get_topic_feeds(
            topic_id=topic.topic_id,
            is_hot=is_hot,
            sort_filter=sort,
            offset=0,
            limit=limit,
        )
        posts = res["posts"][:limit]
        if as_json:
            _print_json(
                {
                    "topic": topic.to_dict(),
                    "is_hot": is_hot,
                    "sort": sort,
                    "posts": [p.to_dict() for p in posts],
                }
            )
            return 0
        mode_lbl = "热点" if is_hot else "综合"
        _print_posts_table(
            posts,
            title=f"🎮 {topic.name} (topic_id={topic.topic_id}) · {mode_lbl} · 排序={sort}",
        )
        return 0


async def _cmd_post(
    link_id: str,
    owner_only: bool = False,
    page: int = 1,
    floors: str | None = None,
    sort: str | None = None,
    max_pages: int = 10,
    as_json: bool = False,
) -> int:
    parsed_range: tuple[int, int | None] | None = None
    if floors is not None:
        parsed_range = parse_floor_range(floors)

    effective_sort_key = sort or ("asc" if parsed_range is not None else "hot")
    api_sort = COMMENT_SORT_API_MAP.get(effective_sort_key, "hot")

    async with HeyboxClient() as client:
        if parsed_range is not None:
            detail = await client.get_post_detail_by_floors(
                link_id=link_id,
                floor_range=parsed_range,
                owner_only=owner_only,
                sort_filter=api_sort,
                max_pages=max_pages,
            )
        else:
            detail = await client.get_post_detail(
                link_id=link_id,
                page=page,
                owner_only=owner_only,
                sort_filter=api_sort,
            )

    if as_json:
        filter_info: dict[str, Any] = {
            "sort": effective_sort_key,
            "owner_only": owner_only,
            "page": page if parsed_range is None else None,
            "floors": (
                {"raw": floors, "start": parsed_range[0], "end": parsed_range[1]}
                if parsed_range is not None
                else None
            ),
        }
        _print_json(detail.to_dict(comment_filter=filter_info))
        return 0

    meta_parts = [f"@{detail.author}"]
    if detail.author_level is not None:
        meta_parts.append(f"Lv.{detail.author_level}")
    if detail.ip_location:
        meta_parts.append(detail.ip_location)
    tm = format_time(detail.create_at)
    if tm:
        meta_parts.append(tm)
    if detail.topic_name:
        meta_parts.append(f"板块: {detail.topic_name}")
    meta_line = " · ".join(meta_parts)

    body_render = Text()
    body_render.append(meta_line + "\n", style="yellow")
    body_render.append(
        f"👍 {detail.up}   ⚡ {detail.award_num}   💬 {detail.comment_num}   ⭐ {detail.favour_count}\n",
        style="green",
    )
    body_render.append("─" * 60 + "\n", style="dim")
    body_render.append(detail.body or "(暂无文本正文)")
    if detail.images:
        body_render.append("\n\n─── 🖼 图片链接 ───\n", style="bold cyan")
        for idx, img in enumerate(detail.images, start=1):
            body_render.append(f"  [图{idx}] {img}\n", style="dim blue")
    body_render.append(f"\n原帖地址: {detail.share_url}", style="dim")

    console.print(
        Panel(
            body_render,
            title=f"[bold white]{detail.title}[/bold white]",
            border_style="cyan",
        )
    )

    mode_str = "只看楼主" if owner_only else "全部评论"
    if parsed_range is not None:
        end_lbl = str(parsed_range[1]) if parsed_range[1] is not None else "末尾"
        scope_str = f"楼层 {parsed_range[0]}-{end_lbl} (匹配 {len(detail.comments)} 条)"
    else:
        scope_str = f"第 {page}/{max(detail.total_page, 1)} 页"
    console.print(f"\n[bold cyan]💬 评论区 ({scope_str} · {mode_str} · 排序={effective_sort_key})[/bold cyan]")

    if not detail.comments:
        console.print("[dim]暂无匹配评论[/dim]")
        return 0

    for thread in detail.comments:
        r = thread.root
        line = Text()
        if r.floor_num:
            line.append(f"#{r.floor_num}楼 ", style="bold cyan")
        if r.is_cy:
            line.append("★[神评] ", style="bold red")
        if r.is_top:
            line.append("📌[置顶] ", style="bold magenta")
        if r.is_link_owner:
            line.append("👑[楼主] ", style="bold yellow")
        line.append(f"@{r.username}", style="bold yellow")
        if r.user_level is not None:
            line.append(f" (Lv.{r.user_level})", style="dim")
        if r.ip_location:
            line.append(f" · {r.ip_location}", style="dim")
        line.append(f" · 👍{r.up}\n", style="green")
        line.append(f"  {r.text or '[图片评论]'}")
        if r.imgs:
            line.append(f"  [🖼 x{len(r.imgs)}]", style="magenta")

        for sub in thread.children:
            line.append("\n    ├─ ", style="dim")
            line.append(f"@{sub.username}", style="cyan")
            if sub.reply_to_user:
                line.append(f" 回复 @{sub.reply_to_user}", style="dim")
            line.append(f": {sub.text or '[图片]'}", style="white")
            if sub.up:
                line.append(f" (👍{sub.up})", style="dim green")

        if thread.has_more and r.child_num > len(thread.children):
            line.append(f"\n    └─ 还有更多回复 (共 {r.child_num} 条)", style="dim")
        console.print(Panel(line, border_style="bright_black", padding=(0, 1)))

    return 0


async def _cmd_search(query: str, limit: int, as_json: bool = False) -> int:
    async with HeyboxClient() as client:
        res = await client.search(query, limit=limit)
        posts = (res.get("posts") or [])[:limit]
        topics = res.get("topics") or []
        if as_json:
            _print_json(
                {
                    "query": query,
                    "topics": [t.to_dict() for t in topics],
                    "posts": [p.to_dict() for p in posts],
                    "suggestions": res.get("suggestions") or [],
                    "requires_login": bool(res.get("requires_login")),
                }
            )
            return 0
        if topics:
            table = Table(title=f"🎮 匹配的游戏/社区板块 ({query})", header_style="bold cyan")
            table.add_column("topic_id", style="cyan", width=10)
            table.add_column("板块名称", style="bold white")
            for t in topics:
                table.add_row(str(t.topic_id), t.name)
            console.print(table)
        if posts:
            _print_posts_table(posts, title=f"📝 帖子搜索结果 ({query})")
        elif res.get("requires_login"):
            console.print("[yellow]提示: 全站帖子搜索需要登录态，可先运行 `xhh login` 扫码登录。[/yellow]")
        if res.get("suggestions"):
            console.print(f"[dim]相关热词: {' / '.join(res['suggestions'][:8])}[/dim]")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xhh",
        description="小黑盒社区终端客户端 (支持全屏 TUI、扫码登录、板块浏览、帖子与评论阅读)",
    )
    parser.add_argument("-V", "--version", action="version", version=f"xhh {__version__}")
    parser.add_argument("--post", dest="direct_post_id", help="不进入界面直接查看指定帖子 link_id")
    parser.add_argument("-f", "--floors", dest="direct_floors", help="配合 --post 使用，指定评论楼层范围 (如 1-10)")
    parser.add_argument("-j", "--json", dest="direct_json", action="store_true", help="配合 --post 使用，以 JSON 结构输出")
    parser.add_argument("--owner", dest="direct_owner", action="store_true", help="配合 --post 使用，只看楼主评论")
    parser.add_argument(
        "--sort",
        dest="direct_sort",
        choices=["hot", "asc", "desc"],
        default=None,
        help="配合 --post 使用，评论排序方式",
    )

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("tui", help="启动全屏交互式 TUI (默认)")

    p_login = sub.add_parser("login", help="在终端显示二维码并扫码登录小黑盒")
    p_login.add_argument("--dry-run", action="store_true", help="仅获取并渲染二维码及检查一次状态后退出")
    p_login.add_argument("--large", action="store_true", help="使用双倍尺寸纯 ASCII 空格方块渲染二维码")
    p_login.add_argument("--compact", action="store_true", help="使用紧凑模式渲染二维码（默认已开启）")
    p_login.add_argument("--browser", action="store_true", help="同时在默认浏览器中打开高清 SVG 二维码")

    sub.add_parser("whoami", help="查看当前登录账号信息")
    sub.add_parser("logout", help="退出登录并清除本地会话")

    p_feed = sub.add_parser("feed", help="查看首页推荐帖子")
    p_feed.add_argument("-n", "--limit", type=int, default=10, help="展示数量 (默认 10)")
    p_feed.add_argument("--offset", type=int, default=0, help="分页偏移量")
    p_feed.add_argument("-j", "--json", dest="as_json", action="store_true", help="以 JSON 结构输出")

    p_hot = sub.add_parser("hot", help="查看黑盒热议 / 热搜总榜 / 热门游戏榜")
    p_hot.add_argument(
        "--tab",
        choices=["all", "news", "fusion", "gamev2"],
        default="all",
        help="榜单分类 (默认 all)",
    )
    p_hot.add_argument("-j", "--json", dest="as_json", action="store_true", help="以 JSON 结构输出")

    p_topics = sub.add_parser("topics", help="列出或搜索热门游戏/社区板块")
    p_topics.add_argument("-q", "--query", default="", help="按板块名称筛选")
    p_topics.add_argument("-j", "--json", dest="as_json", action="store_true", help="以 JSON 结构输出")

    p_topic = sub.add_parser("topic", help="查看指定游戏/社区板块帖子")
    p_topic.add_argument("target", help="游戏/板块名称或 topic_id (如 CS2、黑神话、43)")
    p_topic.add_argument("--hot", action="store_true", help="切换到板块「热点」子分类")
    p_topic.add_argument(
        "--sort",
        choices=["hot-rank", "reply", "create"],
        default="hot-rank",
        help="排序方式: hot-rank(智能) / reply(回复时间) / create(发布时间)",
    )
    p_topic.add_argument("-n", "--limit", type=int, default=10, help="展示数量 (默认 10)")
    p_topic.add_argument("-j", "--json", dest="as_json", action="store_true", help="以 JSON 结构输出")

    p_post = sub.add_parser("post", aliases=["view"], help="查看指定帖子正文与评论区 (支持楼层过滤与 JSON 输出)")
    p_post.add_argument("link_id", help="帖子 link_id")
    p_post.add_argument("-f", "--floors", "--floor", dest="floors", default=None, help="指定评论楼层范围，如 1-10、8、15-")
    p_post.add_argument(
        "--sort",
        choices=["hot", "asc", "desc"],
        default=None,
        help="评论排序: hot(热门) / asc(正序楼层) / desc(倒序); 指定 --floors 时默认 asc",
    )
    p_post.add_argument("--owner", action="store_true", help="只看楼主评论")
    p_post.add_argument("--page", type=int, default=1, help="评论页码 (未指定 --floors 时生效，默认 1)")
    p_post.add_argument("--max-pages", type=int, default=10, help="按楼层过滤时最多扫描的评论页数 (默认 10)")
    p_post.add_argument("-j", "--json", dest="as_json", action="store_true", help="直接以 JSON 结构返回，不渲染界面")

    p_search = sub.add_parser("search", help="搜索游戏板块与帖子")
    p_search.add_argument("query", help="搜索关键词")
    p_search.add_argument("-n", "--limit", type=int, default=10, help="展示数量 (默认 10)")
    p_search.add_argument("-j", "--json", dest="as_json", action="store_true", help="以 JSON 结构输出")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.direct_post_id:
        try:
            return asyncio.run(
                _cmd_post(
                    link_id=args.direct_post_id,
                    owner_only=args.direct_owner,
                    floors=args.direct_floors,
                    sort=args.direct_sort,
                    as_json=args.direct_json,
                )
            )
        except (HeyboxAPIError, ValueError) as exc:
            if args.direct_json:
                _print_json({"error": str(exc)})
            else:
                console.print(f"[bold red]错误: {exc}[/bold red]")
            return 1

    if args.command is None or args.command == "tui":
        from xhh.tui.app import HeyboxApp

        HeyboxApp().run()
        return 0

    want_json = bool(getattr(args, "as_json", False))
    try:
        if args.command == "login":
            use_large = bool(args.large) and not bool(args.compact)
            return asyncio.run(
                _cmd_login(dry_run=args.dry_run, large=use_large, open_browser=args.browser)
            )
        if args.command == "whoami":
            return asyncio.run(_cmd_whoami())
        if args.command == "logout":
            return asyncio.run(_cmd_logout())
        if args.command == "feed":
            return asyncio.run(_cmd_feed(limit=args.limit, offset=args.offset, as_json=want_json))
        if args.command == "hot":
            return asyncio.run(_cmd_hot(tab=args.tab, as_json=want_json))
        if args.command == "topics":
            return asyncio.run(_cmd_topics(query=args.query, as_json=want_json))
        if args.command == "topic":
            return asyncio.run(
                _cmd_topic(
                    name_or_id=args.target,
                    is_hot=args.hot,
                    sort=args.sort,
                    limit=args.limit,
                    as_json=want_json,
                )
            )
        if args.command in ("post", "view"):
            return asyncio.run(
                _cmd_post(
                    link_id=args.link_id,
                    owner_only=args.owner,
                    page=args.page,
                    floors=args.floors,
                    sort=args.sort,
                    max_pages=args.max_pages,
                    as_json=want_json,
                )
            )
        if args.command == "search":
            return asyncio.run(_cmd_search(query=args.query, limit=args.limit, as_json=want_json))
    except (HeyboxAPIError, ValueError) as exc:
        if want_json:
            _print_json({"error": str(exc)})
        else:
            console.print(f"[bold red]错误: {exc}[/bold red]")
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
