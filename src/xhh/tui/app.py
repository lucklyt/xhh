"""小黑盒社区 Textual 全屏 TUI 主程序."""

from __future__ import annotations

import asyncio
import webbrowser
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import ContentSwitcher, OptionList, Static
from textual.widgets.option_list import Option

from xhh.client import HeyboxClient
from xhh.config import AppConfig, SessionData
from xhh.models import (
    CommentThread,
    HotRankItem,
    PostDetail,
    PostSummary,
    TopicItem,
    format_count,
    format_time,
)
from xhh.tui.modals import BossScreen, QRLoginModal, SearchModal, SubCommentModal

FEED_PAGE_SIZE = 15

SORT_MODES: list[tuple[str, str]] = [
    ("hot-rank", "智能排序"),
    ("reply", "回复时间"),
    ("create", "最新发布"),
]

COMMENT_SORT_MODES: list[tuple[str, str]] = [
    ("hot", "热门"),
    ("time_aes", "正序"),
    ("time_desc", "倒序"),
]

HOT_TAB_TITLES: dict[str, str] = {
    "news": "黑盒热议榜",
    "fusion": "热搜总榜",
    "gamev2": "热门游戏榜",
}


class HeyboxApp(App[None]):
    """小黑盒 CLI 终端交互应用."""

    CSS_PATH = Path(__file__).with_name("theme.tcss")
    TITLE = "小黑盒 CLI"

    BINDINGS = [
        Binding("q", "back_or_quit", "返回/退出"),
        Binding("escape", "back_to_list", "返回列表", show=False),
        Binding("backspace", "back_to_list", "返回列表", show=False),
        Binding("j", "move_down", "下移", show=False),
        Binding("k", "move_up", "上移", show=False),
        Binding("h", "focus_sidebar", "左侧栏", show=False),
        Binding("left", "focus_sidebar", "左侧栏", show=False),
        Binding("right", "focus_content", "内容区", show=False),
        Binding("l", "open_login", "扫码登录"),
        Binding("slash", "open_search", "搜索"),
        Binding("1", "filter_all", "综合"),
        Binding("2", "filter_hot", "热点"),
        Binding("s", "cycle_sort", "切排序"),
        Binding("f", "toggle_favorite", "收藏板块"),
        Binding("n", "next_page", "下一页"),
        Binding("p", "prev_page", "上一页"),
        Binding("r", "refresh_view", "刷新"),
        Binding("o", "toggle_owner_only", "只看楼主"),
        Binding("c", "toggle_detail_view", "切换正文/评论"),
        Binding("e", "expand_subcomments", "楼中楼"),
        Binding("b", "open_browser", "浏览器打开"),
        Binding("grave_accent", "boss_key", "老板键"),
    ]

    def __init__(self, client: HeyboxClient | None = None) -> None:
        super().__init__()
        self._external_client = client is not None
        self.client = client if client is not None else HeyboxClient()
        self.app_config = AppConfig.load()

        # 导航与列表状态
        self._sidebar_map: dict[str, dict[str, Any]] = {}
        self._hot_topics: list[TopicItem] = []
        self._subscribed_topics: list[TopicItem] = []
        self._view_mode: str = "feed"  # "feed" | "hot" | "topic" | "search_posts"
        self._current_hot_tab: str = "news"
        self._current_topic: TopicItem | None = None
        self._topic_is_hot: bool = False
        self._topic_sort_idx: int = 0

        # 固定分页状态
        self._feed_page: int = 1
        self._feed_pages_cache: dict[int, list[PostSummary]] = {}
        self._recommended_buffer: list[PostSummary] = []
        self._recommended_api_offset: int = 0
        self._recommended_api_has_more: bool = True
        self._topic_lastval_by_page: dict[int, str] = {1: ""}
        self._has_more_feeds: bool = True

        self._posts: list[PostSummary] = []
        self._hot_items: list[HotRankItem] = []
        self._saved_feed_highlight: int = 0

        # 帖子详情状态
        self._current_post_summary: PostSummary | None = None
        self._current_detail: PostDetail | None = None
        self._detail_tab: str = "body"  # "body" | "comments"
        self._comment_page: int = 1
        self._owner_only: bool = False
        self._comment_sort_idx: int = 0
        self._detail_threads: list[CommentThread] = []

        self._load_task: asyncio.Task[None] | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="top-bar"):
            yield Static("📦 小黑盒 CLI", id="top-title")
            yield Static("⚪ 未登录 [l:扫码登录]", id="top-account")

        with Horizontal(id="main-body"):
            with Vertical(id="sidebar"):
                yield OptionList(id="sidebar-list")

            with ContentSwitcher(id="content-switcher", initial="list-pane"):
                with Vertical(id="list-pane"):
                    yield Static("加载中...", id="list-subbar")
                    yield OptionList(id="feed-list")

                with Vertical(id="detail-pane"):
                    yield Static("", id="detail-header")
                    yield Static("", id="detail-tabbar")
                    with ContentSwitcher(id="detail-sub-switcher", initial="detail-body-view"):
                        with Vertical(id="detail-body-view"):
                            with VerticalScroll(id="post-body-scroll"):
                                yield Static("", id="detail-body")
                        with Vertical(id="detail-comments-view"):
                            yield Static("", id="comment-bar")
                            yield OptionList(id="comment-list")

        yield Static(
            "j/k:移动  Enter:进入  n/p:翻页  c:正文/评论  /:搜板块  l:扫码登录  1/2:综合/热点  `:老板键  q:返回/退出",
            id="status-footer",
        )

    async def on_mount(self) -> None:
        self._update_account_banner()
        self._rebuild_sidebar()
        self._schedule_load(self._bootstrap())

    async def on_unmount(self) -> None:
        if self._load_task and not self._load_task.done():
            self._load_task.cancel()
        if not self._external_client:
            await self.client.close()

    def _schedule_load(self, coro: Any) -> None:
        if self._load_task and not self._load_task.done():
            self._load_task.cancel()
        self._load_task = asyncio.create_task(coro)

    async def _bootstrap(self) -> None:
        tasks = [
            self._safe_restore_login(),
            self._load_categories(),
            self._load_recommended_feeds(page=1, reset=True),
        ]
        await asyncio.gather(*tasks)
        self.query_one("#feed-list", OptionList).focus()

    async def _safe_restore_login(self) -> None:
        try:
            await self.client.restore_login()
        except Exception:
            pass
        self._update_account_banner()

    async def _load_categories(self) -> None:
        try:
            cats = await self.client.get_topic_categories()
            self._hot_topics = cats.get("latest_hot_topics") or []
            self._subscribed_topics = cats.get("subscribed_topics") or []
            self._rebuild_sidebar()
        except Exception:
            pass

    def _update_account_banner(self) -> None:
        acct = self.query_one("#top-account", Static)
        if self.client.session.is_logged_in:
            lvl = f" (Lv.{self.client.session.level})" if self.client.session.level is not None else ""
            name = self.client.session.nickname or self.client.session.heybox_id
            acct.update(f"🟢 @{name}{lvl} [l:账号]")
        else:
            acct.update("⚪ 未登录 [l:扫码登录]")

    def _update_top_title(self) -> None:
        title = self.query_one("#top-title", Static)
        if self._view_mode == "feed":
            title.update(f"📦 小黑盒 CLI  |  🔥 推荐 (首页) · 第 {self._feed_page} 页")
        elif self._view_mode == "hot":
            name = HOT_TAB_TITLES.get(self._current_hot_tab, "热点榜单")
            title.update(f"📦 小黑盒 CLI  |  {name}")
        elif self._view_mode == "topic" and self._current_topic:
            tab_str = "1.综合 / [2.热点]" if self._topic_is_hot else "[1.综合] / 2.热点"
            sort_label = SORT_MODES[self._topic_sort_idx][1]
            fav_mark = "⭐" if self.app_config.is_favorite(self._current_topic.topic_id) else ""
            title.update(
                f"📦 小黑盒 CLI  |  🎮 {fav_mark}{self._current_topic.name} ({self._current_topic.topic_id})  |  {tab_str}  |  排序: {sort_label} · 第 {self._feed_page} 页"
            )
        elif self._view_mode == "search_posts":
            title.update("📦 小黑盒 CLI  |  🔍 搜索结果")

    def _rebuild_sidebar(self) -> None:
        sidebar = self.query_one("#sidebar-list", OptionList)
        prev_hl = sidebar.highlighted
        self._sidebar_map.clear()
        options: list[Option] = []

        nav_items = [
            ("nav:feed", "🔥 推荐 (首页)", {"mode": "feed"}),
            ("nav:hot:news", "📈 黑盒热议榜", {"mode": "hot", "tab": "news"}),
            ("nav:hot:fusion", "🔍 热搜总榜", {"mode": "hot", "tab": "fusion"}),
            ("nav:hot:gamev2", "🏆 热门游戏榜", {"mode": "hot", "tab": "gamev2"}),
        ]
        for oid, label, meta in nav_items:
            self._sidebar_map[oid] = meta
            options.append(Option(label, id=oid))

        options.append(Option("── ⭐ 收藏/订阅 ──", id="hdr:fav", disabled=True))
        seen_ids: set[str] = set()
        fav_topics: list[TopicItem] = [
            TopicItem(topic_id=t["topic_id"], name=t["name"])
            for t in self.app_config.favorite_topics
            if t.get("topic_id") and t.get("name")
        ]
        for t in [*fav_topics, *self._subscribed_topics]:
            tid = str(t.topic_id)
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            oid = f"nav:topic:fav:{tid}"
            self._sidebar_map[oid] = {"mode": "topic", "topic": t}
            options.append(Option(f"⭐ {t.name}", id=oid))

        options.append(Option("── 🎮 热门板块 ──", id="hdr:hot", disabled=True))
        for t in self._hot_topics:
            tid = str(t.topic_id)
            oid = f"nav:topic:hot:{tid}"
            self._sidebar_map[oid] = {"mode": "topic", "topic": t}
            prefix = "★ " if self.app_config.is_favorite(tid) else "• "
            options.append(Option(f"{prefix}{t.name}", id=oid))

        sidebar.set_options(options)
        if prev_hl is not None and 0 <= prev_hl < len(options):
            sidebar.highlighted = prev_hl
        else:
            sidebar.highlighted = 0

    def _render_posts_list(self, status_msg: str = "") -> None:
        subbar = self.query_one("#list-subbar", Static)
        feed_list = self.query_one("#feed-list", OptionList)
        self._update_top_title()

        options: list[Option] = []
        for idx, post in enumerate(self._posts, start=1):
            t = Text()
            hot_badge = "[热] " if post.is_hot_post else ""
            t.append(f"{idx:>2}. ", style="bold cyan")
            if hot_badge:
                t.append(hot_badge, style="bold red")
            t.append(post.title, style="bold white")
            t.append("\n    ")
            if post.author:
                t.append(f"@{post.author}", style="yellow")
            if post.topic_name:
                sep = " · " if post.author else ""
                t.append(f"{sep}[{post.topic_name}]", style="cyan")
            stats_prefix = " · " if (post.author or post.topic_name) else ""
            t.append(
                f"{stats_prefix}👍{format_count(post.up)} · ⚡{format_count(post.award_num)} · 💬{format_count(post.comment_num)}",
                style="green",
            )
            tm = post.create_str or format_time(post.create_at)
            if tm:
                t.append(f" · {tm}", style="dim")
            if post.has_video:
                t.append(" · 🎬视频", style="magenta")
            elif post.image_count > 0:
                t.append(f" · 🖼{post.image_count}图", style="blue")
            if post.description:
                snippet = post.description[:68] + ("..." if len(post.description) > 68 else "")
                t.append(f"\n    {snippet}", style="dim")
            options.append(Option(t, id=f"post:{post.linkid}:{idx-1}"))

        if not options:
            options.append(Option(Text("暂无帖子数据，按 r 重试或切换其他板块", style="dim"), disabled=True))

        feed_list.set_options(options)
        if options and not options[0].disabled:
            feed_list.highlighted = 0
            feed_list.scroll_home(animate=False)

        page_info = f"第 {self._feed_page} 页 (本页 {len(self._posts)} 条)"
        nav_hint = "n:下一页" + (" p:上一页" if self._feed_page > 1 else "")
        if status_msg:
            subbar.update(status_msg)
        elif self._view_mode == "topic" and self._current_topic:
            subbar.update(
                f"板块「{self._current_topic.name}」· {page_info} | 1:综合 2:热点 | s:排序 | f:收藏 | {nav_hint}"
            )
        else:
            subbar.update(f"{page_info} | Enter:阅读正文 | {nav_hint} | r:刷新")

    def _render_hot_list(self) -> None:
        subbar = self.query_one("#list-subbar", Static)
        feed_list = self.query_one("#feed-list", OptionList)
        self._update_top_title()

        options: list[Option] = []
        for item in self._hot_items:
            t = Text()
            rank_style = "bold red" if item.rank <= 3 else "bold cyan"
            t.append(f"#{item.rank:<2} ", style=rank_style)
            t.append(item.title, style="bold white")
            if item.tag:
                t.append(f"  [{item.tag}]", style="bold magenta")
            if item.item_type == "game":
                t.append("  🎮 进入游戏板块", style="dim cyan")
            options.append(Option(t, id=f"hot:{item.rank - 1}"))

        if not options:
            options.append(Option(Text("暂无榜单数据", style="dim"), disabled=True))
        feed_list.set_options(options)
        if options and not options[0].disabled:
            feed_list.highlighted = 0
            feed_list.scroll_home(animate=False)
        tab_name = HOT_TAB_TITLES.get(self._current_hot_tab, "热榜")
        subbar.update(f"{tab_name} (共 {len(self._hot_items)} 条) | Enter: 直达板块或搜索相关帖子 | r: 刷新")

    async def _load_recommended_feeds(self, page: int = 1, reset: bool = False) -> None:
        self._view_mode = "feed"
        self.query_one("#content-switcher", ContentSwitcher).current = "list-pane"
        if reset:
            self._feed_page = 1
            self._feed_pages_cache.clear()
            self._recommended_buffer.clear()
            self._recommended_api_offset = 0
            self._recommended_api_has_more = True
            self._posts = []
            self.query_one("#list-subbar", Static).update("⏳ 正在加载首页推荐流...")
        else:
            self.query_one("#list-subbar", Static).update(f"⏳ 正在切换到第 {page} 页...")

        if page in self._feed_pages_cache:
            self._feed_page = page
            self._posts = self._feed_pages_cache[page]
            self._has_more_feeds = (page + 1) in self._feed_pages_cache or len(self._recommended_buffer) > page * FEED_PAGE_SIZE or self._recommended_api_has_more
            self._render_posts_list()
            return

        need_end = page * FEED_PAGE_SIZE
        seen_ids = {p.linkid for p in self._recommended_buffer}

        try:
            while len(self._recommended_buffer) < need_end and self._recommended_api_has_more:
                batch = await self.client.get_recommended_feeds(offset=self._recommended_api_offset)
                if not batch:
                    self._recommended_api_has_more = False
                    break
                added = 0
                for p in batch:
                    if p.linkid not in seen_ids:
                        seen_ids.add(p.linkid)
                        self._recommended_buffer.append(p)
                        added += 1
                self._recommended_api_offset += len(batch)
                if added == 0:
                    self._recommended_api_has_more = False
                    break
        except Exception as exc:
            self.query_one("#list-subbar", Static).update(f"❌ 加载失败: {exc} (按 r 重试)")
            return

        start = (page - 1) * FEED_PAGE_SIZE
        page_posts = self._recommended_buffer[start : start + FEED_PAGE_SIZE]
        if not page_posts and page > 1:
            self._has_more_feeds = False
            self.query_one("#list-subbar", Static).update(f"已到最后一页 (第 {self._feed_page} 页)")
            return

        self._feed_page = page
        self._feed_pages_cache[page] = page_posts
        self._posts = page_posts
        self._has_more_feeds = len(self._recommended_buffer) > start + FEED_PAGE_SIZE or self._recommended_api_has_more
        self._render_posts_list()

    async def _load_hot_ranking(self, tab_type: str) -> None:
        self._view_mode = "hot"
        self._current_hot_tab = tab_type
        self.query_one("#content-switcher", ContentSwitcher).current = "list-pane"
        self.query_one("#list-subbar", Static).update("⏳ 正在加载热榜...")
        try:
            grouped = await self.client.get_hot_rankings()
        except Exception as exc:
            self.query_one("#list-subbar", Static).update(f"❌ 加载热榜失败: {exc}")
            return
        self._hot_items = grouped.get(tab_type) or []
        self._render_hot_list()

    async def _load_topic_feeds(self, topic: TopicItem, page: int = 1, reset: bool = False) -> None:
        self._view_mode = "topic"
        self._current_topic = topic
        self.query_one("#content-switcher", ContentSwitcher).current = "list-pane"
        if reset:
            self._feed_page = 1
            self._feed_pages_cache.clear()
            self._topic_lastval_by_page = {1: ""}
            self._posts = []
            self.query_one("#list-subbar", Static).update(f"⏳ 正在加载「{topic.name}」...")
        else:
            self.query_one("#list-subbar", Static).update(f"⏳ 正在加载「{topic.name}」第 {page} 页...")

        if page in self._feed_pages_cache:
            self._feed_page = page
            self._posts = self._feed_pages_cache[page]
            self._has_more_feeds = len(self._posts) > 0
            self._render_posts_list()
            return

        sort_key = SORT_MODES[self._topic_sort_idx][0]
        offset = (page - 1) * FEED_PAGE_SIZE
        lastval = self._topic_lastval_by_page.get(page, "")
        try:
            res = await self.client.get_topic_feeds(
                topic_id=topic.topic_id,
                is_hot=self._topic_is_hot,
                sort_filter=sort_key,
                offset=offset,
                limit=FEED_PAGE_SIZE,
                lastval=lastval,
            )
        except Exception as exc:
            self.query_one("#list-subbar", Static).update(f"❌ 加载板块失败: {exc}")
            return

        page_posts: list[PostSummary] = (res["posts"] or [])[:FEED_PAGE_SIZE]
        if not page_posts and page > 1:
            self._has_more_feeds = False
            self.query_one("#list-subbar", Static).update(f"板块「{topic.name}」已到最后一页 (第 {self._feed_page} 页)")
            return

        self._feed_page = page
        self._feed_pages_cache[page] = page_posts
        self._topic_lastval_by_page[page + 1] = res.get("lastval") or ""
        self._posts = page_posts
        self._has_more_feeds = bool(res.get("has_more")) and len(page_posts) > 0
        self._render_posts_list()

    def _update_detail_tabbar(self) -> None:
        tabbar = self.query_one("#detail-tabbar", Static)
        detail = self._current_detail
        cmt_cnt = detail.comment_num if detail else 0
        total_pg = max(detail.total_page, 1) if detail else 1
        sort_lbl = COMMENT_SORT_MODES[self._comment_sort_idx][1]

        t = Text()
        if self._detail_tab == "body":
            t.append(" [ 📄 正文 ] ", style="bold black on #58a6ff")
            t.append(f"   💬 评论区 ({cmt_cnt}) ", style="dim")
            t.append("   |   c:切到评论  j/k:滚动  b:浏览器打开  Esc:返回列表", style="dim")
        else:
            t.append("  📄 正文  ", style="dim")
            t.append(f" [ 💬 评论区 ({cmt_cnt}) · 第 {self._comment_page}/{total_pg} 页 ] ", style="bold black on #f0883e")
            t.append(f"   |   c:返回正文  s:排序[{sort_lbl}]  o:只看楼主  e:楼中楼  n/p:翻页", style="dim")
        tabbar.update(t)

    def _switch_detail_tab(self, tab: str) -> None:
        self._detail_tab = tab
        sub = self.query_one("#detail-sub-switcher", ContentSwitcher)
        if tab == "comments":
            sub.current = "detail-comments-view"
            self.query_one("#comment-list", OptionList).focus()
        else:
            sub.current = "detail-body-view"
            self.query_one("#post-body-scroll", VerticalScroll).focus()
        self._update_detail_tabbar()

    async def _open_post_detail(
        self,
        link_id: str,
        summary: PostSummary | None = None,
        page: int = 1,
        reset_tab: bool = True,
    ) -> None:
        if reset_tab:
            feed_list = self.query_one("#feed-list", OptionList)
            if feed_list.highlighted is not None:
                self._saved_feed_highlight = feed_list.highlighted
            self._current_post_summary = summary
            self._detail_tab = "body"
            self._comment_page = 1
            self._owner_only = False
            self._comment_sort_idx = 0

        self.query_one("#content-switcher", ContentSwitcher).current = "detail-pane"
        self.query_one("#detail-header", Static).update(f"⏳ 正在加载帖子 #{link_id} ...")
        if reset_tab:
            self.query_one("#detail-body", Static).update("")
            self._switch_detail_tab("body")
        self.query_one("#comment-bar", Static).update("⏳ 正在加载评论...")

        fallback_imgs = summary.imgs if summary else None
        cmt_sort = COMMENT_SORT_MODES[self._comment_sort_idx][0]
        try:
            detail = await self.client.get_post_detail(
                link_id=link_id,
                page=page,
                limit=20,
                owner_only=self._owner_only,
                sort_filter=cmt_sort,
                fallback_imgs=fallback_imgs,
            )
        except Exception as exc:
            self.query_one("#detail-header", Static).update(f"❌ 加载帖子失败: {exc} (按 Esc 返回)")
            return

        self._current_detail = detail
        self._comment_page = page
        self._detail_threads = detail.comments

        # Header
        hdr = Text()
        if detail.topic_name:
            hdr.append(f"[{detail.topic_name}] ", style="bold cyan")
        hdr.append(detail.title, style="bold white")
        hdr.append("\n")
        hdr.append(f"作者: @{detail.author}", style="bold yellow")
        if detail.author_level is not None:
            hdr.append(f" (Lv.{detail.author_level})", style="dim")
        if detail.ip_location:
            hdr.append(f" · {detail.ip_location}", style="dim")
        tm = format_time(detail.create_at)
        if tm:
            hdr.append(f" · {tm}", style="dim")
        hdr.append(
            f"   |   👍 {detail.up}   ⚡ {detail.award_num}   💬 {detail.comment_num}   ⭐ {detail.favour_count}",
            style="green",
        )
        self.query_one("#detail-header", Static).update(hdr)

        # Body + Images
        body_text = Text(detail.body or "(暂无文本正文)")
        if detail.images:
            body_text.append("\n\n─── 🖼 帖子图片列表 (按 b 可在浏览器查看原图) ───\n", style="bold cyan")
            for idx, img_url in enumerate(detail.images, start=1):
                body_text.append(f"  [图{idx}] ", style="bold blue")
                body_text.append(f"{img_url}\n", style="dim underline")
        self.query_one("#detail-body", Static).update(body_text)
        if reset_tab:
            self.query_one("#post-body-scroll", VerticalScroll).scroll_home(animate=False)

        # Comments
        mode_tag = "[只看楼主]" if self._owner_only else "[全部评论]"
        sort_lbl = COMMENT_SORT_MODES[self._comment_sort_idx][1]
        self.query_one("#comment-bar", Static).update(
            f"💬 第 {page}/{max(detail.total_page, 1)} 页 · {mode_tag} · 排序:{sort_lbl} | o:只看楼主  s:切排序  e:楼中楼  n/p:翻页  c:回正文"
        )

        cmt_list = self.query_one("#comment-list", OptionList)
        cmt_options: list[Option] = []
        for idx, th in enumerate(detail.comments):
            r = th.root
            ct = Text()
            if r.floor_num:
                ct.append(f"#{r.floor_num}楼 ", style="bold cyan")
            if r.is_cy:
                ct.append("★[神评] ", style="bold red")
            if r.is_top:
                ct.append("📌[置顶] ", style="bold magenta")
            if r.is_link_owner:
                ct.append("👑[楼主] ", style="bold yellow")
            ct.append(f"@{r.username}", style="bold yellow")
            if r.user_level is not None:
                ct.append(f" (Lv.{r.user_level})", style="dim")
            meta: list[str] = []
            if r.ip_location:
                meta.append(r.ip_location)
            rtm = format_time(r.create_at)
            if rtm:
                meta.append(rtm)
            if meta:
                ct.append(f" · {' · '.join(meta)}", style="dim")
            ct.append(f"   👍 {r.up}\n", style="green")
            ct.append(f"  {r.text or '[图片评论]'}", style="white")
            if r.imgs:
                ct.append(f"  [🖼 x{len(r.imgs)}]", style="magenta")

            for sub in th.children[:3]:
                ct.append("\n    ├─ ", style="dim")
                ct.append(f"@{sub.username}", style="cyan")
                if sub.reply_to_user:
                    ct.append(f" 回复 @{sub.reply_to_user}", style="dim")
                ct.append(f": {sub.text or '[图片]'}", style="dim white")
                if sub.up:
                    ct.append(f" (👍{sub.up})", style="dim green")

            if th.has_more or r.child_num > 0:
                ct.append(
                    f"\n    └─ 共 {max(r.child_num, len(th.children))} 条回复 (按 e / Enter 展开完整楼中楼)",
                    style="dim cyan",
                )
            cmt_options.append(Option(ct, id=f"cmt:{idx}"))

        if not cmt_options:
            cmt_options.append(Option(Text("暂无评论", style="dim"), disabled=True))

        cmt_list.set_options(cmt_options)
        if cmt_options and not cmt_options[0].disabled:
            cmt_list.highlighted = 0
            cmt_list.scroll_home(animate=False)

        self._switch_detail_tab("body" if reset_tab else self._detail_tab)

    # -------------------- 事件响应 --------------------

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        list_id = event.option_list.id
        opt_id = event.option_id or ""

        if list_id == "sidebar-list":
            meta = self._sidebar_map.get(opt_id)
            if not meta:
                return
            mode = meta["mode"]
            if mode == "feed":
                self._schedule_load(self._load_recommended_feeds(page=1, reset=True))
            elif mode == "hot":
                self._schedule_load(self._load_hot_ranking(meta["tab"]))
            elif mode == "topic":
                self._schedule_load(self._load_topic_feeds(meta["topic"], page=1, reset=True))
            self.query_one("#feed-list", OptionList).focus()
            return

        if list_id == "feed-list":
            if opt_id.startswith("post:"):
                parts = opt_id.split(":")
                link_id = parts[1]
                idx = int(parts[2])
                summary = self._posts[idx] if 0 <= idx < len(self._posts) else None
                self._schedule_load(self._open_post_detail(link_id, summary=summary, page=1, reset_tab=True))
            elif opt_id.startswith("hot:"):
                idx = int(opt_id.split(":")[1])
                if 0 <= idx < len(self._hot_items):
                    self._schedule_load(self._activate_hot_item(self._hot_items[idx]))
            return

        if list_id == "comment-list":
            self.action_expand_subcomments()

    async def _activate_hot_item(self, item: HotRankItem) -> None:
        if item.item_type == "game" and item.item_id:
            try:
                menu = await self.client.get_topic_menu(appid=item.item_id)
                topic_raw = menu.get("topic") or {}
                if topic_raw.get("topic_id"):
                    await self._load_topic_feeds(TopicItem.from_api(topic_raw), page=1, reset=True)
                    return
            except Exception:
                pass

        topic = await self.client.resolve_topic(item.title)
        if topic:
            await self._load_topic_feeds(topic, page=1, reset=True)
            return

        self.query_one("#list-subbar", Static).update(f"⏳ 正在检索「{item.title}」相关内容...")
        res = await self.client.search(item.title)
        if res.get("posts"):
            self._view_mode = "search_posts"
            self._feed_page = 1
            self._posts = res["posts"][:FEED_PAGE_SIZE]
            self._render_posts_list(f"热点「{item.title}」相关帖子 (共 {len(self._posts)} 条) | Enter:看帖")
        elif res.get("topics"):
            await self._load_topic_feeds(res["topics"][0], page=1, reset=True)
        else:
            tip = "按 l 扫码登录后可搜索该热词全站帖子" if res.get("requires_login") else "未找到直接匹配的帖子"
            self.query_one("#list-subbar", Static).update(f"热点「{item.title}」: {tip}")

    # -------------------- 快捷键 Actions --------------------

    def _in_detail_view(self) -> bool:
        return self.query_one("#content-switcher", ContentSwitcher).current == "detail-pane"

    def action_back_to_list(self) -> None:
        if self._in_detail_view():
            self.query_one("#content-switcher", ContentSwitcher).current = "list-pane"
            feed_list = self.query_one("#feed-list", OptionList)
            if 0 <= self._saved_feed_highlight < feed_list.option_count:
                feed_list.highlighted = self._saved_feed_highlight
            feed_list.focus()

    def action_back_or_quit(self) -> None:
        if self._in_detail_view():
            self.action_back_to_list()
        else:
            self.exit()

    def action_move_down(self) -> None:
        focused = self.focused
        if isinstance(focused, OptionList):
            focused.action_cursor_down()
        elif isinstance(focused, VerticalScroll):
            focused.scroll_down(animate=False)

    def action_move_up(self) -> None:
        focused = self.focused
        if isinstance(focused, OptionList):
            focused.action_cursor_up()
        elif isinstance(focused, VerticalScroll):
            focused.scroll_up(animate=False)

    def action_focus_sidebar(self) -> None:
        if self._in_detail_view():
            self.action_back_to_list()
        else:
            self.query_one("#sidebar-list", OptionList).focus()

    def action_focus_content(self) -> None:
        if self._in_detail_view():
            self._switch_detail_tab("comments" if self._detail_tab == "body" else "body")
        else:
            self.query_one("#feed-list", OptionList).focus()

    def action_toggle_detail_view(self) -> None:
        if not self._in_detail_view():
            return
        next_tab = "comments" if self._detail_tab == "body" else "body"
        self._switch_detail_tab(next_tab)

    def action_open_login(self) -> None:
        def _after_login(res: SessionData | None) -> None:
            self._update_account_banner()
            if res is not None:
                self._schedule_load(self._load_categories())

        self.push_screen(QRLoginModal(self.client), _after_login)

    def action_open_search(self) -> None:
        def _after_search(res: dict[str, Any] | None) -> None:
            if not res:
                return
            if res["type"] == "topic":
                self._schedule_load(self._load_topic_feeds(res["topic"], page=1, reset=True))
                self.query_one("#feed-list", OptionList).focus()
            elif res["type"] == "post":
                post: PostSummary = res["post"]
                self._schedule_load(self._open_post_detail(post.linkid, summary=post, page=1, reset_tab=True))

        self.push_screen(SearchModal(self.client), _after_search)

    def action_filter_all(self) -> None:
        if not self._in_detail_view() and self._view_mode == "topic" and self._current_topic:
            self._topic_is_hot = False
            self._schedule_load(self._load_topic_feeds(self._current_topic, page=1, reset=True))

    def action_filter_hot(self) -> None:
        if not self._in_detail_view() and self._view_mode == "topic" and self._current_topic:
            self._topic_is_hot = True
            self._schedule_load(self._load_topic_feeds(self._current_topic, page=1, reset=True))

    def action_cycle_sort(self) -> None:
        if self._in_detail_view() and self._current_detail:
            self._comment_sort_idx = (self._comment_sort_idx + 1) % len(COMMENT_SORT_MODES)
            self._detail_tab = "comments"
            self._schedule_load(
                self._open_post_detail(
                    self._current_detail.linkid,
                    summary=self._current_post_summary,
                    page=1,
                    reset_tab=False,
                )
            )
            return

        if self._view_mode == "topic" and self._current_topic:
            self._topic_sort_idx = (self._topic_sort_idx + 1) % len(SORT_MODES)
            self._schedule_load(self._load_topic_feeds(self._current_topic, page=1, reset=True))

    def action_toggle_favorite(self) -> None:
        if not self._in_detail_view() and self._view_mode == "topic" and self._current_topic:
            added = self.app_config.toggle_favorite(
                self._current_topic.topic_id, self._current_topic.name
            )
            self._rebuild_sidebar()
            self._update_top_title()
            msg = "⭐ 已收藏板块" if added else "已取消收藏板块"
            self.query_one("#list-subbar", Static).update(f"{msg}: {self._current_topic.name}")

    def action_next_page(self) -> None:
        if self._in_detail_view() and self._current_detail:
            if self._detail_tab != "comments":
                self._switch_detail_tab("comments")
                return
            if self._current_detail.has_more_floors or self._comment_page < self._current_detail.total_page:
                self._schedule_load(
                    self._open_post_detail(
                        self._current_detail.linkid,
                        summary=self._current_post_summary,
                        page=self._comment_page + 1,
                        reset_tab=False,
                    )
                )
            return

        if not self._has_more_feeds:
            return
        if self._view_mode == "feed":
            self._schedule_load(self._load_recommended_feeds(page=self._feed_page + 1, reset=False))
        elif self._view_mode == "topic" and self._current_topic:
            self._schedule_load(self._load_topic_feeds(self._current_topic, page=self._feed_page + 1, reset=False))

    def action_prev_page(self) -> None:
        if self._in_detail_view() and self._current_detail:
            if self._detail_tab == "comments" and self._comment_page > 1:
                self._schedule_load(
                    self._open_post_detail(
                        self._current_detail.linkid,
                        summary=self._current_post_summary,
                        page=self._comment_page - 1,
                        reset_tab=False,
                    )
                )
            return

        if self._feed_page <= 1:
            return
        if self._view_mode == "feed":
            self._schedule_load(self._load_recommended_feeds(page=self._feed_page - 1, reset=False))
        elif self._view_mode == "topic" and self._current_topic:
            self._schedule_load(self._load_topic_feeds(self._current_topic, page=self._feed_page - 1, reset=False))

    def action_refresh_view(self) -> None:
        if self._in_detail_view() and self._current_detail:
            self._schedule_load(
                self._open_post_detail(
                    self._current_detail.linkid,
                    summary=self._current_post_summary,
                    page=self._comment_page,
                    reset_tab=False,
                )
            )
        elif self._view_mode == "feed":
            self._schedule_load(self._load_recommended_feeds(page=1, reset=True))
        elif self._view_mode == "hot":
            self._schedule_load(self._load_hot_ranking(self._current_hot_tab))
        elif self._view_mode == "topic" and self._current_topic:
            self._schedule_load(self._load_topic_feeds(self._current_topic, page=1, reset=True))

    def action_toggle_owner_only(self) -> None:
        if self._in_detail_view() and self._current_detail:
            self._owner_only = not self._owner_only
            self._detail_tab = "comments"
            self._schedule_load(
                self._open_post_detail(
                    self._current_detail.linkid,
                    summary=self._current_post_summary,
                    page=1,
                    reset_tab=False,
                )
            )

    def action_expand_subcomments(self) -> None:
        if not self._in_detail_view() or not self._current_detail:
            return
        if self._detail_tab != "comments":
            self._switch_detail_tab("comments")
            return
        cmt_list = self.query_one("#comment-list", OptionList)
        hl = cmt_list.highlighted
        if hl is None or hl < 0 or hl >= len(self._detail_threads):
            return
        thread = self._detail_threads[hl]
        self.push_screen(SubCommentModal(self.client, self._current_detail.linkid, thread))

    def action_open_browser(self) -> None:
        if self._in_detail_view() and self._current_detail:
            webbrowser.open(self._current_detail.share_url)
        elif not self._in_detail_view() and self._posts:
            feed_list = self.query_one("#feed-list", OptionList)
            hl = feed_list.highlighted
            if hl is not None and 0 <= hl < len(self._posts):
                lid = self._posts[hl].linkid
                webbrowser.open(f"https://www.xiaoheihe.cn/app/bbs/link/{lid}")

    def action_boss_key(self) -> None:
        self.push_screen(BossScreen())
