"""小黑盒 CLI 单元测试与集成测试."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import ContentSwitcher, OptionList, Static

from xhh.client import HeyboxClient
from xhh.config import AppConfig, SessionData
from xhh.hkey import compute_hkey, generate_hkey_params
from xhh.models import (
    CommentReply,
    CommentThread,
    HotRankItem,
    PostDetail,
    PostSummary,
    TopicItem,
    clean_inline_html,
    format_count,
    format_post_content,
    translate_emojis,
)
from xhh.qr import render_qr_half_blocks, render_qr_rich
from xhh.tui.app import HeyboxApp
from xhh.tui.modals import BossScreen, QRLoginModal, SearchModal, SubCommentModal


def test_hkey_computation_and_params() -> None:
    h = compute_hkey("/bbs/app/feeds", 1700000001, "0123456789ABCDEF0123456789ABCDEF")
    assert len(h) == 7
    assert h[-2:].isdigit()

    # 路径带首尾斜杠和不带首尾斜杠应当一致
    h2 = compute_hkey("bbs/app/feeds/", 1700000001, "0123456789ABCDEF0123456789ABCDEF")
    assert h == h2

    params = generate_hkey_params("/bbs/app/link/tree", timestamp=1700000000)
    assert params["_time"] == 1700000000
    assert params["version"] == "999.0.4"
    assert len(str(params["nonce"])) == 32
    assert len(str(params["hkey"])) == 7


def test_qr_rendering() -> None:
    qr_text = render_qr_half_blocks("https://api.xiaoheihe.cn/account/qr_login/?qr=test&app=web", border=1)
    lines = qr_text.splitlines()
    assert len(lines) > 10
    widths = {len(line) for line in lines}
    assert len(widths) == 1
    assert any(ch in qr_text for ch in ("█", "▀", "▄"))

    rich_qr = render_qr_rich("https://example.com")
    assert rich_qr.style == "black on white"


def test_config_and_session_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XHH_CONFIG_DIR", str(tmp_path))

    sess = SessionData(heybox_id="12345", pkey="secret_pkey", nickname="测试盒友", level=18)
    sess.save()
    loaded = SessionData.load()
    assert loaded.is_logged_in
    assert loaded.nickname == "测试盒友"
    cookies = loaded.get_cookies()
    assert cookies["heybox_id"] == "12345"
    assert cookies["user_pkey"] == "secret_pkey"

    SessionData.clear()
    assert not SessionData.load().is_logged_in

    cfg = AppConfig.load()
    assert len(cfg.favorite_topics) >= 1
    added = cfg.toggle_favorite(733575, "影之刃零")
    assert added is True
    assert AppConfig.load().is_favorite(733575)
    removed = cfg.toggle_favorite(733575, "影之刃零")
    assert removed is False
    assert not AppConfig.load().is_favorite(733575)


def test_models_emoji_and_content_parsing() -> None:
    assert translate_emojis("你好[cube_滑稽][cube_未知]") == "你好😏[滑稽][未知]"
    assert clean_inline_html('回复<a href="heybox://abc">@小明</a><br>测试[cube_点赞]') == "回复@小明\n测试👍[点赞]"
    assert format_count(1520) == "1.5k"
    assert format_count(24500) == "2.5w"

    json_blocks = json.dumps(
        [
            {"type": "text", "text": "第一段内容[cube_doge]"},
            {"type": "img", "url": "https://img.example.com/1.png"},
        ],
        ensure_ascii=False,
    )
    body, imgs = format_post_content({"text": json_blocks})
    assert "第一段内容🐶[doge]" in body
    assert "[🖼 图片 1]" in body
    assert imgs == ["https://img.example.com/1.png"]

    html_text = '<p>HTML段落<br>第二行</p><img src="https://img.example.com/2.jpg"><p>结尾</p>'
    body2, imgs2 = format_post_content({"text": html_text})
    assert "HTML段落\n第二行" in body2
    assert "[🖼 图片 1]" in body2
    assert imgs2 == ["https://img.example.com/2.jpg"]


def test_live_api_end_to_end() -> None:
    async def _run() -> None:
        async with HeyboxClient() as client:
            # 1. 推荐流
            feeds = await client.get_recommended_feeds()
            assert len(feeds) > 0
            first_post = feeds[0]
            assert first_post.linkid

            # 2. 热榜
            rankings = await client.get_hot_rankings()
            assert len(rankings.get("news") or []) > 0
            assert len(rankings.get("fusion") or []) > 0
            assert len(rankings.get("gamev2") or []) > 0

            # 3. 热门板块分类与板块名解析
            cats = await client.get_topic_categories()
            assert len(cats["latest_hot_topics"]) >= 10
            cs2 = await client.resolve_topic("CS2")
            assert cs2 is not None
            assert str(cs2.topic_id) == "43"

            # 4. 指定游戏板块（综合 & 热点）
            topic_all = await client.get_topic_feeds(43, is_hot=False, sort_filter="hot-rank", limit=5)
            assert len(topic_all["posts"]) > 0
            topic_hot = await client.get_topic_feeds(43, is_hot=True, sort_filter="reply", limit=5)
            assert len(topic_hot["posts"]) > 0

            # 5. 帖子详情与评论
            detail = await client.get_post_detail(first_post.linkid, limit=10)
            assert detail.linkid == first_post.linkid
            assert detail.title

            # 6. 二维码登录获取与状态轮询
            qr = await client.get_qrcode()
            assert "qr_url" in qr and qr["qr_token"]
            poll = await client.poll_qr_state(qr["qr_token"])
            assert poll["state"] in ("waiting", "scanned", "expired")

    asyncio.run(_run())


class _FakeClient:
    """供 TUI 自动化测试使用的确定性 Mock 客户端."""

    def __init__(self) -> None:
        self.session = SessionData()

    async def close(self) -> None:
        return None

    async def restore_login(self) -> SessionData | None:
        return None

    async def get_topic_categories(self) -> dict[str, list[TopicItem]]:
        return {
            "latest_hot_topics": [
                TopicItem(topic_id=43, name="CS2", app_id=730),
                TopicItem(topic_id=568547, name="黑神话：悟空", app_id=2358720),
            ],
            "subscribed_topics": [],
            "recommend_topics": [],
        }

    async def get_recommended_feeds(self, offset: int = 0) -> list[PostSummary]:
        return [
            PostSummary(
                linkid="10001",
                title="测试推荐帖子标题",
                description="这是一段测试摘要",
                author="测试盒友",
                author_level=16,
                topic_name="CS2",
                topic_id="43",
                up=120,
                award_num=15,
                comment_num=32,
                create_at=1794800000,
                create_str="10分钟前",
                image_count=1,
                has_video=False,
                is_hot_post=True,
                imgs=["https://img.example.com/a.jpg"],
            )
        ]

    async def get_hot_rankings(self) -> dict[str, list[HotRankItem]]:
        return {
            "news": [HotRankItem(rank=1, title="测试热议事件", tab_type="news", tag="爆")],
            "fusion": [HotRankItem(rank=1, title="黑神话：悟空", tab_type="fusion", tag="热")],
            "gamev2": [HotRankItem(rank=1, title="CS2", tab_type="gamev2", item_type="game", item_id="730")],
        }

    async def get_topic_menu(self, topic_id: Any = None, appid: Any = None) -> dict[str, Any]:
        return {"topic": {"topic_id": 43, "name": "CS2"}}

    async def resolve_topic(self, query: str) -> TopicItem | None:
        if "cs2" in query.lower():
            return TopicItem(topic_id=43, name="CS2", app_id=730)
        return None

    async def get_topic_feeds(
        self,
        topic_id: Any,
        is_hot: bool = False,
        sort_filter: str = "hot-rank",
        offset: int = 0,
        limit: int = 20,
        lastval: str = "",
    ) -> dict[str, Any]:
        tag = "热点" if is_hot else "综合"
        return {
            "posts": [
                PostSummary(
                    linkid="20001",
                    title=f"CS2 {tag}帖子 ({sort_filter})",
                    description="CS2 板块内容",
                    author="茄子",
                    author_level=20,
                    topic_name="CS2",
                    topic_id=str(topic_id),
                    up=50,
                    award_num=3,
                    comment_num=8,
                    create_at=1794800000,
                    create_str="刚刚",
                    image_count=0,
                    has_video=False,
                    is_hot_post=is_hot,
                )
            ],
            "lastval": "",
            "has_more": False,
        }

    async def get_post_detail(
        self,
        link_id: Any,
        page: int = 1,
        limit: int = 20,
        owner_only: bool = False,
        sort_filter: str = "hot",
        fallback_imgs: Any = None,
    ) -> PostDetail:
        root_reply = CommentReply(
            commentid="9001",
            username="神评大王",
            user_level=25,
            ip_location="广东",
            text="太真实了😏[滑稽]",
            up=99,
            create_at=1794801000,
            floor_num=1,
            child_num=2,
            is_cy=True,
            is_top=True,
            is_link_owner=False,
            reply_to_user="",
        )
        return PostDetail(
            linkid=str(link_id),
            title="测试帖子完整标题",
            body="这是完整的帖子正文内容。",
            images=["https://img.example.com/a.jpg"],
            author="测试盒友",
            author_level=16,
            ip_location="北京",
            topic_name="CS2",
            topic_id="43",
            up=120,
            award_num=15,
            comment_num=32,
            favour_count=9,
            create_at=1794800000,
            share_url="https://www.xiaoheihe.cn/app/bbs/link/10001",
            has_video=False,
            comments=[CommentThread(root=root_reply, children=[], has_more=True)],
            has_more_floors=False,
            total_page=1,
            total_floor_num=1,
        )

    async def get_sub_comments(self, link_id: Any, root_comment_id: Any, page: int = 1, limit: int = 20) -> dict[str, Any]:
        return {
            "comments": [
                CommentReply(
                    commentid="9002",
                    username="楼中楼盒友",
                    user_level=12,
                    ip_location="浙江",
                    text="确实如此",
                    up=5,
                    create_at=1794802000,
                    floor_num=0,
                    child_num=0,
                    is_cy=False,
                    is_top=False,
                    is_link_owner=True,
                    reply_to_user="神评大王",
                )
            ],
            "has_more": False,
            "lastval": "",
        }

    async def get_qrcode(self) -> dict[str, Any]:
        return {"qr_url": "https://api.xiaoheihe.cn/account/qr_login/?qr=mocktoken&app=web", "qr_token": "mocktoken", "expire": 120}

    async def poll_qr_state(self, qr_token: str) -> dict[str, Any]:
        return {"state": "waiting", "msg": "等待小黑盒 App 扫码..."}

    async def search(self, query: str, search_type: str = "general", offset: int = 0, limit: int = 20) -> dict[str, Any]:
        return {
            "topics": [TopicItem(topic_id=43, name="CS2", app_id=730)],
            "suggestions": ["CS2 Major"],
            "posts": [],
            "requires_login": True,
        }


def test_tui_interactive_flows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XHH_CONFIG_DIR", str(tmp_path))

    async def _run() -> None:
        fake_client = _FakeClient()
        app = HeyboxApp(client=fake_client)  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause(0.2)

            # 1. 验证初始加载推荐流
            feed_list = app.query_one("#feed-list", OptionList)
            assert feed_list.option_count == 1

            # 2. 按 Enter 进入帖子详情视图
            await pilot.press("enter")
            await pilot.pause(0.2)
            switcher = app.query_one("#content-switcher", ContentSwitcher)
            assert switcher.current == "detail-pane"
            assert "测试帖子完整标题" in str(app.query_one("#detail-header", Static).render())

            # 3. 切焦点到评论区并按 e 弹出楼中楼弹窗
            await pilot.press("c")
            await pilot.press("e")
            await pilot.pause(0.2)
            assert isinstance(app.screen, SubCommentModal)
            await pilot.press("escape")
            await pilot.pause(0.1)

            # 4. 返回帖子列表
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert switcher.current == "list-pane"

            # 5. 弹出扫码登录弹窗并关闭
            await pilot.press("l")
            await pilot.pause(0.2)
            assert isinstance(app.screen, QRLoginModal)
            await pilot.press("escape")
            await pilot.pause(0.1)

            # 6. 弹出搜索弹窗并关闭
            await pilot.press("/")
            await pilot.pause(0.1)
            assert isinstance(app.screen, SearchModal)
            await pilot.press("escape")
            await pilot.pause(0.1)

            # 7. 触发老板键伪装屏并恢复
            await pilot.press("`")
            await pilot.pause(0.1)
            assert isinstance(app.screen, BossScreen)
            await pilot.press("escape")
            await pilot.pause(0.1)

            # 8. 切换板块并测试 1/2/s/f 快捷键
            sidebar = app.query_one("#sidebar-list", OptionList)
            cs2_idx = None
            for idx, opt in enumerate(sidebar.options):
                if opt.id and opt.id.endswith(":43"):
                    cs2_idx = idx
                    break
            assert cs2_idx is not None
            sidebar.focus()
            sidebar.highlighted = cs2_idx
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert app._view_mode == "topic"

            await pilot.press("2")
            await pilot.pause(0.2)
            assert app._topic_is_hot is True

            await pilot.press("s")
            await pilot.pause(0.2)
            assert app._topic_sort_idx == 1

    asyncio.run(_run())
