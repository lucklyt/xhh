"""TUI 弹窗与特殊屏幕组件."""

from __future__ import annotations

import asyncio
import webbrowser
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from xhh.client import HeyboxClient
from xhh.config import SessionData
from xhh.image import fetch_image_to_cache, open_image_quicklook, render_image_file_rich
from xhh.models import CommentReply, CommentThread, format_count, format_time
from xhh.qr import render_qr_rich, save_qr_html


class QRLoginModal(ModalScreen[SessionData | None]):
    """小黑盒 App 扫码登录弹窗."""

    BINDINGS = [
        Binding("escape", "close_modal", "关闭"),
        Binding("q", "close_modal", "关闭"),
        Binding("r", "refresh_qr", "刷新二维码"),
        Binding("m", "toggle_qr_mode", "切换显示模式"),
        Binding("b", "open_qr_browser", "浏览器打开二维码"),
        Binding("u", "logout_account", "退出当前账号"),
    ]

    def __init__(self, client: HeyboxClient) -> None:
        super().__init__()
        self.client = client
        self._stop_polling = False
        self._poll_task: asyncio.Task[None] | None = None
        self._logged_in_session: SessionData | None = None
        self._current_qr_url: str = ""
        self._compact_qr: bool = True

    def compose(self) -> ComposeResult:
        with Vertical(id="qr-dialog"):
            yield Static("📱 小黑盒扫码登录", id="qr-title")
            with VerticalScroll(id="qr-scroll"):
                yield Static("正在生成登录二维码...", id="qr-box")
            yield Static("⏳ 初始化中...  |  b:浏览器  m:切尺寸  r:刷新  Esc:关闭", id="qr-status")

    def on_mount(self) -> None:
        self._refresh_banner()
        self._start_qr_flow()

    def on_unmount(self) -> None:
        self._stop_polling = True
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()

    def _refresh_banner(self) -> None:
        title = self.query_one("#qr-title", Static)
        if self.client.session.is_logged_in:
            lvl = f"(Lv.{self.client.session.level})" if self.client.session.level is not None else ""
            title.update(f"📱 小黑盒扫码登录  |  🟢 @{self.client.session.nickname}{lvl}")
        else:
            title.update("📱 小黑盒扫码登录  |  ⚪ 未登录")

    def _start_qr_flow(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        self._stop_polling = False
        self._poll_task = asyncio.create_task(self._run_qr_loop())

    async def _run_qr_loop(self) -> None:
        qr_box = self.query_one("#qr-box", Static)
        status_lbl = self.query_one("#qr-status", Static)
        qr_box.update("正在获取二维码...")
        status_lbl.update("⏳ 请求官方登录二维码中...")

        try:
            qr_info = await self.client.get_qrcode()
        except Exception as exc:
            qr_box.update("获取失败")
            status_lbl.update(f"❌ {exc}（按 r 重试）")
            return

        self._current_qr_url = qr_info["qr_url"]
        qr_box.update(render_qr_rich(self._current_qr_url, border=1, compact=self._compact_qr))
        hint_tail = "b:浏览器 m:切尺寸 r:刷新 u:登出 Esc:关"
        status_lbl.update(f"⏳ 等待 App 扫码 | {hint_tail}")

        qr_token = qr_info["qr_token"]
        while not self._stop_polling:
            await asyncio.sleep(1.5)
            if self._stop_polling:
                break
            try:
                res = await self.client.poll_qr_state(qr_token)
            except Exception as exc:
                status_lbl.update(f"⚠️ 轮询波动: {exc}")
                continue

            state = res.get("state")
            if state == "waiting":
                status_lbl.update(f"⏳ 等待 App 扫码 | {hint_tail}")
            elif state == "scanned":
                status_lbl.update("📱 已扫码！请在手机端点击「确认登录」")
            elif state == "confirmed":
                session = res["session"]
                self._logged_in_session = session
                self._refresh_banner()
                status_lbl.update(f"✅ 登录成功！欢迎 @{session.nickname}")
                await asyncio.sleep(0.8)
                self.dismiss(session)
                return
            elif state == "expired":
                status_lbl.update("⌛ 二维码已过期，按 r 刷新 | Esc:关闭")
                return
            elif state == "requires_2fa":
                status_lbl.update(f"⚠️ {res.get('msg')}")
                return
            else:
                status_lbl.update(f"❌ {res.get('msg', '登录失败')}（按 r 重试）")
                return

    def action_refresh_qr(self) -> None:
        self._start_qr_flow()

    def action_toggle_qr_mode(self) -> None:
        self._compact_qr = not self._compact_qr
        dialog = self.query_one("#qr-dialog")
        dialog.styles.width = 60 if self._compact_qr else 90
        if self._current_qr_url:
            qr_box = self.query_one("#qr-box", Static)
            qr_box.update(render_qr_rich(self._current_qr_url, border=1, compact=self._compact_qr))

    def action_open_qr_browser(self) -> None:
        if not self._current_qr_url:
            return
        html_path = save_qr_html(self._current_qr_url)
        webbrowser.open(html_path.as_uri())
        self.query_one("#qr-status", Static).update(f"🌐 已在浏览器打开高清二维码，请扫码确认...")

    async def action_logout_account(self) -> None:
        if not self.client.session.is_logged_in:
            self.query_one("#qr-status", Static).update("当前未登录任何账号")
            return
        await self.client.logout()
        self._refresh_banner()
        self.query_one("#qr-status", Static).update("🚪 已退出登录（可扫码重新登录）")

    def action_close_modal(self) -> None:
        self._stop_polling = True
        self.dismiss(self._logged_in_session)


class SearchModal(ModalScreen[dict[str, Any] | None]):
    """搜索游戏板块 / 全站帖子弹窗."""

    BINDINGS = [
        Binding("escape", "close_modal", "关闭"),
    ]

    def __init__(self, client: HeyboxClient) -> None:
        super().__init__()
        self.client = client
        self._entries: dict[str, dict[str, Any]] = {}
        self._search_task: asyncio.Task[None] | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="search-dialog"):
            yield Static("🔍 搜索游戏板块 / 帖子 (输入关键词后回车，↑/↓ 选择)", id="search-title")
            yield Input(placeholder="输入游戏名或关键词，例如: 黑神话、CS2、Steam...", id="search-input")
            yield Static("提示: 可直接匹配热门游戏板块；登录后可搜索全站帖子", id="search-status")
            yield OptionList(id="search-results")

    def on_mount(self) -> None:
        self.query_one("#search-input", Input).focus()

    def on_unmount(self) -> None:
        if self._search_task and not self._search_task.done():
            self._search_task.cancel()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if not query:
            return
        if self._search_task and not self._search_task.done():
            self._search_task.cancel()
        self._search_task = asyncio.create_task(self._perform_search(query))

    async def _perform_search(self, query: str) -> None:
        status_lbl = self.query_one("#search-status", Static)
        results_list = self.query_one("#search-results", OptionList)
        status_lbl.update(f"⏳ 正在搜索「{query}」...")

        try:
            res = await self.client.search(query)
        except Exception as exc:
            status_lbl.update(f"❌ 搜索失败: {exc}")
            return

        self._entries.clear()
        options: list[Option] = []

        for idx, topic in enumerate(res.get("topics") or []):
            opt_id = f"topic:{idx}"
            self._entries[opt_id] = {"type": "topic", "topic": topic}
            prompt = Text.assemble(
                ("🎮 [板块] ", "bold cyan"),
                (topic.name, "bold white"),
                (f"  (topic_id: {topic.topic_id})", "dim"),
            )
            options.append(Option(prompt, id=opt_id))

        for idx, post in enumerate(res.get("posts") or []):
            opt_id = f"post:{idx}"
            self._entries[opt_id] = {"type": "post", "post": post}
            prompt = Text.assemble(
                ("📝 [帖子] ", "bold green"),
                (post.title, "white"),
                (f"  @{post.author} · 👍{format_count(post.up)} · 💬{format_count(post.comment_num)}", "dim"),
            )
            options.append(Option(prompt, id=opt_id))

        for idx, sug in enumerate(res.get("suggestions") or []):
            opt_id = f"sug:{idx}"
            self._entries[opt_id] = {"type": "suggestion", "query": sug}
            prompt = Text.assemble(
                ("💡 [热词] ", "yellow"),
                (sug, "white"),
            )
            options.append(Option(prompt, id=opt_id))

        results_list.set_options(options)

        login_tip = " | 提示: 按 l 扫码登录后解锁全站帖子搜索" if res.get("requires_login") else ""
        status_lbl.update(f"找到 {len(res.get('topics') or [])} 个板块, {len(res.get('posts') or [])} 条帖子{login_tip}")
        if options:
            results_list.highlighted = 0
            results_list.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        opt_id = event.option_id or ""
        entry = self._entries.get(opt_id)
        if not entry:
            return
        if entry["type"] == "suggestion":
            inp = self.query_one("#search-input", Input)
            inp.value = entry["query"]
            if self._search_task and not self._search_task.done():
                self._search_task.cancel()
            self._search_task = asyncio.create_task(self._perform_search(entry["query"]))
            return
        self.dismiss(entry)

    def action_close_modal(self) -> None:
        self.dismiss(None)


