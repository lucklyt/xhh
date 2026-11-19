"""数据模型与富文本 / HTML / 盒友表情清洗工具."""

from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup

CUBE_EMOJI_MAP: dict[str, str] = {
    "滑稽": "😏",
    "doge": "🐶",
    "菜doge": "🥬",
    "哭": "😭",
    "哭泣": "😭",
    "捂脸哭": "🤦",
    "笑哭": "😂",
    "点赞": "👍",
    "赞": "👍",
    "电牛": "⚡",
    "充能": "⚡",
    "喜欢": "❤️",
    "爱心": "❤️",
    "生气": "😠",
    "怒": "😡",
    "吃瓜": "🍉",
    "墨镜": "😎",
    "酷": "😎",
    "疑问": "❓",
    "问号": "❓",
    "思考": "🤔",
    "震惊": "😲",
    "坏笑": "😈",
    "流汗": "😅",
    "汗": "💦",
    "鼓掌": "👏",
    "抱拳": "🙏",
    "感谢": "🙏",
    "可怜": "🥺",
    "吐血": "🩸",
    "菜": "🥬",
    "炒菜": "🍳",
    "比心": "🫶",
    "加油": "💪",
    "ok": "👌",
    "OK": "👌",
    "拜拜": "👋",
    "再见": "👋",
    "害羞": "😊",
    "偷笑": "🤭",
    "斜眼": "😒",
    "无语": "😑",
    "发呆": "😶",
    "困": "😴",
    "打call": "📣",
}

_CUBE_RE = re.compile(r"\[cube_([^\]]+)\]")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_IMG_TAG_RE = re.compile(r"\[\s*🖼\s*图片\s*(\d+)\s*\]")
_VIDEO_TAG_RE = re.compile(r"\[\s*🎬\s*视频片段\s*\]")
_EMOJI_STRIP_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0000231A-\U000023FA"
    "\U00002B05-\U00002B55"
    "\U0000FE0E\U0000FE0F\U0000200D\U000020E3"
    "]+"
)


def strip_emojis(text: str) -> str:
    """在极简低调主题下移除彩色 Emoji，并将媒体占位符转为纯 ASCII 标签."""
    if not text:
        return ""
    cleaned = _IMG_TAG_RE.sub(r"[img:\1]", text)
    cleaned = _VIDEO_TAG_RE.sub("[video]", cleaned)
    cleaned = cleaned.replace("[🎬 该帖包含视频，阅读时可按 b 在浏览器中播放原帖]", "[video: 按 b 在浏览器播放]")
    cleaned = _EMOJI_STRIP_RE.sub("", cleaned)
    return cleaned.strip()


