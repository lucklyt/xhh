"""小黑盒社区异步 API 客户端."""

from __future__ import annotations

import urllib.parse
from typing import Any

import httpx

from xhh.config import AppConfig, SessionData
from xhh.hkey import generate_hkey_params
from xhh.models import (
    CommentReply,
    CommentThread,
    HotRankItem,
    PostDetail,
    PostSummary,
    TopicItem,
)

BASE_URL = "https://api.xiaoheihe.cn"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.xiaoheihe.cn/",
    "Origin": "https://www.xiaoheihe.cn",
    "Accept": "application/json, text/plain, */*",
}


class HeyboxAPIError(RuntimeError):
    """小黑盒 API 调用异常."""

    def __init__(self, message: str, status: str = "error", raw: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.raw = raw


class HeyboxClient:
    """小黑盒社区异步客户端，支持免登录浏览与扫码登录鉴权."""

    def __init__(self, session: SessionData | None = None, timeout: float = 12.0) -> None:
        self.session = session if session is not None else SessionData.load()
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers=DEFAULT_HEADERS,
            cookies=self.session.get_cookies(),
            timeout=timeout,
            follow_redirects=True,
        )
        self._topic_cache: list[TopicItem] = []

    async def __aenter__(self) -> HeyboxClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    def _sync_cookies_to_client(self) -> None:
        for k, v in self.session.get_cookies().items():
            self._client.cookies.set(k, v, domain=".xiaoheihe.cn")

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], httpx.Response]:
        query = generate_hkey_params(path)
        if params:
            for k, v in params.items():
                if v is not None:
                    query[k] = v

        resp = await self._client.request(method, path, params=query, data=data)
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict):
            raise HeyboxAPIError("接口返回格式异常", raw=payload)
        return payload, resp

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload, _ = await self._request("GET", path, params=params)
        return payload

    def _update_session_from_login_result(
        self, result: dict[str, Any], resp: httpx.Response | None = None
    ) -> SessionData:
        profile = result.get("profile") or {}
        account = result.get("account_detail") or {}
        level_info = account.get("level_info") or {}

        cookies_dict = dict(self.session.cookies)
        for jar in (self._client.cookies, resp.cookies if resp is not None else None):
            if jar is not None:
                for ck, cv in jar.items():
                    if cv and cv not in ("None", "none", "null", "undefined"):
                        cookies_dict[ck] = cv

        heybox_id = str(
            profile.get("heybox_id")
            or account.get("userid")
            or cookies_dict.get("heybox_id")
            or cookies_dict.get("user_heybox_id")
            or self.session.heybox_id
            or ""
        ).strip()
        pkey = str(
            result.get("pkey")
            or cookies_dict.get("pkey")
            or cookies_dict.get("user_pkey")
            or self.session.pkey
            or ""
        ).strip()
        nickname = str(
            profile.get("nickname")
            or account.get("username")
            or self.session.nickname
            or ""
        ).strip()
        avatar = str(
            profile.get("avatar")
            or account.get("avatar")
            or account.get("avartar")
            or self.session.avatar
            or ""
        ).strip()
        level = level_info.get("level")
        if level is None:
            level = self.session.level

        self.session = SessionData(
            heybox_id=heybox_id,
            pkey=pkey,
            nickname=nickname,
            avatar=avatar,
            level=level,
            cookies=cookies_dict,
        )
        if self.session.is_logged_in:
            self.session.save()
            self._sync_cookies_to_client()
        return self.session

    # -------------------- 扫码登录与账号管理 --------------------

    async def get_qrcode(self) -> dict[str, Any]:
        """获取扫码登录二维码 URL 与 token."""
        payload = await self._get("/account/get_qrcode_url/", {"app": "web"})
        if payload.get("status") != "ok":
            raise HeyboxAPIError(payload.get("msg") or "获取二维码失败", status=str(payload.get("status")), raw=payload)
        res = payload.get("result") or {}
        qr_url = str(res.get("qr_url") or "")
        if not qr_url:
            raise HeyboxAPIError("返回数据缺少 qr_url", raw=payload)
        parsed = urllib.parse.urlparse(qr_url)
        qr_token = (urllib.parse.parse_qs(parsed.query).get("qr") or [""])[0]
        if not qr_token:
            raise HeyboxAPIError("二维码 URL 缺少 qr 参数", raw=payload)
        return {
            "qr_url": qr_url,
            "qr_token": qr_token,
            "expire": int(res.get("expire") or 120),
        }

    async def poll_qr_state(self, qr_token: str) -> dict[str, Any]:
        """轮询一次扫码登录状态."""
        payload, resp = await self._request(
            "GET", "/account/qr_state/", params={"qr": qr_token, "app": "web"}
        )
        status = payload.get("status")
        if status == "need_google_check":
            return {
                "state": "requires_2fa",
                "msg": payload.get("msg") or "当前账号开启了谷歌两步验证，请先在网页端处理",
            }
        if status != "ok":
            return {
                "state": "error",
                "msg": payload.get("msg") or f"状态异常 ({status})",
            }

        res = payload.get("result") or {}
        err = res.get("error")
        if err == "wait":
            return {"state": "waiting", "msg": "等待小黑盒 App 扫码..."}
        if err == "ready":
            return {"state": "scanned", "msg": "已扫码，请在手机端点击确认登录"}
        if err == "ok":
            session = self._update_session_from_login_result(res, resp)
            if not session.is_logged_in:
                return {"state": "error", "msg": "登录成功但未返回有效凭证 (heybox_id/pkey)"}
            return {"state": "confirmed", "msg": "登录成功", "session": session}
        return {
            "state": "expired",
            "msg": res.get("error_msg") or payload.get("msg") or "二维码已失效",
        }

    async def restore_login(self) -> SessionData | None:
        """校验本地会话并同步最新用户信息."""
        if not self.session.is_logged_in:
            return None
        payload, resp = await self._request("GET", "/account/restore_login")
        status = payload.get("status")
        if status == "ok":
            res = payload.get("result") or {}
            session = self._update_session_from_login_result(res, resp)
            if session.is_logged_in:
                return session
        if status in ("login", "relogin"):
            return None
        return self.session if self.session.is_logged_in else None

    async def logout(self) -> None:
        """注销账号并清除本地会话."""
        if self.session.heybox_id:
            try:
                await self._get("/account/logout", {"heybox_id": self.session.heybox_id})
            except Exception:
                pass
        SessionData.clear()
        self.session = SessionData()
        self._client.cookies.clear()

    # -------------------- 社区浏览与板块接口 --------------------

    async def get_recommended_feeds(self, offset: int = 0) -> list[PostSummary]:
        """获取首页推荐帖子流."""
        payload = await self._get("/bbs/app/feeds", {"pull": 0, "offset": offset, "dw": 604})
        if payload.get("status") != "ok":
            raise HeyboxAPIError(payload.get("msg") or "获取推荐流失败", status=str(payload.get("status")), raw=payload)
        links = (payload.get("result") or {}).get("links") or []
        posts: list[PostSummary] = []
        for item in links:
            if not isinstance(item, dict):
                continue
            if item.get("content_type") == 18 or not item.get("linkid"):
                continue
            posts.append(PostSummary.from_api(item))
        return posts

    async def get_hot_rankings(self) -> dict[str, list[HotRankItem]]:
        """获取热搜总榜 / 黑盒热议 / 热门游戏榜."""
        payload = await self._get("/bbs/app/api/search/welcome_page/v2")
        if payload.get("status") != "ok":
            raise HeyboxAPIError(payload.get("msg") or "获取热榜失败", status=str(payload.get("status")), raw=payload)
        lists = (payload.get("result") or {}).get("Lists") or []
        grouped: dict[str, list[HotRankItem]] = {}
        for lst in lists:
            tab_type = str(lst.get("tab_type") or "")
            items: list[HotRankItem] = []
            for idx, entry in enumerate(lst.get("items") or [], start=1):
                if not isinstance(entry, dict):
                    continue
                report = entry.get("report") or {}
                right_tag = entry.get("right_tag") or {}
                items.append(
                    HotRankItem(
                        rank=idx,
                        title=str(entry.get("text") or "").strip(),
                        tab_type=tab_type,
                        tag=str(right_tag.get("text") or ""),
                        item_type=str(report.get("type") or ""),
                        item_id=str(report.get("id") or ""),
                        raw=entry,
                    )
                )
            grouped[tab_type] = items
        return grouped

    async def get_topic_categories(self) -> dict[str, list[TopicItem]]:
        """获取热门板块、已订阅板块与个性化推荐板块."""
        payload = await self._get("/bbs/app/topic/categories")
        if payload.get("status") != "ok":
            raise HeyboxAPIError(payload.get("msg") or "获取板块分类失败", status=str(payload.get("status")), raw=payload)
        res = payload.get("result") or {}

        def _parse_topics(raw_list: Any) -> list[TopicItem]:
            out: list[TopicItem] = []
            for item in raw_list or []:
                if isinstance(item, dict) and item.get("topic_id") and item.get("name"):
                    out.append(TopicItem.from_api(item))
            return out

        latest_hot = _parse_topics((res.get("latest_hot_topics") or {}).get("children"))
        subscribed = _parse_topics(res.get("subscribed_topics"))
        recommend = _parse_topics((res.get("recommend_for_user_topics") or {}).get("children"))

        seen: set[str] = set()
        cache: list[TopicItem] = []
        for t in [*subscribed, *latest_hot, *recommend]:
            tid = str(t.topic_id)
            if tid not in seen:
                seen.add(tid)
                cache.append(t)
        if cache:
            self._topic_cache = cache

        return {
            "latest_hot_topics": latest_hot,
            "subscribed_topics": subscribed,
            "recommend_topics": recommend,
        }

    async def get_topic_menu(
        self, topic_id: int | str | None = None, appid: int | str | None = None
    ) -> dict[str, Any]:
        """获取板块信息及二级分类菜单."""
        params: dict[str, Any] = {}
        if topic_id is not None and str(topic_id).strip():
            params["topic_id"] = topic_id
        if appid is not None and str(appid).strip():
            params["appid"] = appid
        payload = await self._get("/bbs/app/topic/menu", params)
        if payload.get("status") != "ok":
            raise HeyboxAPIError(payload.get("msg") or "获取板块信息失败", status=str(payload.get("status")), raw=payload)
        return payload.get("result") or {}

    async def resolve_topic(self, query: str) -> TopicItem | None:
        """将板块名称或数字 topic_id 解析为 TopicItem."""
        q = query.strip()
        if not q:
            return None
        if q.isdigit():
            try:
                menu = await self.get_topic_menu(topic_id=q)
                topic = menu.get("topic") or {}
                if topic.get("topic_id"):
                    return TopicItem.from_api(topic)
            except Exception:
                pass
            return TopicItem(topic_id=int(q), name=f"板块 #{q}")

        if not self._topic_cache:
            try:
                await self.get_topic_categories()
            except Exception:
                pass

        cfg = AppConfig.load()
        candidates: list[TopicItem] = list(self._topic_cache)
        seen = {str(t.topic_id) for t in candidates}
        for fav in cfg.favorite_topics:
            tid = str(fav.get("topic_id") or "")
            if tid and tid not in seen:
                candidates.append(TopicItem(topic_id=fav["topic_id"], name=str(fav.get("name") or "")))
                seen.add(tid)

        q_lower = q.lower()
        for t in candidates:
            if t.name.lower() == q_lower:
                return t
        for t in candidates:
            if q_lower in t.name.lower():
                return t

        # 尝试从热门游戏榜通过 appid 反查 topic_id
        try:
            rankings = await self.get_hot_rankings()
            for item in rankings.get("gamev2", []):
                if q_lower in item.title.lower() and item.item_id:
                    menu = await self.get_topic_menu(appid=item.item_id)
                    topic = menu.get("topic") or {}
                    if topic.get("topic_id"):
                        return TopicItem.from_api(topic)
        except Exception:
            pass
        return None

    async def get_topic_feeds(
        self,
        topic_id: int | str,
        is_hot: bool = False,
        sort_filter: str = "hot-rank",
        offset: int = 0,
        limit: int = 20,
        lastval: str = "",
    ) -> dict[str, Any]:
        """获取指定游戏/社区板块的帖子列表（支持 综合/热点 与排序切换）."""
        path = "/bbs/app/topic/feeds/news" if is_hot else "/bbs/app/topic/feeds"
        params: dict[str, Any] = {
            "topic_id": topic_id,
            "offset": offset,
            "limit": limit,
            "lastval": lastval,
            "sort_filter": sort_filter,
            "dw": 604,
        }
        if is_hot:
            params["cate_id"] = 10
        payload = await self._get(path, params)
        if payload.get("status") != "ok":
            raise HeyboxAPIError(payload.get("msg") or "获取板块帖子失败", status=str(payload.get("status")), raw=payload)
        res = payload.get("result") or {}
        links = res.get("links") or []
        posts = [
            PostSummary.from_api(item)
            for item in links
            if isinstance(item, dict) and item.get("linkid") and item.get("content_type") != 18
        ]
        return {
            "posts": posts,
            "lastval": str(res.get("lastval") or ""),
            "has_more": len(links) > 0,
            "filters": res.get("filters") or [],
            "sort_filter": res.get("sort_filter") or [],
        }

    # -------------------- 帖子详情与评论 --------------------

    async def get_post_detail(
        self,
        link_id: int | str,
        page: int = 1,
        limit: int = 20,
        owner_only: bool = False,
        sort_filter: str = "hot",
        fallback_imgs: list[Any] | None = None,
    ) -> PostDetail:
        """获取帖子正文与主楼评论列表."""
        params: dict[str, Any] = {
            "link_id": link_id,
            "page": page,
            "limit": limit,
            "is_first": 1 if page == 1 else 0,
            "index": 1,
            "owner_only": 1 if owner_only else 0,
            "sort_filter": sort_filter,
        }
        payload = await self._get("/bbs/app/link/tree", params)
        st = str(payload.get("status") or "")
        if st == "show_captcha":
            raise HeyboxAPIError(
                "触发小黑盒安全校验 (show_captcha)：未登录高频请求帖子详情会受限，建议先执行 `xhh login` 扫码登录或稍后重试",
                status="show_captcha",
                raw=payload,
            )
        if st != "ok":
            raise HeyboxAPIError(payload.get("msg") or "获取帖子详情失败", status=st, raw=payload)
        res = payload.get("result") or {}
        if not res.get("link"):
            raise HeyboxAPIError("帖子不存在或已删除", raw=payload)
        return PostDetail.from_api(res, fallback_imgs=fallback_imgs)

    async def get_post_detail_by_floors(
        self,
        link_id: int | str,
        floor_range: tuple[int, int | None],
        owner_only: bool = False,
        sort_filter: str = "time_aes",
        max_pages: int = 10,
    ) -> PostDetail:
        """获取指定楼层范围 [start, end] 的帖子详情与过滤后评论."""
        start, end = floor_range
        limit = 20
        base_detail = await self.get_post_detail(
            link_id=link_id,
            page=1,
            limit=limit,
            owner_only=owner_only,
            sort_filter=sort_filter,
        )

        collected: dict[str, CommentThread] = {}
        visited_pages: set[int] = {1}

        def _record(threads: list[CommentThread]) -> tuple[int, int]:
            fls: list[int] = []
            for th in threads:
                cid = th.root.commentid or f"floor:{th.root.floor_num}:{th.root.username}"
                collected[cid] = th
                if th.root.floor_num > 0:
                    fls.append(th.root.floor_num)
            return (min(fls), max(fls)) if fls else (0, 0)

        _, max_floor = _record(base_detail.comments)
        total_pages = max(1, base_detail.total_page)
        pages_fetched = 1

        if total_pages > 1 and max_pages > 1:
            if sort_filter == "time_aes":
                need_more = end is None or max_floor < end
                if need_more:
                    if start > 1 and max_floor < start:
                        probe = min(total_pages, max(2, (start - 1) // limit + 1))
                        while probe >= 2 and pages_fetched < max_pages:
                            if probe in visited_pages:
                                break
                            visited_pages.add(probe)
                            d = await self.get_post_detail(
                                link_id=link_id,
                                page=probe,
                                limit=limit,
                                owner_only=owner_only,
                                sort_filter=sort_filter,
                            )
                            pages_fetched += 1
                            p_min, p_max = _record(d.comments)
                            if p_max > max_floor:
                                max_floor = p_max
                            if p_min <= start or probe == 2:
                                cur_page = probe
                                break
                            probe -= 1
                        else:
                            cur_page = probe
                    else:
                        cur_page = 1

                    while cur_page < total_pages and pages_fetched < max_pages:
                        if end is not None and max_floor >= end:
                            break
                        cur_page += 1
                        if cur_page in visited_pages:
                            continue
                        visited_pages.add(cur_page)
                        d = await self.get_post_detail(
                            link_id=link_id,
                            page=cur_page,
                            limit=limit,
                            owner_only=owner_only,
                            sort_filter=sort_filter,
                        )
                        pages_fetched += 1
                        _, p_max = _record(d.comments)
                        if p_max > max_floor:
                            max_floor = p_max
                        if not d.comments:
                            break
            else:
                cur_page = 1
                while cur_page < total_pages and pages_fetched < max_pages:
                    if end is not None:
                        matched_cnt = sum(
                            1 for t in collected.values() if start <= t.root.floor_num <= end
                        )
                        if matched_cnt >= (end - start + 1):
                            break
                    cur_page += 1
                    visited_pages.add(cur_page)
                    d = await self.get_post_detail(
                        link_id=link_id,
                        page=cur_page,
                        limit=limit,
                        owner_only=owner_only,
                        sort_filter=sort_filter,
                    )
                    pages_fetched += 1
                    _record(d.comments)
                    if not d.comments:
                        break

        filtered = [
            th
            for th in collected.values()
            if th.root.floor_num >= start and (end is None or th.root.floor_num <= end)
        ]
        if sort_filter == "time_aes":
            filtered.sort(key=lambda t: t.root.floor_num)

        base_detail.comments = filtered
        return base_detail

    async def get_sub_comments(
        self,
        link_id: int | str,
        root_comment_id: int | str,
        page: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        """获取单条主评下的完整楼中楼回复."""
        payload = await self._get(
            "/bbs/app/comment/sub/comments",
            {
                "link_id": link_id,
                "root_comment_id": root_comment_id,
                "page": page,
                "limit": limit,
            },
        )
        if payload.get("status") != "ok":
            raise HeyboxAPIError(payload.get("msg") or "获取楼中楼失败", status=str(payload.get("status")), raw=payload)
        res = payload.get("result") or {}
        replies = [
            CommentReply.from_api(c)
            for c in (res.get("comments") or [])
            if isinstance(c, dict)
        ]
        return {
            "comments": replies,
            "has_more": bool(res.get("has_more")),
            "lastval": str(res.get("lastval") or ""),
        }

    # -------------------- 搜索 --------------------

    async def search(
        self,
        query: str,
        search_type: str = "general",
        offset: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        """搜索游戏板块、搜索建议及全站帖子（全站帖子接口需登录态）."""
        q = query.strip()
        if not self._topic_cache:
            try:
                await self.get_topic_categories()
            except Exception:
                pass

        q_lower = q.lower()
        topic_matches = [
            t for t in self._topic_cache if q_lower in t.name.lower() or q == str(t.topic_id)
        ]

        suggestions: list[str] = []
        try:
            sug_payload = await self._get("/bbs/app/api/search/suggestion/v2", {"q": q})
            if sug_payload.get("status") == "ok":
                for s in (sug_payload.get("result") or {}).get("suggestions") or []:
                    txt = str(s.get("text") or "").strip()
                    if txt and txt not in suggestions:
                        suggestions.append(txt)
        except Exception:
            pass

        posts: list[PostSummary] = []
        requires_login = False
        try:
            res_payload = await self._get(
                "/bbs/app/api/general/search/v1",
                {
                    "q": q,
                    "search_type": search_type,
                    "offset": offset,
                    "limit": limit,
                    "is_pull_down": 0,
                    "dw": 604,
                },
            )
            st = res_payload.get("status")
            if st in ("login", "relogin"):
                requires_login = True
            elif st == "ok":
                items = (res_payload.get("result") or {}).get("items") or []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    info = item.get("info") if isinstance(item.get("info"), dict) else item
                    if isinstance(info, dict) and info.get("linkid"):
                        posts.append(PostSummary.from_api(info))
                    elif isinstance(info, dict) and info.get("topic_id") and info.get("name"):
                        t_obj = TopicItem.from_api(info)
                        if all(str(x.topic_id) != str(t_obj.topic_id) for x in topic_matches):
                            topic_matches.insert(0, t_obj)
        except Exception:
            pass

        return {
            "topics": topic_matches,
            "suggestions": suggestions,
            "posts": posts,
            "requires_login": requires_login,
        }
