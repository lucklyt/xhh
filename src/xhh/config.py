"""配置与登录会话持久化管理 (~/.config/xhh)."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_THEME = "stealth"
VALID_THEMES: tuple[str, str] = ("elegant", "stealth")

DEFAULT_FAVORITE_TOPICS: list[dict[str, Any]] = [
    {"topic_id": 7214, "name": "盒友杂谈"},
    {"topic_id": 425422, "name": "Steam"},
    {"topic_id": 43, "name": "CS2"},
    {"topic_id": 568547, "name": "黑神话：悟空"},
]


def get_config_dir() -> Path:
    """获取配置目录路径，支持 XHH_CONFIG_DIR 环境变量覆盖."""
    override = os.environ.get("XHH_CONFIG_DIR")
    base = Path(override) if override else Path.home() / ".config" / "xhh"
    base.mkdir(parents=True, exist_ok=True)
    return base


@dataclass
class SessionData:
    """用户登录会话信息."""

    heybox_id: str = ""
    pkey: str = ""
    nickname: str = ""
    avatar: str = ""
    level: int | None = None
    cookies: dict[str, str] = field(default_factory=dict)
    updated_at: float = 0.0

    @property
    def is_logged_in(self) -> bool:
        return bool(self.heybox_id and self.pkey)

    def get_cookies(self) -> dict[str, str]:
        jar = dict(self.cookies)
        if self.heybox_id:
            jar.setdefault("heybox_id", self.heybox_id)
            jar["user_heybox_id"] = self.heybox_id
        if self.pkey:
            jar.setdefault("pkey", self.pkey)
            jar["user_pkey"] = self.pkey
        return jar

    def save(self) -> Path:
        self.updated_at = time.time()
        path = get_config_dir() / "session.json"
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path

    @classmethod
    def load(cls) -> SessionData:
        path = get_config_dir() / "session.json"
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        return cls(
            heybox_id=str(raw.get("heybox_id") or ""),
            pkey=str(raw.get("pkey") or ""),
            nickname=str(raw.get("nickname") or ""),
            avatar=str(raw.get("avatar") or ""),
            level=raw.get("level"),
            cookies=dict(raw.get("cookies") or {}),
            updated_at=float(raw.get("updated_at") or 0.0),
        )

    @classmethod
    def clear(cls) -> None:
        path = get_config_dir() / "session.json"
        if path.exists():
            path.unlink()


@dataclass
class AppConfig:
    """用户偏好配置（收藏的游戏板块、TUI 主题等）."""

    favorite_topics: list[dict[str, Any]] = field(
        default_factory=lambda: [dict(item) for item in DEFAULT_FAVORITE_TOPICS]
    )
    theme: str = DEFAULT_THEME

    def save(self) -> Path:
        path = get_config_dir() / "config.json"
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls) -> AppConfig:
        path = get_config_dir() / "config.json"
        if not path.exists():
            cfg = cls()
            cfg.save()
            return cfg
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        favs = raw.get("favorite_topics")
        if not isinstance(favs, list):
            favs = [dict(item) for item in DEFAULT_FAVORITE_TOPICS]
        raw_theme = str(raw.get("theme") or DEFAULT_THEME).strip().lower()
        theme = raw_theme if raw_theme in VALID_THEMES else DEFAULT_THEME
        return cls(favorite_topics=favs, theme=theme)

    def set_theme(self, theme: str) -> str:
        norm = theme.strip().lower()
        self.theme = norm if norm in VALID_THEMES else DEFAULT_THEME
        self.save()
        return self.theme

    def is_favorite(self, topic_id: int | str) -> bool:
        tid = str(topic_id)
        return any(str(t.get("topic_id")) == tid for t in self.favorite_topics)

    def toggle_favorite(self, topic_id: int | str, name: str) -> bool:
        """切换收藏状态，返回切换后是否处于已收藏状态."""
        tid = str(topic_id)
        for idx, item in enumerate(self.favorite_topics):
            if str(item.get("topic_id")) == tid:
                self.favorite_topics.pop(idx)
                self.save()
                return False
        self.favorite_topics.insert(0, {"topic_id": int(tid) if tid.isdigit() else tid, "name": name})
        self.save()
        return True