class SubCommentModal(ModalScreen[None]):
    """楼中楼完整回复查看弹窗."""

    BINDINGS = [
        Binding("escape", "close_modal", "关闭"),
        Binding("q", "close_modal", "关闭"),
        Binding("j", "cursor_down", "下移", show=False),
        Binding("k", "cursor_up", "上移", show=False),
        Binding("w", "reply_root", "回复该楼"),
        Binding("n", "load_more", "加载更多"),
    ]

    def __init__(self, client: HeyboxClient, link_id: str, thread: CommentThread) -> None:
        super().__init__()
        self.client = client
        self.link_id = link_id
        self.thread = thread
        self._page = 1
        self._has_more = True
        self._replies: list[CommentReply] = []

    def compose(self) -> ComposeResult:
        root = self.thread.root
        root_text = Text.assemble(
            (f"#{root.floor_num}楼 ", "bold cyan"),
            (f"@{root.username}", "bold yellow"),
            (f" (Lv.{root.user_level})" if root.user_level else "", "dim"),
            (f" · 👍{root.up}\n", "green"),
            (root.text or "[图片评论]", "white"),
        )
        with Vertical(id="subcomment-dialog"):
            yield Static(root_text, id="subcomment-root")
            yield OptionList(id="subcomment-list")
            yield Static("j/k: 滚动  |  n: 加载更多楼中楼  |  Esc/q: 关闭", id="subcomment-hint")

    def on_mount(self) -> None:
        self._page = 1
        self._replies = []
        asyncio.create_task(self._fetch_page(1))

    async def _fetch_page(self, page: int) -> None:
        lst = self.query_one("#subcomment-list", OptionList)
        hint = self.query_one("#subcomment-hint", Static)
        hint.update("⏳ 正在加载楼中楼...")
        try:
            res = await self.client.get_sub_comments(self.link_id, self.thread.root.commentid, page=page)
        except Exception as exc:
            hint.update(f"❌ 加载失败: {exc}  |  Esc: 关闭")
            return

        new_replies: list[CommentReply] = res["comments"]
        self._has_more = bool(res["has_more"])
        self._page = page
        if page == 1:
            self._replies = new_replies
        else:
            self._replies.extend(new_replies)

        options: list[Option] = []
        for r in self._replies:
            t = Text()
            t.append(f"@{r.username}", style="bold cyan")
            if r.user_level is not None:
                t.append(f" (Lv.{r.user_level})", style="dim")
            if r.reply_to_user:
                t.append(" 回复 ", style="dim")
                t.append(f"@{r.reply_to_user}", style="yellow")
            meta_parts: list[str] = []
            if r.ip_location:
                meta_parts.append(r.ip_location)
            tm = format_time(r.create_at)
            if tm:
                meta_parts.append(tm)
            meta_parts.append(f"👍{r.up}")
            t.append(f"  [{' · '.join(meta_parts)}]\n", style="dim")
            t.append(r.text or "[图片]", style="white")
            if r.imgs:
                t.append(f"  [🖼 x{len(r.imgs)}]", style="magenta")
            options.append(Option(t))

        if not options:
            options.append(Option(Text("暂无更多回复", style="dim")))

        lst.set_options(options)
        lst.focus()
        more_tip = "n: 加载下一页  |  " if self._has_more else "已加载全部  |  "
        hint.update(f"共 {len(self._replies)} 条回复  |  w: 回复该楼  |  {more_tip}j/k: 移动  |  Esc/q: 关闭")

    def action_cursor_down(self) -> None:
        self.query_one("#subcomment-list", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#subcomment-list", OptionList).action_cursor_up()

    async def action_load_more(self) -> None:
        if self._has_more:
            await self._fetch_page(self._page + 1)

    def action_reply_root(self) -> None:
        root = self.thread.root
        def _after(res: dict[str, Any] | None) -> None:
            if res is not None:
                asyncio.create_task(self._fetch_page(1))

        self.app.push_screen(
            CommentModal(
                self.client,
                link_id=self.link_id,
                root_id=root.commentid,
                reply_id=root.commentid,
                target_label=f"回复 #{root.floor_num}楼 @{root.username}",
            ),
            _after,
        )

    def action_close_modal(self) -> None:
        self.dismiss(None)


class ImagePreviewModal(ModalScreen[None]):
    """终端内嵌图片预览浮层（支持悬停触发、左右切换、macOS QuickLook 原图）."""

    BINDINGS = [
        Binding("escape", "close_modal", "关闭"),
        Binding("q", "close_modal", "关闭"),
        Binding("left", "prev_image", "上一张"),
        Binding("h", "prev_image", "上一张", show=False),
        Binding("p", "prev_image", "上一张", show=False),
        Binding("right", "next_image", "下一张"),
        Binding("l", "next_image", "下一张", show=False),
        Binding("n", "next_image", "下一张", show=False),
        Binding("space", "open_quicklook", "原图高清弹窗"),
        Binding("i", "open_quicklook", "原图高清弹窗", show=False),
        Binding("b", "open_browser", "浏览器打开"),
    ]

    def __init__(self, images: list[str], start_index: int = 0) -> None:
        super().__init__()
        self.images = images
        self.index = max(0, min(start_index, len(images) - 1)) if images else 0
        self._cached_paths: dict[int, Path] = {}
        self._load_task: asyncio.Task[None] | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="img-preview-dialog"):
            yield Static("", id="img-preview-title")
            yield Static("⏳ 正在加载图片...", id="img-preview-box")
            yield Static("←/→:切换  Space/i:高清原图弹窗  b:浏览器  Esc/点击外部:关闭", id="img-preview-hint")

    def on_mount(self) -> None:
        self._start_load()

    def on_unmount(self) -> None:
        if self._load_task and not self._load_task.done():
            self._load_task.cancel()

    def on_click(self, event: events.Click) -> None:
        if event.widget is self:
            self.dismiss(None)

    def _start_load(self) -> None:
        if self._load_task and not self._load_task.done():
            self._load_task.cancel()
        self._load_task = asyncio.create_task(self._load_current())

    async def _load_current(self) -> None:
        title_lbl = self.query_one("#img-preview-title", Static)
        box = self.query_one("#img-preview-box", Static)
        if not self.images:
            title_lbl.update("🖼 图片预览")
            box.update("暂无可预览的图片")
            return

        total = len(self.images)
        url = self.images[self.index]
        title_lbl.update(f"🖼 图片预览 ({self.index + 1}/{total})")
        box.update("⏳ 正在下载并解码图片...")

        try:
            path = self._cached_paths.get(self.index)
            if path is None:
                path = await fetch_image_to_cache(url)
                self._cached_paths[self.index] = path
            max_w = max(24, min(68, (self.app.size.width or 80) - 12))
            max_h = max(10, min(26, (self.app.size.height or 24) - 6))
            rendered = render_image_file_rich(path, max_width=max_w, max_height=max_h)
            box.update(rendered)
        except Exception as exc:
            box.update(f"❌ 图片加载失败: {exc}\n(按 b 可直接在浏览器打开原图)")

    def action_prev_image(self) -> None:
        if len(self.images) > 1:
            self.index = (self.index - 1) % len(self.images)
            self._start_load()

    def action_next_image(self) -> None:
        if len(self.images) > 1:
            self.index = (self.index + 1) % len(self.images)
            self._start_load()

    async def action_open_quicklook(self) -> None:
        if not self.images:
            return
        url = self.images[self.index]
        path = self._cached_paths.get(self.index)
        if path is None:
            try:
                path = await fetch_image_to_cache(url)
                self._cached_paths[self.index] = path
            except Exception:
                webbrowser.open(url)
                return
        open_image_quicklook(path)

    def action_open_browser(self) -> None:
        if self.images:
            webbrowser.open(self.images[self.index])

    def action_close_modal(self) -> None:
        self.dismiss(None)