def translate_emojis(text: str) -> str:
    """将小黑盒 [cube_xxx] 表情转译为终端醒目的 Emoji 标签."""
    if not text:
        return ""

    def _repl(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        icon = CUBE_EMOJI_MAP.get(name)
        return f"{icon}[{name}]" if icon else f"[{name}]"

    return _CUBE_RE.sub(_repl, text)


def clean_inline_html(text: str) -> str:
    """清洗评论等短文本中的 HTML 标签（保留链接文字）并转译表情."""
    if not text:
        return ""
    if "<" in text and ">" in text:
        soup = BeautifulSoup(text, "html.parser")
        for br in soup.find_all("br"):
            br.replace_with("\n")
        cleaned = soup.get_text()
    else:
        cleaned = html.unescape(text)
    return translate_emojis(cleaned).strip()


def format_time(ts: int | float | str | None) -> str:
    """将 Unix 时间戳格式化为易读的相对时间或日期."""
    if ts is None or ts == "":
        return ""
    try:
        t_val = float(ts)
    except (TypeError, ValueError):
        return str(ts)
    if t_val <= 0:
        return ""
    # 兼容毫秒时间戳
    if t_val > 1e12:
        t_val /= 1000.0
    now = time.time()
    diff = now - t_val
    if 0 <= diff < 60:
        return "刚刚"
    if 60 <= diff < 3600:
        return f"{int(diff // 60)}分钟前"
    if 3600 <= diff < 86400:
        return f"{int(diff // 3600)}小时前"
    if 86400 <= diff < 86400 * 7:
        return f"{int(diff // 86400)}天前"
    dt = datetime.fromtimestamp(t_val)
    if dt.year == datetime.fromtimestamp(now).year:
        return dt.strftime("%m-%d %H:%M")
    return dt.strftime("%Y-%m-%d %H:%M")


def format_count(value: int | float | str | None) -> str:
    """格式化点赞/评论/热度数字."""
    if value is None or value == "":
        return "0"
    try:
        num = int(float(value))
    except (TypeError, ValueError):
        return str(value)
    if num >= 10000:
        return f"{num / 10000:.1f}w"
    if num >= 1000:
        return f"{num / 1000:.1f}k"
    return str(num)


def _extract_image_url(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in ("url", "image", "src", "thumb"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _append_unique_image(images: list[str], url: str) -> int:
    url = url.strip()
    if not url:
        return 0
    if url not in images:
        images.append(url)
    return images.index(url) + 1


def format_post_content(
    link_data: dict[str, Any], fallback_imgs: list[Any] | None = None
) -> tuple[str, list[str]]:
    """解析小黑盒帖子正文（JSON 块 / HTML / 纯文本）并提取图片 URL 列表."""
    images: list[str] = []
    body_parts: list[str] = []

    raw_text = link_data.get("text")
    parsed_blocks: list[Any] | None = None

    if isinstance(raw_text, list):
        parsed_blocks = raw_text
    elif isinstance(raw_text, str):
        stripped = raw_text.lstrip()
        if stripped.startswith("["):
            try:
                loaded = json.loads(stripped)
                if isinstance(loaded, list):
                    parsed_blocks = loaded
            except json.JSONDecodeError:
                parsed_blocks = None

    if parsed_blocks is not None:
        for block in parsed_blocks:
            if not isinstance(block, dict):
                if isinstance(block, str) and block.strip():
                    body_parts.append(block.strip())
                continue
            btype = str(block.get("type") or "").lower()
            if btype == "text":
                txt = str(block.get("text") or "")
                if txt:
                    body_parts.append(txt)
            elif btype in ("img", "image"):
                url = _extract_image_url(block)
                if url:
                    idx = _append_unique_image(images, url)
                    body_parts.append(f"[🖼 图片 {idx}]")
            elif btype == "video":
                body_parts.append("[🎬 视频片段]")
            else:
                txt = block.get("text") or block.get("title") or block.get("content")
                if isinstance(txt, str) and txt.strip():
                    body_parts.append(txt.strip())
    elif isinstance(raw_text, str) and raw_text.strip():
        if re.search(r"<(?:p|br|div|img|h[1-6]|blockquote|li|ul|ol|span|a)\b", raw_text, re.I):
            soup = BeautifulSoup(raw_text, "html.parser")
            for img in soup.find_all("img"):
                src = img.get("src") or img.get("data-src") or img.get("data-original") or ""
                if isinstance(src, str) and src.strip():
                    idx = _append_unique_image(images, src)
                    img.replace_with(f"\n[🖼 图片 {idx}]\n")
                else:
                    img.decompose()
            for br in soup.find_all("br"):
                br.replace_with("\n")
            for tag in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li"]):
                if tag.name == "li":
                    tag.insert_before("\n• ")
                else:
                    tag.insert_after("\n")
            body_parts.append(soup.get_text())
        else:
            body_parts.append(html.unescape(raw_text))

    for img_source in (link_data.get("imgs") or [], fallback_imgs or []):
        for item in img_source:
            url = _extract_image_url(item)
            if url:
                _append_unique_image(images, url)

    body = "\n".join(part.strip("\r") for part in body_parts if part is not None).strip()
    if not body:
        desc = str(link_data.get("description") or "").strip()
        if desc:
            body = html.unescape(desc)

    if link_data.get("has_video"):
        video_hint = "[🎬 该帖包含视频，阅读时可按 b 在浏览器中播放原帖]"
        body = f"{video_hint}\n\n{body}" if body else video_hint

    body = translate_emojis(body)
    body = _BLANK_LINES_RE.sub("\n\n", body).strip()
    return body, images


def parse_floor_range(spec: str) -> tuple[int, int | None]:
    """解析楼层范围字符串，如 '1-10'、'8'、'15-'、'-20'."""
    raw = spec.strip()
    if not raw:
        raise ValueError("楼层范围不能为空")

    if "-" not in raw:
        if not raw.isdigit() or int(raw) < 1:
            raise ValueError(f"无效的楼层号: {spec!r}（应为正整数或如 1-10 的区间）")
        val = int(raw)
        return val, val

    parts = raw.split("-")
    if len(parts) != 2:
        raise ValueError(f"无效的楼层范围: {spec!r}（示例: 1-10、5、15-）")

    left, right = parts[0].strip(), parts[1].strip()
    if not left and not right:
        raise ValueError(f"无效的楼层范围: {spec!r}")

    start = int(left) if left else 1
    if left and (not left.isdigit() or start < 1):
        raise ValueError(f"起始楼层必须为正整数: {spec!r}")

    if right:
        if not right.isdigit() or int(right) < 1:
            raise ValueError(f"结束楼层必须为正整数: {spec!r}")
        end: int | None = int(right)
        if end < start:
            raise ValueError(f"结束楼层不能小于起始楼层: {spec!r}")
    else:
        end = None

    return start, end


@dataclass
class TopicItem:
    """社区 / 游戏板块."""

    topic_id: int | str
    name: str
    app_id: int | str = -1
    hot_value: int = 0
    pic_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "name": self.name,
            "app_id": self.app_id,
            "hot_value": self.hot_value,
            "pic_url": self.pic_url,
        }

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> TopicItem:
        game = data.get("game") or {}
        hot = data.get("hot") or {}
        return cls(
            topic_id=data.get("topic_id") or data.get("id") or "",
            name=str(data.get("name") or "").strip(),
            app_id=data.get("app_id") or game.get("app_id") or -1,
            hot_value=int(hot.get("raw_hot_value") or data.get("hot_value_v2") or 0),
            pic_url=str(data.get("small_pic_url") or data.get("pic_url") or ""),
        )


@dataclass
class PostSummary:
    """帖子列表摘要."""

    linkid: str
    title: str
    description: str
    author: str
    author_level: int | None
    topic_name: str
    topic_id: str
    up: int
    award_num: int
    comment_num: int
    create_at: int | float
    create_str: str
    image_count: int
    has_video: bool
    is_hot_post: bool
    imgs: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> PostSummary:
        raw_user = data.get("user")
        user = raw_user if isinstance(raw_user, dict) else {}
        level_info = user.get("level_info") or {}
        topics = data.get("topics") or []
        first_topic = topics[0] if topics and isinstance(topics[0], dict) else {}
        imgs = [u for u in (_extract_image_url(i) for i in (data.get("imgs") or [])) if u]
        title = clean_inline_html(str(data.get("title") or ""))
        desc = clean_inline_html(str(data.get("description") or "")).replace("\n", " ").strip()
        if not title:
            title = desc[:60] or f"帖子 #{data.get('linkid', '')}"
        author = str(user.get("username") or ("匿名盒友" if raw_user is not None else ""))
        return cls(
            linkid=str(data.get("linkid") or ""),
            title=title,
            description=desc,
            author=author,
            author_level=level_info.get("level"),
            topic_name=str(first_topic.get("name") or "").strip(),
            topic_id=str(first_topic.get("topic_id") or ""),
            up=int(data.get("up") or 0),
            award_num=int(data.get("link_award_num") or 0),
            comment_num=int(data.get("comment_num") or 0),
            create_at=data.get("create_at") or data.get("modify_at") or 0,
            create_str=str(data.get("create_str") or data.get("formated_time") or ""),
            image_count=len(imgs),
            has_video=bool(data.get("has_video")),
            is_hot_post=bool(data.get("is_hot_post")),
            imgs=imgs,
            raw=data,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "linkid": self.linkid,
            "title": self.title,
            "description": self.description,
            "author": self.author,
            "author_level": self.author_level,
            "topic_name": self.topic_name,
            "topic_id": self.topic_id,
            "up": self.up,
            "award_num": self.award_num,
            "comment_num": self.comment_num,
            "create_at": self.create_at,
            "create_str": self.create_str,
            "image_count": self.image_count,
            "has_video": self.has_video,
            "is_hot_post": self.is_hot_post,
            "imgs": self.imgs,
        }


@dataclass
class CommentReply:
    """单条评论或楼中楼回复."""

    commentid: str
    username: str
    user_level: int | None
    ip_location: str
    text: str
    up: int
    create_at: int | float
    floor_num: int
    child_num: int
    is_cy: bool
    is_top: bool
    is_link_owner: bool
    reply_to_user: str
    imgs: list[str] = field(default_factory=list)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> CommentReply:
        user = data.get("user") or {}
        level_info = user.get("level_info") or {}
        reply_user = data.get("replyuser") or {}
        imgs = [u for u in (_extract_image_url(i) for i in (data.get("imgs") or [])) if u]
        return cls(
            commentid=str(data.get("commentid") or ""),
            username=str(user.get("username") or "匿名盒友"),
            user_level=level_info.get("level"),
            ip_location=str(data.get("ip_location") or ""),
            text=clean_inline_html(str(data.get("text") or "")),
            up=int(data.get("up") or 0),
            create_at=data.get("create_at") or 0,
            floor_num=int(data.get("floor_num") or 0),
            child_num=int(data.get("child_num") or 0),
            is_cy=bool(data.get("is_cy")),
            is_top=bool(data.get("is_top")),
            is_link_owner=bool(data.get("is_link_owner")),
            reply_to_user=str(reply_user.get("username") or ""),
            imgs=imgs,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "commentid": self.commentid,
            "floor_num": self.floor_num,
            "username": self.username,
            "user_level": self.user_level,
            "ip_location": self.ip_location,
            "text": self.text,
            "up": self.up,
            "create_at": self.create_at,
            "child_num": self.child_num,
            "is_cy": self.is_cy,
            "is_top": self.is_top,
            "is_link_owner": self.is_link_owner,
            "reply_to_user": self.reply_to_user,
            "imgs": self.imgs,
        }


@dataclass
class CommentThread:
    """主楼评论及其前排子评论预览."""

    root: CommentReply
    children: list[CommentReply] = field(default_factory=list)
    has_more: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = self.root.to_dict()
        data["has_more"] = self.has_more
        data["replies"] = [c.to_dict() for c in self.children]
        return data

    @classmethod
    def from_api_group(cls, group: dict[str, Any]) -> CommentThread | None:
        items = group.get("comment") or []
        if not items:
            return None
        root = CommentReply.from_api(items[0])
        children = [CommentReply.from_api(c) for c in items[1:]]
        has_more = bool(items[0].get("has_more")) or (root.child_num > len(children))
        return cls(root=root, children=children, has_more=has_more)


@dataclass
class PostDetail:
    """帖子详情与评论区分页数据."""

    linkid: str
    title: str
    body: str
    images: list[str]
    author: str
    author_level: int | None
    ip_location: str
    topic_name: str
    topic_id: str
    up: int
    award_num: int
    comment_num: int
    favour_count: int
    create_at: int | float
    share_url: str
    has_video: bool
    comments: list[CommentThread]
    has_more_floors: bool
    total_page: int
    total_floor_num: int

    @classmethod
    def from_api(
        cls, result: dict[str, Any], fallback_imgs: list[Any] | None = None
    ) -> PostDetail:
        lnk = result.get("link") or {}
        user = lnk.get("user") or {}
        level_info = user.get("level_info") or {}
        topics = lnk.get("topics") or []
        first_topic = topics[0] if topics and isinstance(topics[0], dict) else {}
        body, images = format_post_content(lnk, fallback_imgs=fallback_imgs)
        threads: list[CommentThread] = []
        for grp in result.get("comments") or []:
            if isinstance(grp, dict):
                thread = CommentThread.from_api_group(grp)
                if thread is not None:
                    threads.append(thread)
        linkid = str(lnk.get("linkid") or "")
        share_url = str(lnk.get("share_url") or f"https://www.xiaoheihe.cn/app/bbs/link/{linkid}")
        return cls(
            linkid=linkid,
            title=translate_emojis(str(lnk.get("title") or "").strip()) or f"帖子 #{linkid}",
            body=body,
            images=images,
            author=str(user.get("username") or "匿名盒友"),
            author_level=level_info.get("level"),
            ip_location=str(lnk.get("ip_location") or ""),
            topic_name=str(first_topic.get("name") or ""),
            topic_id=str(first_topic.get("topic_id") or ""),
            up=int(lnk.get("up") or 0),
            award_num=int(lnk.get("link_award_num") or 0),
            comment_num=int(lnk.get("comment_num") or 0),
            favour_count=int(lnk.get("favour_count") or 0),
            create_at=lnk.get("create_at") or lnk.get("modify_at") or 0,
            share_url=share_url,
            has_video=bool(lnk.get("has_video")),
            comments=threads,
            has_more_floors=bool(result.get("has_more_floors")),
            total_page=int(result.get("total_page") or 1),
            total_floor_num=int(result.get("total_floor_num") or 0),
        )

    def to_dict(self, comment_filter: dict[str, Any] | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "linkid": self.linkid,
            "title": self.title,
            "author": self.author,
            "author_level": self.author_level,
            "ip_location": self.ip_location,
            "topic_name": self.topic_name,
            "topic_id": self.topic_id,
            "create_at": self.create_at,
            "up": self.up,
            "award_num": self.award_num,
            "comment_num": self.comment_num,
            "favour_count": self.favour_count,
            "has_video": self.has_video,
            "share_url": self.share_url,
            "body": self.body,
            "images": self.images,
            "total_page": self.total_page,
            "total_floor_num": self.total_floor_num,
            "has_more_floors": self.has_more_floors,
            "comments": [c.to_dict() for c in self.comments],
        }
        if comment_filter is not None:
            out["comment_filter"] = comment_filter
        return out


@dataclass
class HotRankItem:
    """热搜榜 / 黑盒热议 / 热门游戏榜条目."""

    rank: int
    title: str
    tab_type: str
    tag: str = ""
    item_type: str = ""
    item_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "title": self.title,
            "tab_type": self.tab_type,
            "tag": self.tag,
            "item_type": self.item_type,
            "item_id": self.item_id,
        }
