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
from xhh.config import VALID_THEMES, AppConfig, SessionData
from xhh.models import (
    CommentThread,
    HotRankItem,
    PostDetail,
    PostSummary,
    TopicItem,
    format_count,
    format_time,
    strip_emojis,
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
    """小黑盒 TUI 终端交互应用."""

    CSS_PATH = Path(__file__).with_name("theme.tcss")
    TITLE = "小黑盒 TUI"

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
        Binding("t", "toggle_theme", "切换主题"),
        Binding("grave_accent", "boss_key", "老板键"),
    ]

    def __init__(self, client: HeyboxClient | None = None, theme: str | None = None) -> None:
        super().__init__()
        self._external_client = client is not None
        self.client = client if client is not None else HeyboxClient()
        self.app_config = AppConfig.load()
        norm_theme = (theme or "").strip().lower()
        self._theme: str = norm_theme if norm_theme in VALID_THEMES else self.app_config.theme

        # 导航与列表状态
        self._sidebar_map: dict[str, dict[str, Any]] = {}
        self._hot_topics: list[TopicItem] = []
        self._subscribed_topics: list[TopicItem] = []
        self._view_mode: str = "feed"  # "feed" | "hot" | "topics" | "topic" | "search_posts"
        self._current_hot_tab: str = "news"
        self._current_topic: TopicItem | None = None
        self._topic_is_hot: bool = False
        self._topic_sort_idx: int = 0
        self._topics_page: int = 1
        self._topics_dir_items: list[TopicItem] = []

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

    @property
    def is_stealth(self) -> bool:
        return self._theme == "stealth"

    @property
    def theme_mode(self) -> str:
        return self._theme

    def _fmt_text(self, s: str) -> str:
        return strip_emojis(s) if self.is_stealth else s

    def _apply_theme_styles(self) -> None:
        if self.is_stealth:
            self.add_class("theme-stealth")
            self.title = "zsh"
        else:
            self.remove_class("theme-stealth")
            self.title = "xhh"
        footer = self.query_one("#status-footer", Static)
        if self.is_stealth:
            footer.update("j/k:移 Enter:进 n/p:页 c:文/评 i:图 m:评 P:帖 /:搜 t:主题 l:登 `:切 q:退")
        else:
            footer.update("j/k:移动 Enter:进 n/p:翻页 c:正文/评 i:图 m:评 P:帖 /:搜 t:主题 l:登 `:老板 q:退")

    def compose(self) -> ComposeResult:
        with Horizontal(id="top-bar"):
            yield Static("❖ 小黑盒 TUI", id="top-title")
            yield Static("○ 未登录 [l:登录]", id="top-account")

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
            "j/k:移动 Enter:进 n/p:翻页 c:正文/评 i:图 m:评 P:帖 /:搜 t:主题 l:登 `:老板 q:退",
            id="status-footer",
        )

    async def on_mount(self) -> None:
        self._apply_theme_styles()
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
                elif self._view_mode == "topics" and self._hot_topics:
                    self._render_topics_directory()

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
        stealth = self.is_stealth
        if self.client.session.is_logged_in:
            lvl = f" (Lv.{self.client.session.level})" if self.client.session.level is not None else ""
            raw_name = self.client.session.nickname or self.client.session.heybox_id
            name = self._fmt_text(raw_name)
            if stealth:
                acct.update(f"auth:@{name}{lvl} (l)")
            else:
                acct.update(f"🟢 @{name}{lvl} [l:账号]")
        else:
            acct.update("auth:guest (l)" if stealth else "⚪ 未登录 [l:扫码登录]")

    def _update_top_title(self) -> None:
        title = self.query_one("#top-title", Static)
        stealth = self.is_stealth
        if stealth:
            if self._view_mode == "feed":
                title.update(f"xhh :: feed [p.{self._feed_page}]")
            elif self._view_mode == "hot":
                name = HOT_TAB_TITLES.get(self._current_hot_tab, "rank")
                title.update(f"xhh :: {name} [p.{self._hot_page}]")
            elif self._view_mode == "topics":
                title.update(f"xhh :: topics [p.{self._topics_page}]")
            elif self._view_mode == "topic" and self._current_topic:
                tab_str = "1.all/[2.hot]" if self._topic_is_hot else "[1.all]/2.hot"
                sort_label = SORT_MODES[self._topic_sort_idx][1]
                fav_mark = "*" if self.app_config.is_favorite(self._current_topic.topic_id) else ""
                tname = self._fmt_text(self._current_topic.name)
                title.update(f"xhh :: {fav_mark}{tname} | {tab_str} | {sort_label} [p.{self._feed_page}]")
            elif self._view_mode == "search_posts":
                title.update(f"xhh :: search [p.{self._feed_page}]")
            return

        if self._view_mode == "feed":
            title.update(f"📦 小黑盒 TUI  ━  🔥 推荐 (首页) · 第 {self._feed_page} 页")
        elif self._view_mode == "hot":
            name = HOT_TAB_TITLES.get(self._current_hot_tab, "热点榜单")
            title.update(f"📦 小黑盒 TUI  ━  📈 {name} · 第 {self._hot_page} 页")
        elif self._view_mode == "topics":
            title.update(f"📦 小黑盒 TUI  ━  🎪 热门板块 · 第 {self._topics_page} 页")
        elif self._view_mode == "topic" and self._current_topic:
            tab_str = "1.综合 / [2.热点]" if self._topic_is_hot else "[1.综合] / 2.热点"
            sort_label = SORT_MODES[self._topic_sort_idx][1]
            fav_mark = "⭐ " if self.app_config.is_favorite(self._current_topic.topic_id) else ""
            title.update(
                f"📦 小黑盒 TUI  ━  🎮 {fav_mark}{self._current_topic.name}  │  {tab_str}  │  {sort_label} · 第 {self._feed_page} 页"
            )
        elif self._view_mode == "search_posts":
            title.update(f"📦 小黑盒 TUI  ━  🔍 搜索结果 · 第 {self._feed_page} 页")

    def _rebuild_sidebar(self) -> None:
        sidebar = self.query_one("#sidebar-list", OptionList)
        prev_hl = sidebar.highlighted
        self._sidebar_map.clear()
        options: list[Option] = []
        stealth = self.is_stealth

        nav_items = [
            ("nav:feed", "* 推荐" if stealth else "🔥 推荐 (首页)", {"mode": "feed"}),
            ("nav:hot:news", "* 热议榜" if stealth else "📈 黑盒热议榜", {"mode": "hot", "tab": "news"}),
            ("nav:hot:fusion", "* 热搜榜" if stealth else "🔍 热搜总榜", {"mode": "hot", "tab": "fusion"}),
            ("nav:hot:gamev2", "* 游戏榜" if stealth else "🏆 热门游戏榜", {"mode": "hot", "tab": "gamev2"}),
            ("nav:topics", "* 热门板块" if stealth else "🎪 热门板块", {"mode": "topics"}),
        ]
        for oid, label, meta in nav_items:
            self._sidebar_map[oid] = meta
            options.append(Option(label, id=oid))

        options.append(Option("-- 收藏 --" if stealth else "━━ ⭐ 收藏订阅 ━━", id="hdr:fav", disabled=True))
        seen_ids: set[str] = set()
        fav_topics: list[TopicItem] = [
            TopicItem(topic_id=t["topic_id"], name=t["name"])
            for t in self.app_config.favorite_topics
            if t.get("topic_id") and str(t.get("name") or "").strip()
        ]
        for t in [*fav_topics, *self._subscribed_topics]:
            if not (t.name or "").strip():
                continue
            tid = str(t.topic_id)
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            oid = f"nav:topic:fav:{tid}"
            self._sidebar_map[oid] = {"mode": "topic", "topic": t}
            tname = _truncate_single_line(self._fmt_text(t.name), 18)
            options.append(Option(f"+ {tname}" if stealth else f"⭐ {tname}", id=oid))

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
        stealth = self.is_stealth

        options: list[Option] = []
        for idx, post in enumerate(self._posts, start=1):
            t = Text()
            prefix_str = f"{idx:>2}. "
            if stealth:
                hot_badge = "[hot] " if post.is_hot_post else ""
                media_str = " [vid]" if post.has_video else (f" [img:{post.image_count}]" if post.image_count else "")
            else:
                hot_badge = "🔥[热] " if post.is_hot_post else ""
                media_str = " 🎬视频" if post.has_video else (f" 🖼×{post.image_count}" if post.image_count else "")

            title_raw = self._fmt_text(post.title)
            avail_title_w = max(12, max_w - cell_len(prefix_str) - cell_len(hot_badge) - cell_len(media_str))
            short_title = _truncate_single_line(title_raw, avail_title_w)

            if stealth:
                t.append(prefix_str, style="#687078")
                if hot_badge:
                    t.append(hot_badge, style="#8b949e")
                t.append(short_title, style="#b0b8c0")
                if media_str:
                    t.append(media_str, style="#687078")
            else:
                t.append(prefix_str, style="bold #38bdf8")
                if hot_badge:
                    t.append(hot_badge, style="bold #f43f5e")
                t.append(short_title, style="bold #f8fafc")
                if media_str:
                    t.append(media_str, style="bold #c084fc" if post.has_video else "bold #38bdf8")

            meta_line = Text()
            author_s = self._fmt_text(post.author) if post.author else ""
            topic_s = self._fmt_text(post.topic_name) if post.topic_name else ""
            if stealth:
                if author_s:
                    meta_line.append(f"@{author_s}", style="#687078")
                if topic_s:
                    sep = " | " if author_s else ""
                    meta_line.append(f"{sep}{topic_s}", style="#687078")
                stats_prefix = " | " if (author_s or topic_s) else ""
                meta_line.append(
                    f"{stats_prefix}up:{format_count(post.up)} awd:{format_count(post.award_num)} cmt:{format_count(post.comment_num)}",
                    style="#687078",
                )
                tm = post.create_str or format_time(post.create_at)
                if tm:
                    meta_line.append(f" | {tm}", style="#687078")
            else:
                if author_s:
                    meta_line.append(f"👤 @{author_s}", style="#fbbf24")
                if topic_s:
                    sep = "  ·  " if author_s else ""
                    meta_line.append(f"{sep}🏷 [{topic_s}]", style="bold #2dd4bf")
                stats_prefix = "  ·  " if (author_s or topic_s) else ""
                meta_line.append(
                    f"{stats_prefix}👍 {format_count(post.up)}  ⚡ {format_count(post.award_num)}  💬 {format_count(post.comment_num)}",
                    style="#4ade80",
                )
                tm = post.create_str or format_time(post.create_at)
                if tm:
                    meta_line.append(f"  ·  🕒 {tm}", style="#94a3b8")

            meta_plain = _truncate_single_line(meta_line.plain, max_w - 4)
            t.append("\n    ")
            if len(meta_plain) < len(meta_line.plain):
                t.append(meta_plain, style="#687078" if stealth else "#94a3b8")
            else:
                t.append_text(meta_line)

            options.append(Option(t, id=f"post:{post.linkid}:{idx-1}"))

        if not options:
            options.append(Option(Text("暂无帖子数据，按 r 重试或切换其他板块", style="dim"), disabled=True))

        feed_list.set_options(options)
        if options and not options[0].disabled:
            feed_list.highlighted = 0
            feed_list.scroll_home(animate=False)

        page_info = (
            f"p.{self._feed_page} ({len(self._posts)}/{self._last_rendered_feed_page_size})"
            if stealth
            else f"✨ 第 {self._feed_page} 页 ({len(self._posts)}/{self._last_rendered_feed_page_size} 条)"
        )
        nav_hint = ("n:下一页 " if self._has_more_feeds else "") + ("p:上一页" if self._feed_page > 1 else "")
        if status_msg:
            subbar.update(self._fmt_text(status_msg))
        elif self._view_mode == "topic" and self._current_topic:
            tname = self._fmt_text(self._current_topic.name)
            sub_prefix = f"{tname}" if stealth else f"🎮 「{tname}」"
            subbar.update(
                f"{sub_prefix} · {page_info} | 1:综合 2:热点 s:排序 f:收藏 {nav_hint}".strip()
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
        stealth = self.is_stealth

        medals = {1: "🥇 ", 2: "🥈 ", 3: "🥉 "}
        options: list[Option] = []
        for item in page_items:
            t = Text()
            if stealth:
                rank_str = f"#{item.rank:<2} "
            else:
                rank_str = f"{medals.get(item.rank, '')}#{item.rank:<2} "
            tag_raw = self._fmt_text(item.tag) if item.tag else ""
            tag_str = f" [{tag_raw}]" if tag_raw else ""
            game_str = (" [game]" if stealth else " 🎮") if item.item_type == "game" else ""
            title_raw = self._fmt_text(item.title)
            short_title = _truncate_single_line(
                title_raw, max(12, max_w - cell_len(rank_str) - cell_len(tag_str) - cell_len(game_str))
            )
            if stealth:
                t.append(rank_str, style="#687078")
                t.append(short_title, style="#b0b8c0")
                if tag_str:
                    t.append(tag_str, style="#8b949e")
                if game_str:
                    t.append(game_str, style="#687078")
            else:
                t.append(rank_str, style="bold #fbbf24" if item.rank <= 3 else "bold #38bdf8")
                t.append(short_title, style="bold #f8fafc")
                if tag_str:
                    t.append(tag_str, style="bold #c084fc")
                if game_str:
                    t.append(game_str, style="#38bdf8")
            options.append(Option(t, id=f"hot:{item.rank - 1}"))

        if not options:
            options.append(Option(Text("暂无榜单数据", style="dim"), disabled=True))
        feed_list.set_options(options)
        if options and not options[0].disabled:
            feed_list.highlighted = 0
            feed_list.scroll_home(animate=False)
        tab_name = HOT_TAB_TITLES.get(self._current_hot_tab, "热榜")
        if stealth:
            subbar.update(f"{tab_name} p.{self._hot_page}/{max_page} | Enter:直达 n/p:翻页 r:刷新")
        else:
            subbar.update(f"🏆 {tab_name} 第 {self._hot_page}/{max_page} 页 | Enter:直达板块/搜索 n/p:翻页 r:刷新")

    async def _open_topics_directory(self, page: int = 1) -> None:
        self._view_mode = "topics"
        self._topics_page = page
        self.query_one("#content-switcher", ContentSwitcher).current = "list-pane"
        if not self._hot_topics:
            self.query_one("#list-subbar", Static).update("正在加载热门板块...")
            await self._load_categories()
        self._render_topics_directory()

    def _render_topics_directory(self) -> None:
        subbar = self.query_one("#list-subbar", Static)
        feed_list = self.query_one("#feed-list", OptionList)
        self._topics_dir_items = [t for t in self._hot_topics if (t.name or "").strip()]
        page_size = self._get_hot_page_size()
        self._last_rendered_feed_page_size = page_size
        max_page = max(1, (len(self._topics_dir_items) + page_size - 1) // page_size)
        self._topics_page = max(1, min(self._topics_page, max_page))
        start = (self._topics_page - 1) * page_size
        page_items = self._topics_dir_items[start : start + page_size]
        self._has_more_feeds = self._topics_page < max_page
        self._update_top_title()
        max_w = self._get_content_width()
        stealth = self.is_stealth

        options: list[Option] = []
        for offset_i, topic in enumerate(page_items):
            abs_idx = start + offset_i
            tid = str(topic.topic_id)
            is_fav = self.app_config.is_favorite(tid)
            t = Text()
            rank_str = f"{abs_idx + 1:>2}. "
            fav_tag = (" * " if is_fav else "   ") if stealth else (" ⭐ " if is_fav else " 🎮 ")
            id_tail = f"  (id:{tid})"
            name_raw = self._fmt_text(topic.name)
            short_name = _truncate_single_line(
                name_raw, max(12, max_w - cell_len(rank_str) - cell_len(fav_tag) - cell_len(id_tail))
            )
            if stealth:
                t.append(rank_str, style="#687078")
                t.append(fav_tag, style="#8b949e")
                t.append(short_name, style="#b0b8c0")
                t.append(id_tail, style="#687078")
            else:
                t.append(rank_str, style="bold #38bdf8")
                t.append(fav_tag, style="#fbbf24" if is_fav else "#38bdf8")
                t.append(short_name, style="bold #f8fafc")
                t.append(id_tail, style="#94a3b8")
            options.append(Option(t, id=f"topic_dir:{abs_idx}"))

        if not options:
            options.append(Option(Text("暂无热门板块数据", style="dim"), disabled=True))
        feed_list.set_options(options)
        if options and not options[0].disabled:
            feed_list.highlighted = 0
            feed_list.scroll_home(animate=False)

        if stealth:
            subbar.update(f"topics p.{self._topics_page}/{max_page} ({len(self._topics_dir_items)}) | Enter:进 n/p:页 r:刷")
        else:
            subbar.update(
                f"🎪 热门板块 第 {self._topics_page}/{max_page} 页 (共 {len(self._topics_dir_items)} 个) | Enter:进入板块 n/p:翻页 r:刷新"
            )

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
            self.query_one("#list-subbar", Static).update("正在加载首页推荐流...")
        else:
            self.query_one("#list-subbar", Static).update(f"正在切换到第 {page} 页...")

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
            self.query_one("#list-subbar", Static).update(f"加载失败: {exc} (按 r 重试)")
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
        self.query_one("#list-subbar", Static).update("正在加载热榜...")
        try:
            grouped = await self.client.get_hot_rankings()
        except Exception as exc:
            self.query_one("#list-subbar", Static).update(f"加载热榜失败: {exc}")
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
            self.query_one("#list-subbar", Static).update(f"正在加载「{self._fmt_text(topic.name)}」...")
        else:
            self.query_one("#list-subbar", Static).update(f"正在加载「{self._fmt_text(topic.name)}」第 {page} 页...")

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
            self.query_one("#list-subbar", Static).update(f"加载板块失败: {exc}")
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
            f"搜索结果 第 {self._feed_page}/{max_page} 页 ({len(self._posts)} 条) | Enter:阅读 n/p:翻页"
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
        stealth = self.is_stealth

        t = Text()
        if stealth:
            if self._detail_tab == "body":
                t.append(" [ body ] ", style="bold #101214 on #8b949e")
                t.append(f"  cmt ({cmt_cnt})", style="#687078")
                img_tip = " i:img" if (detail and detail.images) else ""
                t.append(f"  |  c:cmt m:reply{img_tip} n/p:page Esc:back", style="#687078")
            else:
                t.append(" body ", style="#687078")
                t.append(
                    f" [ cmt ({cmt_cnt}) p.{self._comment_page}/{total_pg}{owner_tag} ] ",
                    style="bold #101214 on #8b949e",
                )
                t.append(f"  |  c:body w:reply s:{sort_lbl} o:op e:sub n/p:page", style="#687078")
        else:
            if self._detail_tab == "body":
                t.append(" [ 📄 正文阅读 ] ", style="bold #0b0f19 on #38bdf8")
                t.append(f"  💬 评论 ({cmt_cnt})", style="#93c5fd")
                img_tip = " i:看图" if (detail and detail.images) else ""
                t.append(f"  │  c:看评论 m:发评论{img_tip} n/p:翻页 Esc:返回", style="#94a3b8")
            else:
                t.append(" 📄 正文阅读 ", style="#94a3b8")
                t.append(
                    f" [ 💬 评论 ({cmt_cnt}) · 第 {self._comment_page}/{total_pg} 页{owner_tag} ] ",
                    style="bold #0b0f19 on #fbbf24",
                )
                t.append(f"  │  c:正文 w:回复 s:{sort_lbl} o:楼主 e:楼中楼 n/p:翻页", style="#94a3b8")
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

    def _render_comment_list_options(self) -> None:
        cmt_list = self.query_one("#comment-list", OptionList)
        max_w = self._get_content_width()
        stealth = self.is_stealth
        cmt_options: list[Option] = []

        for idx, th in enumerate(self._detail_threads, start=1):
            r = th.root
            ct = Text()
            hdr = Text()
            uname = self._fmt_text(r.username)
            if stealth:
                hdr.append(f"{idx:>2}. ", style="#687078")
                if r.floor_num:
                    hdr.append(f"#{r.floor_num} ", style="#687078")
                if r.is_cy:
                    hdr.append("[hot] ", style="#8b949e")
                if r.is_top:
                    hdr.append("[top] ", style="#8b949e")
                if r.is_link_owner:
                    hdr.append("[op] ", style="#8b949e")
                hdr.append(f"@{uname}", style="#8b949e")
                if r.user_level is not None:
                    hdr.append(f"(Lv.{r.user_level})", style="#687078")
                if r.ip_location:
                    hdr.append(f"|{r.ip_location}", style="#687078")
                hdr.append(f" up:{r.up}", style="#687078")
                sub_cnt = max(r.child_num, len(th.children))
                if sub_cnt > 0:
                    hdr.append(f" [+{sub_cnt}]", style="#8b949e")
            else:
                hdr.append(f"{idx:>2}. ", style="bold #38bdf8")
                if r.floor_num:
                    hdr.append(f"#{r.floor_num}楼 ", style="bold #2dd4bf")
                if r.is_cy:
                    hdr.append("🔥神评 ", style="bold #f43f5e")
                if r.is_top:
                    hdr.append("📌置顶 ", style="bold #c084fc")
                if r.is_link_owner:
                    hdr.append("👑楼主 ", style="bold #fbbf24")
                hdr.append(f"@{uname}", style="bold #fbbf24")
                if r.user_level is not None:
                    hdr.append(f"(Lv.{r.user_level})", style="#94a3b8")
                if r.ip_location:
                    hdr.append(f"·{r.ip_location}", style="#94a3b8")
                hdr.append(f" 👍{r.up}", style="#4ade80")
                sub_cnt = max(r.child_num, len(th.children))
                if sub_cnt > 0:
                    hdr.append(f" 💬↳{sub_cnt}", style="bold #38bdf8")

            hdr_plain = _truncate_single_line(hdr.plain, max_w)
            if len(hdr_plain) < len(hdr.plain):
                ct.append(hdr_plain, style="#8b949e" if stealth else "bold #f8fafc")
            else:
                ct.append_text(hdr)

            r_body = self._fmt_text(r.text) if r.text else ("[img]" if stealth else "[🖼 图片评论]")
            body_line = _truncate_single_line(r_body or "[img]", max(12, max_w - 4))
            ct.append(f"\n    {body_line}", style="#b0b8c0" if stealth else "#f1f5f9")
            cmt_options.append(Option(ct, id=f"cmt:{idx - 1}"))

        if not cmt_options:
            cmt_options.append(Option(Text("暂无评论", style="dim"), disabled=True))

        cmt_list.set_options(cmt_options)
        if cmt_options and not cmt_options[0].disabled:
            cmt_list.highlighted = 0
            cmt_list.scroll_home(animate=False)

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
        self._render_comment_list_options()

    def _render_detail_header_and_body(self) -> None:
        detail = self._current_detail
        if not detail:
            return
        stealth = self.is_stealth
        max_w = self._get_content_width()

        title_s = self._fmt_text(detail.title)
        author_s = self._fmt_text(detail.author)
        topic_s = self._fmt_text(detail.topic_name) if detail.topic_name else ""
        if stealth:
            stats_tail = (
                f" | @{author_s} up:{format_count(detail.up)} cmt:{format_count(detail.comment_num)}"
            )
            topic_prefix = f"[{topic_s}] " if topic_s else ""
            avail_t = max(12, max_w - cell_len(topic_prefix) - cell_len(stats_tail))
            hdr = Text()
            if topic_prefix:
                hdr.append(topic_prefix, style="#687078")
            hdr.append(_truncate_single_line(title_s, avail_t), style="#b0b8c0")
            hdr.append(stats_tail, style="#687078")
        else:
            stats_tail = (
                f" │ 👤@{author_s} 👍{format_count(detail.up)} ⚡{format_count(detail.award_num)} 💬{format_count(detail.comment_num)}"
            )
            topic_prefix = f"🏷 [{topic_s}] " if topic_s else ""
            avail_t = max(12, max_w - cell_len(topic_prefix) - cell_len(stats_tail))
            hdr = Text()
            if topic_prefix:
                hdr.append(topic_prefix, style="bold #2dd4bf")
            hdr.append(_truncate_single_line(title_s, avail_t), style="bold #f8fafc")
            hdr.append(stats_tail, style="#4ade80")
        self.query_one("#detail-header", Static).update(hdr)

        if stealth:
            meta_parts = [f"@{author_s}"]
            if detail.author_level is not None:
                meta_parts.append(f"Lv.{detail.author_level}")
            if detail.ip_location:
                meta_parts.append(detail.ip_location)
            tm = format_time(detail.create_at)
            if tm:
                meta_parts.append(tm)
            body_text = Text()
            body_text.append(f"{title_s}\n", style="#b0b8c0")
            body_text.append(
                f"{' | '.join(meta_parts)}  |  up:{detail.up}  awd:{detail.award_num}  cmt:{detail.comment_num}  fav:{detail.favour_count}\n",
                style="#687078",
            )
            body_text.append("-" * 50 + "\n\n", style="#687078")
            body_content = self._fmt_text(detail.body) if detail.body else "(暂无文本正文)"
            body_text.append(body_content or "(暂无文本正文)", style="#b0b8c0")
        else:
            meta_parts = [f"👤 @{author_s}"]
            if detail.author_level is not None:
                meta_parts.append(f"🎖 Lv.{detail.author_level}")
            if detail.ip_location:
                meta_parts.append(f"📍 {detail.ip_location}")
            tm = format_time(detail.create_at)
            if tm:
                meta_parts.append(f"🕒 {tm}")
            body_text = Text()
            body_text.append(f"✨ {title_s}\n", style="bold #f8fafc")
            body_text.append(
                f"{'  ·  '.join(meta_parts)}  │  👍 {detail.up}  ⚡ {detail.award_num}  💬 {detail.comment_num}  ⭐ {detail.favour_count}\n",
                style="#fbbf24",
            )
            body_text.append("━" * 54 + "\n\n", style="#2563eb")
            body_content = detail.body or "(暂无文本正文)"
            body_text.append(body_content, style="#f1f5f9")
        self.query_one("#detail-body", Static).update(body_text)

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
        self.query_one("#detail-header", Static).update(f"正在加载帖子 #{link_id} ...")
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
            self.query_one("#detail-header", Static).update(f"加载帖子失败: {exc} (按 Esc 返回)")
            return

        self._current_detail = detail
        self._comment_buffer = list(detail.comments)
        self._comment_api_page = 1
        self._comment_api_has_more = detail.has_more_floors or (detail.total_page > 1)
        self._comment_page = 1

        self._render_detail_header_and_body()
        if reset_tab:
            self.query_one("#post-body-scroll", VerticalScroll).scroll_home(animate=False)

        pills_bar = self.query_one("#image-pills-bar", Horizontal)
        await pills_bar.remove_children()
        if detail.images:
            pills_bar.display = True
            for idx, img_url in enumerate(detail.images):
                lbl = f"[img:{idx + 1}]" if self.is_stealth else f"🖼 图{idx + 1}"
                await pills_bar.mount(ImagePill(lbl, idx, img_url))
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
            elif mode == "topics":
                self._schedule_load(self._open_topics_directory(page=1))
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
            elif opt_id.startswith("topic_dir:"):
                idx = int(opt_id.split(":")[1])
                if 0 <= idx < len(self._topics_dir_items):
                    self._schedule_load(self._load_topic_feeds(self._topics_dir_items[idx], page=1, reset=True))
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

        self.query_one("#list-subbar", Static).update(f"正在检索「{self._fmt_text(item.title)}」相关内容...")
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
            msg = "已收藏板块" if added else "已取消收藏板块"
            self.query_one("#list-subbar", Static).update(f"{msg}: {self._fmt_text(self._current_topic.name)}")

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

        if self._view_mode == "topics":
            if self._has_more_feeds:
                self._topics_page += 1
                self._render_topics_directory()
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

        if self._view_mode == "topics":
            if self._topics_page > 1:
                self._topics_page -= 1
                self._render_topics_directory()
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
        elif self._view_mode == "topics":
            self._hot_topics = []
            self._schedule_load(self._open_topics_directory(page=1))
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

    def action_toggle_theme(self) -> None:
        next_theme = "stealth" if self._theme == "elegant" else "elegant"
        self._theme = self.app_config.set_theme(next_theme)
        self._apply_theme_styles()
        self._update_account_banner()
        self._rebuild_sidebar()

        if self._view_mode == "hot":
            self._render_hot_list()
        elif self._view_mode == "topics":
            self._render_topics_directory()
        elif self._view_mode == "search_posts":
            self._render_search_posts_page(self._feed_page)
        else:
            self._render_posts_list()

        if self._current_detail:
            self._render_detail_header_and_body()
            for pill in self.query(ImagePill):
                pill.update(f"[img:{pill.image_index + 1}]" if self.is_stealth else f"🖼 图{pill.image_index + 1}")
            self._render_comment_list_options()

    def action_boss_key(self) -> None:
        self.push_screen(BossScreen())