class CommentModal(ModalScreen[dict[str, Any] | None]):
    """发表帖子评论 / 楼中楼回复弹窗."""

    BINDINGS = [
        Binding("escape", "close_modal", "取消"),
    ]

    def __init__(
        self,
        client: HeyboxClient,
        link_id: str,
        root_id: str = "-1",
        reply_id: str = "-1",
        target_label: str = "",
    ) -> None:
        super().__init__()
        self.client = client
        self.link_id = link_id
        self.root_id = root_id
        self.reply_id = reply_id
        self.target_label = target_label or f"评论帖子 #{link_id}"
        self._submitting = False

    def compose(self) -> ComposeResult:
        with Vertical(id="comment-dialog"):
            yield Static("💬 发表评论", id="comment-title")
            yield Static(self.target_label, id="comment-target")
            yield Input(placeholder="输入评论内容，按 Enter 发送（支持 [cube_滑稽] 等表情）...", id="comment-input")
            yield Static("Enter: 发送  |  Esc: 取消", id="comment-status")

    def on_mount(self) -> None:
        self.query_one("#comment-input", Input).focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._submitting:
            return
        text = event.value.strip()
        status = self.query_one("#comment-status", Static)
        if not text:
            status.update("⚠️ 评论内容不能为空")
            return
        self._submitting = True
        status.update("⏳ 正在发送评论...")
        try:
            res = await self.client.create_comment(
                link_id=self.link_id,
                text=text,
                root_id=self.root_id,
                reply_id=self.reply_id,
            )
        except Exception as exc:
            self._submitting = False
            status.update(f"❌ {exc}")
            return
        status.update("✅ 评论发送成功！")
        self.dismiss(res)

    def action_close_modal(self) -> None:
        self.dismiss(None)


