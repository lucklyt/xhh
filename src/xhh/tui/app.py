"""小黑盒社区 Textual 全屏 TUI 主程序（零滑动窗口自适应分页）."""

from __future__ import annotations

import asyncio
import webbrowser
from pathlib import Path
from typing import Any

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
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
from xhh.tui.modals import (
    BossScreen,
    CommentModal,
    ImagePreviewModal,
    NewPostModal,
    QRLoginModal,
    SearchModal,
    SubCommentModal,
)

DEFAULT_PAGE_SIZE = 10
FEED_PAGE_SIZE = DEFAULT_PAGE_SIZE

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


def _truncate_single_line(text: str, max_cells: int) -> str:
    """将文本压平为单行并按终端显示列宽安全截断，杜绝意外折行."""
    clean = " ".join((text or "").replace("\r", " ").replace("\n", " ").split())
    if max_cells <= 1:
        return ""
    if cell_len(clean) <= max_cells:
        return clean
    out: list[str] = []
    used = 0
    limit = max_cells - 1  # 预留 "…"
    for ch in clean:
        w = cell_len(ch)
        if used + w > limit:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


class ImagePill(Static):
    """支持鼠标悬停（250ms 防抖）与点击预览图片的胶囊按钮."""

    def __init__(self, label: str, image_index: int, image_url: str) -> None:
        super().__init__(label, classes="image-pill")
        self.image_index = image_index
        self.image_url = image_url
        self.tooltip = f"悬停或点击预览图片 {image_index + 1}"
        self._hover_timer: Timer | None = None

    def _cancel_timer(self) -> None:
        if self._hover_timer is not None:
            self._hover_timer.stop()
            self._hover_timer = None

    def on_enter(self) -> None:
        self._cancel_timer()
        self._hover_timer = self.set_timer(0.25, self._trigger_hover)

    def on_leave(self) -> None:
        self._cancel_timer()

    def on_unmount(self) -> None:
        self._cancel_timer()

    def on_click(self) -> None:
        self._cancel_timer()
        self._open_preview()

    def _trigger_hover(self) -> None:
        self._hover_timer = None
        if self.is_mounted and not isinstance(self.app.screen, ModalScreen):
            self._open_preview()

    def _open_preview(self) -> None:
        app = self.app
        if isinstance(app, HeyboxApp):
            app.open_image_preview(self.image_index)


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
        Binding("space", "body_page_down", "正文下翻", show=False),
        Binding("pagedown", "next_page", "下一页", show=False),
        Binding("pageup", "prev_page", "上一页", show=False),
        Binding("r", "refresh_view", "刷新"),
        Binding("o", "toggle_owner_only", "只看楼主"),
        Binding("c", "toggle_detail_view", "切换正文/评论"),
        Binding("i", "preview_images", "看图"),
        Binding("m", "comment_post", "发评论"),
        Binding("w", "reply_comment", "回复评论"),
        Binding("P", "new_post", "发帖"),
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

        # 缓冲与自适应分页状态
        self._feed_page: int = 1
        self._recommended_buffer: list[PostSummary] = []
        self._recommended_api_offset: int = 0
        self._recommended_api_has_more: bool = True

        self._topic_buffer: list[PostSummary] = []
        self._topic_api_offset: int = 0
        self._topic_api_lastval: str = ""
        self._topic_api_has_more: bool = True

        self._search_posts_buffer: list[PostSummary] = []

        self._has_more_feeds: bool = True
        self._last_rendered_feed_page_size: int = DEFAULT_PAGE_SIZE

        self._posts: list[PostSummary] = []
        self._hot_items: list[HotRankItem] = []
        self._hot_page: int = 1
        self._saved_feed_highlight: int = 0

        # 帖子详情与评论缓冲状态
        self._current_post_summary: PostSummary | None = None
        self._current_detail: PostDetail | None = None
        self._detail_tab: str = "body"  # "body" | "comments"
        self._comment_page: int = 1
        self._comment_buffer: list[CommentThread] = []
        self._comment_api_page: int = 0
        self._comment_api_has_more: bool = False
        self._owner_only: bool = False
        self._comment_sort_idx: int = 0
        self._detail_threads: list[CommentThread] = []
        self._last_rendered_comment_page_size: int = DEFAULT_PAGE_SIZE

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
                            yield Horizontal(id="image-pills-bar")
                        with Vertical(id="detail-comments-view"):
                            yield Static("", id="comment-bar")
                            yield OptionList(id="comment-list")

        yield Static(
            "j/k:移动 Enter:进 n/p:翻页 c:正文/评 i:看图 m:评论 P:发帖 /:搜 l:登录 `:老板键 q:退",
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

    def on_resize(self, _: events.Resize) -> None:
        if not self.is_mounted:
            return
        if self._in_detail_view():
            if self._detail_tab == "comments" and self._current_detail:
                new_sz = self._get_comment_page_size()
                if new_sz != self._last_rendered_comment_page_size:
                    self._schedule_load(self._show_comment_page(self._comment_page))
        else:
            new_sz = self._get_feed_page_size()
            if new_sz != self._last_rendered_feed_page_size:
                if self._view_mode == "feed" and self._recommended_buffer:
                    self._schedule_load(self._load_recommended_feeds(page=self._feed_page, reset=False))
                elif self._view_mode == "topic" and self._current_topic and self._topic_buffer:
                    self._schedule_load(self._load_topic_feeds(self._current_topic, page=self._feed_page, reset=False))
                elif self._view_mode == "search_posts" and self._search_posts_buffer:
                    self._render_search_posts_page(self._feed_page)
                elif self._view_mode == "hot" and self._hot_items:
                    self._render_hot_list()

    def _schedule_load(self, coro: Any) -> None:
        if self._load_task and not self._load_task.done():
            self._load_task.cancel()
        self._load_task = asyncio.create_task(coro)

    def _get_feed_page_size(self) -> int:
        """根据当前窗口高度计算帖子列表单页条数（标准 24 行下默认 10 条）."""
        feed_list = self.query_one("#feed-list", OptionList)
        h = feed_list.size.height
        if h <= 0:
            term_h = self.size.height or 24
            # top-bar(1) + list-subbar(1) + status-footer(1) = 3
            h = max(10, term_h - 3)
        return max(5, h // 2)

    def _get_comment_page_size(self) -> int:
        """根据当前窗口高度计算评论列表单页条数（标准 24 行下默认 10 条）."""
        cmt_list = self.query_one("#comment-list", OptionList)
        h = cmt_list.size.height
        if h <= 0:
            term_h = self.size.height or 24
            # top-bar(1) + detail-header(1) + detail-tabbar(1) + status-footer(1) = 4
            h = max(10, term_h - 4)
        return max(5, h // 2)

    def _get_hot_page_size(self) -> int:
        feed_list = self.query_one("#feed-list", OptionList)
        h = feed_list.size.height
        if h <= 0:
            term_h = self.size.height or 24
            h = max(10, term_h - 3)
        return max(5, h)

    def _get_content_width(self) -> int:
        switcher = self.query_one("#content-switcher", ContentSwitcher)
        w = switcher.size.width
        if w <= 0:
            w = max(40, (self.size.width or 80) - 26)
        return max(30, w - 4)

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
            title.update(f"📦 小黑盒 CLI  |  {name} · 第 {self._hot_page} 页")
        elif self._view_mode == "topic" and self._current_topic:
            tab_str = "1.综合 / [2.热点]" if self._topic_is_hot else "[1.综合] / 2.热点"
            sort_label = SORT_MODES[self._topic_sort_idx][1]
            fav_mark = "⭐" if self.app_config.is_favorite(self._current_topic.topic_id) else ""
            title.update(
                f"📦 小黑盒 CLI  |  🎮 {fav_mark}{self._current_topic.name}  |  {tab_str}  |  {sort_label} · 第 {self._feed_page} 页"
            )
        elif self._view_mode == "search_posts":
            title.update(f"📦 小黑盒 CLI  |  🔍 搜索结果 · 第 {self._feed_page} 页")

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
        max_w = self._get_content_width()

        options: list[Option] = []
        for idx, post in enumerate(self._posts, start=1):
            t = Text()
            prefix_str = f"{idx:>2}. "
            hot_badge = "[热] " if post.is_hot_post else ""
            media_str = " 🎬" if post.has_video else (f" 🖼{post.image_count}" if post.image_count else "")
            avail_title_w = max(12, max_w - cell_len(prefix_str) - cell_len(hot_badge) - cell_len(media_str))
            short_title = _truncate_single_line(post.title, avail_title_w)

            t.append(prefix_str, style="bold cyan")
            if hot_badge:
                t.append(hot_badge, style="bold red")
            t.append(short_title, style="bold white")
            if media_str:
                t.append(media_str, style="magenta" if post.has_video else "blue")

            meta_line = Text()
            if post.author:
                meta_line.append(f"@{post.author}", style="yellow")
            if post.topic_name:
                sep = " · " if post.author else ""
                meta_line.append(f"{sep}[{post.topic_name}]", style="cyan")
            stats_prefix = " · " if (post.author or post.topic_name) else ""
            meta_line.append(
                f"{stats_prefix}👍{format_count(post.up)} ⚡{format_count(post.award_num)} 💬{format_count(post.comment_num)}",
                style="green",
            )
            tm = post.create_str or format_time(post.create_at)
            if tm:
                meta_line.append(f" · {tm}", style="dim")

            meta_plain = _truncate_single_line(meta_line.plain, max_w - 4)
            t.append("\n    ")
            if len(meta_plain) < len(meta_line.plain):
                t.append(meta_plain, style="dim")
            else:
                t.append_text(meta_line)

            options.append(Option(t, id=f"post:{post.linkid}:{idx-1}"))

        if not options:
            options.append(Option(Text("暂无帖子数据，按 r 重试或切换其他板块", style="dim"), disabled=True))

        feed_list.set_options(options)
        if options and not options[0].disabled:
            feed_list.highlighted = 0
            feed_list.scroll_home(animate=False)

        page_info = f"第 {self._feed_page} 页 (本页 {len(self._posts)}/{self._last_rendered_feed_page_size} 条)"
        nav_hint = ("n:下一页 " if self._has_more_feeds else "") + ("p:上一页" if self._feed_page > 1 else "")
        if status_msg:
            subbar.update(status_msg)
        elif self._view_mode == "topic" and self._current_topic:
            subbar.update(
                f"「{self._current_topic.name}」· {page_info} | 1:综合 2:热点 s:排序 f:收藏 {nav_hint}".strip()
            )
        else:
            subbar.update(f"{page_info} | Enter:阅读 {nav_hint} r:刷新".strip())

    def _render_hot_list(self) -> None:
        subbar = self.query_one("#list-subbar", Static)
        feed_list = self.query_one("#feed-list", OptionList)
        page_size = self._get_hot_page_size()
        max_page = max(1, (len(self._hot_items) + page_size - 1) // page_size)
        self._hot_page = max(1, min(self._hot_page, max_page))
        start = (self._hot_page - 1) * page_size
        page_items = self._hot_items[start : start + page_size]
        self._has_more_feeds = self._hot_page < max_page
        self._update_top_title()
        max_w = self._get_content_width()

        options: list[Option] = []
        for item in page_items:
            t = Text()
            rank_str = f"#{item.rank:<2} "
            tag_str = f" [{item.tag}]" if item.tag else ""
            game_str = " 🎮" if item.item_type == "game" else ""
            short_title = _truncate_single_line(
                item.title, max(12, max_w - cell_len(rank_str) - cell_len(tag_str) - cell_len(game_str))
            )
            t.append(rank_str, style="bold red" if item.rank <= 3 else "bold cyan")
            t.append(short_title, style="bold white")
            if tag_str:
                t.append(tag_str, style="bold magenta")
            if game_str:
                t.append(game_str, style="dim cyan")
            options.append(Option(t, id=f"hot:{item.rank - 1}"))

        if not options:
            options.append(Option(Text("暂无榜单数据", style="dim"), disabled=True))
        feed_list.set_options(options)
        if options and not options[0].disabled:
            feed_list.highlighted = 0
            feed_list.scroll_home(animate=False)
        tab_name = HOT_TAB_TITLES.get(self._current_hot_tab, "热榜")
        subbar.update(f"{tab_name} 第 {self._hot_page}/{max_page} 页 | Enter:直达板块/搜索 n/p:翻页 r:刷新")

    async def _load_recommended_feeds(self, page: int = 1, reset: bool = False) -> None:
        self._view_mode = "feed"
        self.query_one("#content-switcher", ContentSwitcher).current = "list-pane"
        page_size = self._get_feed_page_size()
        self._last_rendered_feed_page_size = page_size

        if reset:
            self._feed_page = 1
            self._recommended_buffer.clear()
            self._recommended_api_offset = 0
            self._recommended_api_has_more = True
            self._posts = []
            self.query_one("#list-subbar", Static).update("⏳ 正在加载首页推荐流...")
        else:
            self.query_one("#list-subbar", Static).update(f"⏳ 正在切换到第 {page} 页...")

        need_end = page * page_size
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

        start = (page - 1) * page_size
        while start >= len(self._recommended_buffer) and page > 1:
            page -= 1
            start = (page - 1) * page_size

        page_posts = self._recommended_buffer[start : start + page_size]
        self._feed_page = page
        self._posts = page_posts
        self._has_more_feeds = len(self._recommended_buffer) > start + page_size or self._recommended_api_has_more
        self._render_posts_list()

    async def _load_hot_ranking(self, tab_type: str) -> None:
        self._view_mode = "hot"
        self._current_hot_tab = tab_type
        self._hot_page = 1
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
        page_size = self._get_feed_page_size()
        self._last_rendered_feed_page_size = page_size

        if reset:
            self._feed_page = 1
            self._topic_buffer.clear()
            self._topic_api_offset = 0
            self._topic_api_lastval = ""
            self._topic_api_has_more = True
            self._posts = []
            self.query_one("#list-subbar", Static).update(f"⏳ 正在加载「{topic.name}」...")
        else:
            self.query_one("#list-subbar", Static).update(f"⏳ 正在加载「{topic.name}」第 {page} 页...")

        sort_key = SORT_MODES[self._topic_sort_idx][0]
        need_end = page * page_size
        seen_ids = {p.linkid for p in self._topic_buffer}

        try:
            while len(self._topic_buffer) < need_end and self._topic_api_has_more:
                fetch_limit = max(page_size, 20)
                res = await self.client.get_topic_feeds(
                    topic_id=topic.topic_id,
                    is_hot=self._topic_is_hot,
                    sort_filter=sort_key,
                    offset=self._topic_api_offset,
                    limit=fetch_limit,
                    lastval=self._topic_api_lastval,
                )
                batch: list[PostSummary] = res["posts"] or []
                self._topic_api_lastval = res.get("lastval") or ""
                if not batch:
                    self._topic_api_has_more = False
                    break
                added = 0
                for p in batch:
                    if p.linkid not in seen_ids:
                        seen_ids.add(p.linkid)
                        self._topic_buffer.append(p)
                        added += 1
                self._topic_api_offset += len(batch)
                if not res.get("has_more") or added == 0:
                    self._topic_api_has_more = False
                    break
        except Exception as exc:
            self.query_one("#list-subbar", Static).update(f"❌ 加载板块失败: {exc}")
            return

        start = (page - 1) * page_size
        while start >= len(self._topic_buffer) and page > 1:
            page -= 1
            start = (page - 1) * page_size

        page_posts = self._topic_buffer[start : start + page_size]
        self._feed_page = page
        self._posts = page_posts
        self._has_more_feeds = len(self._topic_buffer) > start + page_size or self._topic_api_has_more
        self._render_posts_list()

    def _render_search_posts_page(self, page: int) -> None:
        page_size = self._get_feed_page_size()
        self._last_rendered_feed_page_size = page_size
        max_page = max(1, (len(self._search_posts_buffer) + page_size - 1) // page_size)
        self._feed_page = max(1, min(page, max_page))
        start = (self._feed_page - 1) * page_size
        self._posts = self._search_posts_buffer[start : start + page_size]
        self._has_more_feeds = self._feed_page < max_page
        self._render_posts_list(
            f"🔍 搜索结果 第 {self._feed_page}/{max_page} 页 (本页 {len(self._posts)} 条) | Enter:阅读 n/p:翻页"
        )

    def _update_detail_tabbar(self) -> None:
        tabbar = self.query_one("#detail-tabbar", Static)
        detail = self._current_detail
        cmt_cnt = detail.comment_num if detail else 0
        page_size = self._get_comment_page_size()
        total_est = max(len(self._comment_buffer), detail.total_floor_num if detail else 0)
        total_pg = max(1, (total_est + page_size - 1) // page_size) if total_est else 1
        sort_lbl = COMMENT_SORT_MODES[self._comment_sort_idx][1]
        owner_tag = " [只看楼主]" if self._owner_only else ""

        t = Text()
        if self._detail_tab == "body":
            t.append(" [ 📄 正文 ] ", style="bold black on #58a6ff")
            t.append(f"  💬 评论 ({cmt_cnt})", style="dim")
            img_tip = " i:看图" if (detail and detail.images) else ""
            t.append(f"  |  c:看评论 m:发评论{img_tip} n/p:翻页 Esc:返回", style="dim")
        else:
            t.append(" 📄 正文 ", style="dim")
            t.append(
                f" [ 💬 评论 ({cmt_cnt}) · 第 {self._comment_page}/{total_pg} 页{owner_tag} ] ",
                style="bold black on #f0883e",
            )
            t.append(f"  |  c:正文 w:回复 s:{sort_lbl} o:楼主 e:楼中楼 n/p:翻页", style="dim")
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

    async def _show_comment_page(self, page: int) -> None:
        if not self._current_detail:
            return
        page_size = self._get_comment_page_size()
        self._last_rendered_comment_page_size = page_size
        need_end = page * page_size
        seen_cids = {th.root.commentid for th in self._comment_buffer}
        cmt_sort = COMMENT_SORT_MODES[self._comment_sort_idx][0]

        while len(self._comment_buffer) < need_end and self._comment_api_has_more:
            next_api_page = self._comment_api_page + 1
            try:
                more = await self.client.get_post_detail(
                    link_id=self._current_detail.linkid,
                    page=next_api_page,
                    limit=20,
                    owner_only=self._owner_only,
                    sort_filter=cmt_sort,
                )
            except Exception:
                break
            self._comment_api_page = next_api_page
            added = 0
            for th in more.comments:
                if th.root.commentid not in seen_cids:
                    seen_cids.add(th.root.commentid)
                    self._comment_buffer.append(th)
                    added += 1
            self._comment_api_has_more = (
                more.has_more_floors or next_api_page < more.total_page
            ) and added > 0
            if not more.comments:
                self._comment_api_has_more = False
                break

        start = (page - 1) * page_size
        while start >= len(self._comment_buffer) and page > 1:
            page -= 1
            start = (page - 1) * page_size

        self._comment_page = page
        self._detail_threads = self._comment_buffer[start : start + page_size]

        cmt_list = self.query_one("#comment-list", OptionList)
        max_w = self._get_content_width()
        cmt_options: list[Option] = []

        for idx, th in enumerate(self._detail_threads, start=1):
            r = th.root
            ct = Text()
            hdr = Text()
            hdr.append(f"{idx:>2}. ", style="bold cyan")
            if r.floor_num:
                hdr.append(f"#{r.floor_num}楼 ", style="cyan")
            if r.is_cy:
                hdr.append("★神评 ", style="bold red")
            if r.is_top:
                hdr.append("📌置顶 ", style="bold magenta")
            if r.is_link_owner:
                hdr.append("👑楼主 ", style="bold yellow")
            hdr.append(f"@{r.username}", style="bold yellow")
            if r.user_level is not None:
                hdr.append(f"(Lv.{r.user_level})", style="dim")
            if r.ip_location:
                hdr.append(f"·{r.ip_location}", style="dim")
            hdr.append(f" 👍{r.up}", style="green")
            sub_cnt = max(r.child_num, len(th.children))
            if sub_cnt > 0:
                hdr.append(f" [↳{sub_cnt}回复]", style="bold cyan")

            hdr_plain = _truncate_single_line(hdr.plain, max_w)
            if len(hdr_plain) < len(hdr.plain):
                ct.append(hdr_plain, style="bold white")
            else:
                ct.append_text(hdr)

            body_line = _truncate_single_line(r.text or "[图片评论]", max(12, max_w - 4))
            ct.append(f"\n    {body_line}", style="white")
            cmt_options.append(Option(ct, id=f"cmt:{idx - 1}"))

        if not cmt_options:
            cmt_options.append(Option(Text("暂无评论", style="dim"), disabled=True))

        cmt_list.set_options(cmt_options)
        if cmt_options and not cmt_options[0].disabled:
            cmt_list.highlighted = 0
            cmt_list.scroll_home(animate=False)

        self._update_detail_tabbar()

    async def _open_post_detail(
        self,
        link_id: str,
        summary: PostSummary | None = None,
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

        fallback_imgs = summary.imgs if summary else None
        cmt_sort = COMMENT_SORT_MODES[self._comment_sort_idx][0]
        try:
            detail = await self.client.get_post_detail(
                link_id=link_id,
                page=1,
                limit=20,
                owner_only=self._owner_only,
                sort_filter=cmt_sort,
                fallback_imgs=fallback_imgs,
            )
        except Exception as exc:
            self.query_one("#detail-header", Static).update(f"❌ 加载帖子失败: {exc} (按 Esc 返回)")
            return

        self._current_detail = detail
        self._comment_buffer = list(detail.comments)
        self._comment_api_page = 1
        self._comment_api_has_more = detail.has_more_floors or (detail.total_page > 1)
        self._comment_page = 1

        # 紧凑单行 Header
        max_w = self._get_content_width()
        stats_tail = f" | @{detail.author} 👍{format_count(detail.up)} ⚡{format_count(detail.award_num)} 💬{format_count(detail.comment_num)}"
        topic_prefix = f"[{detail.topic_name}] " if detail.topic_name else ""
        avail_t = max(12, max_w - cell_len(topic_prefix) - cell_len(stats_tail))
        hdr = Text()
        if topic_prefix:
            hdr.append(topic_prefix, style="bold cyan")
        hdr.append(_truncate_single_line(detail.title, avail_t), style="bold white")
        hdr.append(stats_tail, style="green")
        self.query_one("#detail-header", Static).update(hdr)

        # 正文与完整元信息
        meta_parts = [f"@{detail.author}"]
        if detail.author_level is not None:
            meta_parts.append(f"Lv.{detail.author_level}")
        if detail.ip_location:
            meta_parts.append(detail.ip_location)
        tm = format_time(detail.create_at)
        if tm:
            meta_parts.append(tm)
        body_text = Text()
        body_text.append(f"{detail.title}\n", style="bold white")
        body_text.append(
            f"{' · '.join(meta_parts)}  |  👍 {detail.up}  ⚡ {detail.award_num}  💬 {detail.comment_num}  ⭐ {detail.favour_count}\n",
            style="yellow",
        )
        body_text.append("─" * 50 + "\n\n", style="dim")
        body_text.append(detail.body or "(暂无文本正文)")
        if detail.images:
            body_text.append("\n\n─── 🖼 帖子图片列表 (按 b 可在浏览器查看原图) ───\n", style="bold cyan")
            for idx, img_url in enumerate(detail.images, start=1):
                body_text.append(f"  [图{idx}] ", style="bold blue")
                body_text.append(f"{img_url}\n", style="dim underline")
        self.query_one("#detail-body", Static).update(body_text)
        if reset_tab:
            self.query_one("#post-body-scroll", VerticalScroll).scroll_home(animate=False)

        pills_bar = self.query_one("#image-pills-bar", Horizontal)
        await pills_bar.remove_children()
        if detail.images:
            pills_bar.display = True
            for idx, img_url in enumerate(detail.images):
                await pills_bar.mount(ImagePill(f"🖼 图{idx + 1}", idx, img_url))
        else:
            pills_bar.display = False

        await self._show_comment_page(1)
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
                self._schedule_load(self._open_post_detail(link_id, summary=summary, reset_tab=True))
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
            self._search_posts_buffer = res["posts"]
            self._render_search_posts_page(1)
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

    def action_body_page_down(self) -> None:
        if self._in_detail_view() and self._detail_tab == "body":
            self.query_one("#post-body-scroll", VerticalScroll).scroll_page_down(animate=False)

    def action_focus_sidebar(self) -> None:
        if self._in_detail_view():
            self.action_back_to_list()
        else:
            self.query_one("#sidebar-list", OptionList).focus()

    def action_focus_content(self) -> None:
        if self._in_detail_view():
            self.action_toggle_detail_view()
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
                self._schedule_load(self._open_post_detail(post.linkid, summary=post, reset_tab=True))

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
            if self._detail_tab == "body":
                self.query_one("#post-body-scroll", VerticalScroll).scroll_page_down(animate=False)
                return
            page_size = self._get_comment_page_size()
            has_next = (
                len(self._comment_buffer) > self._comment_page * page_size
                or self._comment_api_has_more
            )
            if has_next:
                self._schedule_load(self._show_comment_page(self._comment_page + 1))
            return

        if self._view_mode == "hot":
            if self._has_more_feeds:
                self._hot_page += 1
                self._render_hot_list()
            return

        if self._view_mode == "search_posts":
            if self._has_more_feeds:
                self._render_search_posts_page(self._feed_page + 1)
            return

        if not self._has_more_feeds:
            return
        if self._view_mode == "feed":
            self._schedule_load(self._load_recommended_feeds(page=self._feed_page + 1, reset=False))
        elif self._view_mode == "topic" and self._current_topic:
            self._schedule_load(self._load_topic_feeds(self._current_topic, page=self._feed_page + 1, reset=False))

    def action_prev_page(self) -> None:
        if self._in_detail_view() and self._current_detail:
            if self._detail_tab == "body":
                self.query_one("#post-body-scroll", VerticalScroll).scroll_page_up(animate=False)
                return
            if self._comment_page > 1:
                self._schedule_load(self._show_comment_page(self._comment_page - 1))
            return

        if self._view_mode == "hot":
            if self._hot_page > 1:
                self._hot_page -= 1
                self._render_hot_list()
            return

        if self._view_mode == "search_posts":
            if self._feed_page > 1:
                self._render_search_posts_page(self._feed_page - 1)
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

    def open_image_preview(self, index: int = 0) -> None:
        if not self._current_detail or not self._current_detail.images:
            return
        self.push_screen(ImagePreviewModal(self._current_detail.images, start_index=index))

    def action_preview_images(self) -> None:
        if not self._in_detail_view() or not self._current_detail:
            return
        if self._detail_tab == "comments":
            cmt_list = self.query_one("#comment-list", OptionList)
            hl = cmt_list.highlighted
            if hl is not None and 0 <= hl < len(self._detail_threads):
                imgs = self._detail_threads[hl].root.imgs
                if imgs:
                    self.push_screen(ImagePreviewModal(imgs, start_index=0))
                    return
        if self._current_detail.images:
            self.push_screen(ImagePreviewModal(self._current_detail.images, start_index=0))

    def action_comment_post(self) -> None:
        if not self._in_detail_view() or not self._current_detail:
            return
        link_id = self._current_detail.linkid

        def _after_comment(res: dict[str, Any] | None) -> None:
            if res is not None:
                self._detail_tab = "comments"
                self._schedule_load(
                    self._open_post_detail(
                        link_id,
                        summary=self._current_post_summary,
                        reset_tab=False,
                    )
                )

        self.push_screen(
            CommentModal(
                self.client,
                link_id=link_id,
                target_label=f"发表评论到: {self._current_detail.title[:40]}",
            ),
            _after_comment,
        )

    def action_reply_comment(self) -> None:
        if not self._in_detail_view() or not self._current_detail:
            return
        if self._detail_tab == "comments":
            cmt_list = self.query_one("#comment-list", OptionList)
            hl = cmt_list.highlighted
            if hl is not None and 0 <= hl < len(self._detail_threads):
                root = self._detail_threads[hl].root
                link_id = self._current_detail.linkid

                def _after_reply(res: dict[str, Any] | None) -> None:
                    if res is not None:
                        self._schedule_load(
                            self._open_post_detail(
                                link_id,
                                summary=self._current_post_summary,
                                reset_tab=False,
                            )
                        )

                self.push_screen(
                    CommentModal(
                        self.client,
                        link_id=link_id,
                        root_id=root.commentid,
                        reply_id=root.commentid,
                        target_label=f"回复 #{root.floor_num}楼 @{root.username}",
                    ),
                    _after_reply,
                )
                return
        self.action_comment_post()

    def action_new_post(self) -> None:
        default_topic = self._current_topic.name if self._current_topic else "盒友杂谈"

        def _after_publish(res: dict[str, Any] | None) -> None:
            if not res:
                return
            new_lid = str(res.get("link_id") or "")
            if new_lid:
                self._schedule_load(self._open_post_detail(new_lid, reset_tab=True))
            else:
                self.action_refresh_view()

        self.push_screen(NewPostModal(self.client, default_topic=default_topic), _after_publish)

    def action_boss_key(self) -> None:
        self.push_screen(BossScreen())
