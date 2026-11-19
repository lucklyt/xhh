"""小黑盒 CLI 单元测试与集成测试."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from textual.widgets import ContentSwitcher, OptionList, Static

from xhh.cli import main as cli_main
from xhh.client import HeyboxAPIError, HeyboxClient
from xhh.config import AppConfig, SessionData
from xhh.hkey import compute_hkey, generate_hkey_params
from xhh.image import render_image_bytes_rich
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
    parse_floor_range,
    translate_emojis,
)
from xhh.qr import render_qr_half_blocks, render_qr_rich, save_qr_html
from xhh.tui.app import FEED_PAGE_SIZE, HeyboxApp, ImagePill
from xhh.tui.modals import (
    BossScreen,
    CommentModal,
    ImagePreviewModal,
    NewPostModal,
    QRLoginModal,
    SearchModal,
    SubCommentModal,
)


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
    url = "https://api.xiaoheihe.cn/account/qr_login/?qr=test&app=web"
    qr_text = render_qr_half_blocks(url, border=1)
    lines = qr_text.splitlines()
    assert len(lines) > 10
    widths = {len(line) for line in lines}
    assert len(widths) == 1
    assert any(ch in qr_text for ch in ("█", "▀", "▄"))

    # 默认紧凑模式 (compact=True) 应全部由统一字符 '▀' 与换行组成，零空格混排，高度约 19~20 行
    compact_qr = render_qr_rich(url, border=1)
    assert set(compact_qr.plain) <= {"▀", "\n"}
    compact_lines = compact_qr.plain.splitlines()
    assert 18 <= len(compact_lines) <= 22
    assert len({len(line) for line in compact_lines}) == 1

    # 双倍尺寸模式 (compact=False) 应全部由纯 ASCII 空格与换行组成，每行宽度严格一致
    rich_qr = render_qr_rich(url, border=1, compact=False)
    assert set(rich_qr.plain) <= {" ", "\n"}
    rich_lines = rich_qr.plain.splitlines()
    assert len(rich_lines) >= 21
    assert len({len(line) for line in rich_lines}) == 1

    # SVG HTML 导出
    html_path = save_qr_html(url)
    assert html_path.exists()
    content = html_path.read_text(encoding="utf-8")
    assert "<svg" in content and "qr_login" in content


def test_image_rendering() -> None:
    img = Image.new("RGB", (40, 20), (255, 100, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    rendered = render_image_bytes_rich(buf.getvalue(), max_width=20, max_height=10)
    assert set(rendered.plain) <= {"▀", "\n"}
    lines = rendered.plain.splitlines()
    assert len(lines) == 5  # 20x10 px -> 5 rows of half-blocks
    assert len(lines[0]) == 20
    assert any("#ff641e" in str(span.style) for span in rendered.spans)


def test_client_create_comment_and_post(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        # 1. 未登录时应当拦截
        anon = HeyboxClient(session=SessionData())
        async with anon:
            with pytest.raises(HeyboxAPIError) as exc1:
                await anon.create_comment("12345", "你好")
            assert exc1.value.status == "login"
            with pytest.raises(HeyboxAPIError) as exc2:
                await anon.create_post("标题", "正文")
            assert exc2.value.status == "login"

        # 2. 已登录时校验表单与返回解析
        authed = HeyboxClient(session=SessionData(heybox_id="1001", pkey="pk_test", nickname="tester"))
        captured: list[tuple[str, dict[str, Any]]] = []

        async def _fake_post(path: str, data: dict[str, Any], params: dict[str, Any] | None = None) -> dict[str, Any]:
            captured.append((path, data))
            if path == "/bbs/app/comment/create":
                return {"status": "ok", "msg": "评论成功", "result": {"comment": [{"commentid": 777}]}}
            return {"status": "ok", "msg": "发布成功", "link_id": "999888", "result": {"link_id": "999888"}}

        monkeypatch.setattr(authed, "_post", _fake_post)
        async with authed:
            c_res = await authed.create_comment("12345", "测试评论", root_id="88", reply_id="99")
            assert c_res["status"] == "ok"
            assert captured[0][0] == "/bbs/app/comment/create"
            assert captured[0][1]["link_id"] == "12345"
            assert captured[0][1]["root_id"] == "88"
            assert captured[0][1]["reply_id"] == "99"

            p_res = await authed.create_post("测试标题", "测试正文内容", topic_id="43")
            assert p_res["status"] == "ok"
            assert p_res["link_id"] == "999888"
            assert captured[1][0] == "/bbs/app/api/link/post"
            assert captured[1][1]["topic_ids"] == "43"
            assert json.loads(captured[1][1]["text"]) == [{"type": "text", "text": "测试正文内容"}]

    asyncio.run(_run())


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


def test_parse_floor_range() -> None:
    assert parse_floor_range("1-10") == (1, 10)
    assert parse_floor_range("8") == (8, 8)
    assert parse_floor_range("15-") == (15, None)
    assert parse_floor_range("-20") == (1, 20)
    for bad in ("", "0", "10-5", "abc", "1-2-3", "-"):
        with pytest.raises(ValueError):
            parse_floor_range(bad)


def test_get_post_detail_by_floors_and_captcha_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        async with HeyboxClient() as client:
            async def _fake_get_pages(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
                assert path == "/bbs/app/link/tree"
                page = int((params or {}).get("page") or 1)
                if page == 1:
                    floors = [1, 2, 3]
                elif page == 2:
                    floors = [4, 5, 6]
                else:
                    floors = []
                return {
                    "status": "ok",
                    "result": {
                        "link": {
                            "linkid": 197278200,
                            "title": "测试分页帖子",
                            "text": "正文内容",
                            "user": {"username": "楼主"},
                        },
                        "comments": [
                            {
                                "comment": [
                                    {
                                        "commentid": 8000 + f,
                                        "floor_num": f,
                                        "text": f"第{f}楼",
                                        "up": f,
                                        "user": {"username": f"用户{f}"},
                                    }
                                ]
                            }
                            for f in floors
                        ],
                        "total_page": 2,
                        "total_floor_num": 6,
                        "has_more_floors": page == 1,
                    },
                }

            monkeypatch.setattr(client, "_get", _fake_get_pages)
            ranged = await client.get_post_detail_by_floors(
                "197278200", floor_range=(2, 5), sort_filter="time_aes", max_pages=5
            )
            assert [th.root.floor_num for th in ranged.comments] == [2, 3, 4, 5]

            async def _fake_get_captcha(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
                return {"msg": "", "result": {"appid": 2061805035}, "status": "show_captcha"}

            monkeypatch.setattr(client, "_get", _fake_get_captcha)
            with pytest.raises(HeyboxAPIError) as exc_info:
                await client.get_post_detail("197278200")
            assert exc_info.value.status == "show_captcha"

    asyncio.run(_run())


def test_live_api_end_to_end() -> None:
    async def _run() -> None:
        async with HeyboxClient() as client:
            # 1. 推荐流
            feeds = await client.get_recommended_feeds()
            assert len(feeds) > 0
            assert feeds[0].linkid

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

            # 5. 二维码登录获取与状态轮询
            qr = await client.get_qrcode()
            assert "qr_url" in qr and qr["qr_token"]
            poll = await client.poll_qr_state(qr["qr_token"])
            assert poll["state"] in ("waiting", "scanned", "expired")

    asyncio.run(_run())


def test_cli_post_floors_and_json(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("xhh.cli.HeyboxClient", lambda *a, **kw: _FakeClient())

    rc = cli_main(["post", "197278200", "--floors", "2-5", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["linkid"] == "197278200"
    assert data["comment_filter"]["floors"] == {"raw": "2-5", "start": 2, "end": 5}
    assert [c["floor_num"] for c in data["comments"]] == [2, 3, 4, 5]

    rc2 = cli_main(["--post", "197278200", "--floors", "3", "--json"])
    assert rc2 == 0
    data2 = json.loads(capsys.readouterr().out)
    assert [c["floor_num"] for c in data2["comments"]] == [3]

    rc3 = cli_main(["view", "197278200", "--floors", "4-6", "--json"])
    assert rc3 == 0
    data3 = json.loads(capsys.readouterr().out)
    assert [c["floor_num"] for c in data3["comments"]] == [4, 5, 6]


def test_cli_comment_and_publish_json(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("xhh.cli.HeyboxClient", lambda *a, **kw: _FakeClient())

    rc = cli_main(["comment", "197278200", "Cli评论测试", "--json"])
    assert rc == 0
    c_data = json.loads(capsys.readouterr().out)
    assert c_data["status"] == "ok"

    rc2 = cli_main(["publish", "-t", "Cli发帖标题", "-c", "Cli发帖正文", "--topic", "CS2", "--json"])
    assert rc2 == 0
    p_data = json.loads(capsys.readouterr().out)
    assert p_data["status"] == "ok"
    assert p_data["topic_id"] == "43"
    assert p_data["link_id"] == "30001"


class _FakeClient:
    """供 TUI 与 CLI 自动化测试使用的确定性 Mock 客户端."""

    def __init__(self) -> None:
        self.session = SessionData()

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

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
        if offset > 0:
            return []
        return [
            PostSummary(
                linkid=str(10001 + i),
                title=f"测试推荐帖子标题 #{i + 1}",
                description=f"这是第 {i + 1} 条测试摘要",
                author="测试盒友",
                author_level=16,
                topic_name="CS2",
                topic_id="43",
                up=120 + i,
                award_num=15,
                comment_num=32,
                create_at=1794800000,
                create_str="10分钟前",
                image_count=1,
                has_video=False,
                is_hot_post=(i == 0),
                imgs=["https://img.example.com/a.jpg"],
            )
            for i in range(25)
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
        threads = [
            CommentThread(
                root=CommentReply(
                    commentid=str(9000 + i),
                    username=f"神评大王{i}" if i == 1 else f"评论盒友{i}",
                    user_level=20 + (i % 10),
                    ip_location="广东",
                    text=f"第 {i} 条评论：太真实了😏[滑稽]",
                    up=100 - i,
                    create_at=1794801000 + i,
                    floor_num=i,
                    child_num=2 if i == 1 else 0,
                    is_cy=(i == 1),
                    is_top=(i == 1),
                    is_link_owner=False,
                    reply_to_user="",
                ),
                children=[],
                has_more=(i == 1),
            )
            for i in range(1, 16)
        ]
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
            comment_num=15,
            favour_count=9,
            create_at=1794800000,
            share_url="https://www.xiaoheihe.cn/app/bbs/link/10001",
            has_video=False,
            comments=threads,
            has_more_floors=False,
            total_page=1,
            total_floor_num=15,
        )

    async def get_post_detail_by_floors(
        self,
        link_id: Any,
        floor_range: tuple[int, int | None],
        owner_only: bool = False,
        sort_filter: str = "time_aes",
        max_pages: int = 10,
    ) -> PostDetail:
        start, end = floor_range
        threads = [
            CommentThread(
                root=CommentReply(
                    commentid=str(9000 + f),
                    username=f"盒友{f}楼",
                    user_level=10 + f,
                    ip_location="上海",
                    text=f"第 {f} 楼评论内容",
                    up=f * 3,
                    create_at=1794801000 + f,
                    floor_num=f,
                    child_num=0,
                    is_cy=(f == 3),
                    is_top=False,
                    is_link_owner=(f == 2),
                    reply_to_user="",
                )
            )
            for f in range(1, 9)
            if f >= start and (end is None or f <= end)
        ]
        detail = await self.get_post_detail(link_id=link_id, owner_only=owner_only, sort_filter=sort_filter)
        detail.comments = threads
        detail.total_floor_num = 8
        return detail

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

    async def create_comment(
        self, link_id: Any, text: str, root_id: Any = "-1", reply_id: Any = "-1"
    ) -> dict[str, Any]:
        return {"status": "ok", "msg": "评论成功", "result": {"comment": [{"commentid": "9999"}]}}

    async def create_post(
        self, title: str, content: str, topic_id: Any = "7214", imgs: Any = None
    ) -> dict[str, Any]:
        return {"status": "ok", "link_id": "30001", "msg": "发布成功", "result": {"link_id": "30001"}}


def test_tui_interactive_flows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XHH_CONFIG_DIR", str(tmp_path))

    async def _run() -> None:
        fake_client = _FakeClient()
        app = HeyboxApp(client=fake_client)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.2)

            # 1. 标准 80x24 窗口下默认固定每页 10 条，一屏完整展示无滚动
            feed_list = app.query_one("#feed-list", OptionList)
            assert feed_list.option_count == 10
            assert app._posts[0].linkid == "10001"
            assert feed_list.virtual_size.height <= feed_list.size.height

            # 1b. 按 n 翻到第 2 页（10 条），第 3 页（5 条），按 p 返回第 1 页
            await pilot.press("n")
            await pilot.pause(0.2)
            assert app._feed_page == 2
            assert feed_list.option_count == 10
            assert app._posts[0].linkid == "10011"

            await pilot.press("n")
            await pilot.pause(0.2)
            assert app._feed_page == 3
            assert feed_list.option_count == 5
            assert app._posts[0].linkid == "10021"

            await pilot.press("p")
            await pilot.pause(0.1)
            await pilot.press("p")
            await pilot.pause(0.1)
            assert app._feed_page == 1
            assert feed_list.option_count == 10
            assert app._posts[0].linkid == "10001"

            # 2. 按 Enter 进入帖子详情视图：默认只显示正文，不显示评论，且存在可悬停图片胶囊
            await pilot.press("enter")
            await pilot.pause(0.2)
            switcher = app.query_one("#content-switcher", ContentSwitcher)
            sub_switcher = app.query_one("#detail-sub-switcher", ContentSwitcher)
            assert switcher.current == "detail-pane"
            assert sub_switcher.current == "detail-body-view"
            assert "测试帖子完整标题" in str(app.query_one("#detail-header", Static).render())

            pills = app.query(ImagePill)
            assert len(pills) == 1
            # 模拟鼠标悬停触发图片预览浮层并关闭
            pills.first()._trigger_hover()
            await pilot.pause(0.1)
            assert isinstance(app.screen, ImagePreviewModal)
            await pilot.press("escape")
            await pilot.pause(0.1)

            # 按 i 也可直接打开图片预览浮层，按 m 打开评论弹窗
            await pilot.press("i")
            await pilot.pause(0.1)
            assert isinstance(app.screen, ImagePreviewModal)
            await pilot.press("escape")
            await pilot.pause(0.1)

            await pilot.press("m")
            await pilot.pause(0.1)
            assert isinstance(app.screen, CommentModal)
            await pilot.press("escape")
            await pilot.pause(0.1)

            # 3. 按 c 切换到评论视图：每页固定 10 条无滚动，按 n 翻到第 2 页（5 条），按 e 弹出楼中楼弹窗
            await pilot.press("c")
            await pilot.pause(0.1)
            assert sub_switcher.current == "detail-comments-view"
            cmt_list = app.query_one("#comment-list", OptionList)
            assert cmt_list.option_count == 10
            assert cmt_list.virtual_size.height <= cmt_list.size.height

            await pilot.press("n")
            await pilot.pause(0.1)
            assert app._comment_page == 2
            assert cmt_list.option_count == 5
            await pilot.press("p")
            await pilot.pause(0.1)
            assert app._comment_page == 1
            assert cmt_list.option_count == 10

            await pilot.press("e")
            await pilot.pause(0.2)
            assert isinstance(app.screen, SubCommentModal)
            await pilot.press("escape")
            await pilot.pause(0.1)

            # 再按 c 切回正文视图
            await pilot.press("c")
            await pilot.pause(0.1)
            assert sub_switcher.current == "detail-body-view"

            # 4. 返回帖子列表
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert switcher.current == "list-pane"

            # 5. 80x24 标准窗口下弹出扫码登录弹窗，二维码无需滚动即可完整展示
            await pilot.press("l")
            await pilot.pause(0.2)
            assert isinstance(app.screen, QRLoginModal)
            qr_scroll = app.screen.query_one("#qr-scroll")
            assert qr_scroll.virtual_size.height <= qr_scroll.size.height
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

            # 7b. 按 P (Shift+p) 弹出发帖弹窗并关闭
            await pilot.press("P")
            await pilot.pause(0.1)
            assert isinstance(app.screen, NewPostModal)
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


def test_tui_adaptive_window_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XHH_CONFIG_DIR", str(tmp_path))

    async def _run() -> None:
        app = HeyboxApp(client=_FakeClient())  # type: ignore[arg-type]
        # 较大窗口 (100x34) 下自动根据高度容纳更多条数 (31 // 2 = 15 条)
        async with app.run_test(size=(100, 34)) as pilot:
            await pilot.pause(0.2)
            feed_list = app.query_one("#feed-list", OptionList)
            assert feed_list.option_count == 15
            assert feed_list.virtual_size.height <= feed_list.size.height

    asyncio.run(_run())