class NewPostModal(ModalScreen[dict[str, Any] | None]):
    """发布新帖弹窗."""

    BINDINGS = [
        Binding("escape", "close_modal", "取消"),
        Binding("ctrl+s", "submit_post", "发布"),
    ]

    def __init__(self, client: HeyboxClient, default_topic: str = "盒友杂谈") -> None:
        super().__init__()
        self.client = client
        self.default_topic = default_topic or "盒友杂谈"
        self._submitting = False

    def compose(self) -> ComposeResult:
        with Vertical(id="newpost-dialog"):
            yield Static("📝 发布新帖 (Ctrl+S 提交 / Esc 取消)", id="newpost-title")
            yield Input(value=self.default_topic, placeholder="目标板块名或 topic_id（默认: 盒友杂谈）", id="post-topic-input")
            yield Input(placeholder="输入帖子标题...", id="post-title-input")
            yield TextArea(placeholder="输入帖子正文内容...", id="post-content-area")
            yield Static("提示: 发帖需先扫码登录 (按 l)", id="newpost-status")
            with Horizontal(id="newpost-actions"):
                yield Button("取消 (Esc)", id="btn-cancel")
                yield Button("发布 (Ctrl+S)", id="btn-publish", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#post-title-input", Input).focus()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
        elif event.button.id == "btn-publish":
            await self.action_submit_post()

    async def action_submit_post(self) -> None:
        if self._submitting:
            return
        topic_raw = self.query_one("#post-topic-input", Input).value.strip() or "盒友杂谈"
        title = self.query_one("#post-title-input", Input).value.strip()
        content = self.query_one("#post-content-area", TextArea).text.strip()
        status = self.query_one("#newpost-status", Static)

        if not title:
            status.update("⚠️ 请输入帖子标题")
            self.query_one("#post-title-input", Input).focus()
            return
        if not content:
            status.update("⚠️ 请输入帖子正文")
            self.query_one("#post-content-area", TextArea).focus()
            return

        self._submitting = True
        status.update("⏳ 正在解析板块并发布...")
        try:
            topic = await self.client.resolve_topic(topic_raw)
            topic_id = topic.topic_id if topic is not None else (topic_raw if topic_raw.isdigit() else "7214")
            res = await self.client.create_post(title=title, content=content, topic_id=topic_id)
        except Exception as exc:
            self._submitting = False
            status.update(f"❌ {exc}")
            return
        status.update("✅ 发帖成功！")
        self.dismiss(res)

    def action_close_modal(self) -> None:
        self.dismiss(None)


class BossScreen(ModalScreen[None]):
    """摸鱼老板键伪装画面."""

    BINDINGS = [
        Binding("grave_accent", "close_boss", "恢复"),
        Binding("escape", "close_boss", "恢复"),
    ]

    _FAKE_LOG = """[04:39:12] INFO  pipeline::scheduler - Initializing distributed worker pool (threads=16)
[04:39:13] INFO  core::storage - Mounted WAL segment /var/lib/cluster/wal_000842.log (checksum=ok)
[04:39:14] DEBUG rpc::handshake - Peer 10.24.18.9:8443 negotiated TLSv1.3 (ALPN=h2)
[04:39:15] INFO  compiler::codegen - Compiling module `engine/renderer/vulkan_pipeline.rs`...
[04:39:16] INFO  compiler::codegen - Optimization pass `licm` completed in 142.8ms (0 warnings)
[04:39:17] INFO  tests::integration - Running 128 unit tests across 14 crates...
[04:39:18] PASS  tests::auth::test_token_rotation (0.012s)
[04:39:18] PASS  tests::scheduler::test_backpressure_window (0.041s)
[04:39:19] PASS  tests::storage::test_snapshot_compaction (0.118s)
[04:39:20] INFO  metrics::exporter - Prometheus scrape endpoint listening on 127.0.0.1:9090
[04:39:21] INFO  deploy::verifier - Waiting for readiness probe on shard-03... [OK]

$ pytest -q tests/test_core_pipeline.py
........................................................................ [100%]
72 passed in 2.41s

(Press ` or Esc to resume workspace)"""

    def compose(self) -> ComposeResult:
        yield Static(self._FAKE_LOG, id="boss-screen-log")

    def action_close_boss(self) -> None:
        self.dismiss(None)
