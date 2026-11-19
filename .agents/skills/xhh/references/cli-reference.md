# `xhh` CLI & JSON Schema Reference

All subcommands listed below match `src/xhh/cli.py` and `src/xhh/models.py`.

## Command & Flag Matrix

| Command | Aliases | Arguments & Flags | Notes |
| :--- | :--- | :--- | :--- |
| `xhh` | `xhh tui` | `--theme {stealth,elegant}` | Interactive full-screen TUI. Skip during headless agent tasks. |
| `xhh login` | — | `--dry-run`, `--compact`, `--large`, `--browser` | Generates App QR code; saves session with `0600` perms to `~/.config/xhh/session.json`. |
| `xhh whoami` | — | — | Validates local session with `/account/restore_login`. Exit `0` if valid, `1` otherwise. |
| `xhh logout` | — | — | Calls remote logout and removes local `session.json`. |
| `xhh feed` | — | `-n/--limit INT` (10), `--offset INT` (0), `-j/--json` | Recommended feed items. |
| `xhh hot` | — | `--tab {all,news,fusion,gamev2}` (`all`), `-j/--json` | Hot discussion (`news`), search ranking (`fusion`), popular games (`gamev2`). |
| `xhh topics` | — | `-q/--query STR`, `-j/--json` | Subscribed topics + hot game/community topics (`topic_id`, `name`). |
| `xhh topic` | — | `target`, `--hot`, `--sort {hot-rank,reply,create}` (`hot-rank`), `-n/--limit INT` (10), `-j/--json` | Feed for a specific game/community board (`target` = name or numeric `topic_id`). |
| `xhh post` | `view`, `--post` | `link_id`, `-f/--floors/--floor RANGE`, `--sort {hot,asc,desc}`, `--owner`, `--page INT` (1), `--max-pages INT` (10), `-j/--json` | Fetches post detail + comments. With `--floors`, `--sort` defaults to `asc`. Floor ranges accept `10`, `1-10`, `15-`. |
| `xhh search` | — | `query`, `-n/--limit INT` (10), `-j/--json` | Matching game boards + site-wide posts (`requires_login: true` when logged out). |
| `xhh comment` | — | `link_id`, `text`, `--root-id ID` (`-1`), `--reply-id ID` (`-1`), `-j/--json` | Requires login. Top-level comment uses defaults `-1`; reply sets `--root-id` & `--reply-id`. |
| `xhh publish` | `new-post` | `-t/--title STR` (req, <=30 chars), `-c/--content STR` (req), `--topic STR` (`盒友杂谈`), `-i/--image PATH_OR_URL` (repeatable), `-j/--json` | Requires login. Local images upload automatically via Tencent COS 4-step pipeline. |
| `xhh edit` | `update` | `link_id`, `-t/--title`, `-c/--content`, `--content-file PATH`, `-i/--image` (repeatable, replaces all images), `--topic`, `-j/--json` | Requires login and ownership. Unspecified fields keep original values; at least one change required. Output: `{status, link_id, share_url, msg, result}`. |

## JSON Output Shapes

### `xhh feed --json`

```json
{
  "offset": 0,
  "count": 10,
  "posts": [
    {
      "linkid": "197309230",
      "title": "...",
      "description": "...",
      "author": "...",
      "author_level": 5,
      "topic_name": "盒友杂谈",
      "topic_id": "7214",
      "up": 0,
      "award_num": 0,
      "comment_num": 2,
      "create_at": 1795089271,
      "create_str": "10分钟前",
      "image_count": 6,
      "has_video": false,
      "is_hot_post": false,
      "imgs": ["https://..."]
    }
  ]
}
```

### `xhh hot --json`

Map keyed by tab (`news`, `fusion`, `gamev2`):

```json
{
  "news": [
    {
      "rank": 1,
      "title": "...",
      "tab_type": "news",
      "tag": "爆",
      "item_type": "...",
      "item_id": "..."
    }
  ]
}
```

### `xhh topics --json`

```json
{
  "subscribed_topics": [],
  "latest_hot_topics": [
    {
      "topic_id": 43,
      "name": "CS2",
      "app_id": 730,
      "hot_value": "...",
      "pic_url": "https://..."
    }
  ]
}
```

### `xhh topic <target> --json`

```json
{
  "topic": { "topic_id": 7214, "name": "盒友杂谈", "app_id": -1, "hot_value": "", "pic_url": "" },
  "is_hot": false,
  "sort": "hot-rank",
  "posts": [ "..." ]
}
```

### `xhh post <link_id> --json`

```json
{
  "linkid": "197309230",
  "title": "...",
  "author": "ly",
  "author_level": 5,
  "ip_location": "北京",
  "topic_name": "盒友杂谈",
  "topic_id": "7214",
  "create_at": 1795089271,
  "up": 0,
  "award_num": 0,
  "comment_num": 2,
  "favour_count": 0,
  "has_video": false,
  "share_url": "https://...",
  "body": "...",
  "images": ["https://..."],
  "total_page": 1,
  "total_floor_num": 1,
  "has_more_floors": false,
  "comments": [
    {
      "commentid": "1004726662",
      "floor_num": 2,
      "username": "ly",
      "user_level": null,
      "ip_location": "北京",
      "text": "...",
      "up": 0,
      "create_at": 1795090033,
      "child_num": 1,
      "is_cy": false,
      "is_top": false,
      "is_link_owner": true,
      "reply_to_user": "",
      "imgs": [],
      "has_more": false,
      "replies": [
        {
          "commentid": "1004726917",
          "floor_num": 0,
          "username": "ly",
          "user_level": null,
          "ip_location": "北京",
          "text": "...",
          "up": 0,
          "create_at": 1795090057,
          "child_num": 0,
          "is_cy": false,
          "is_top": false,
          "is_link_owner": true,
          "reply_to_user": "ly",
          "imgs": []
        }
      ]
    }
  ],
  "comment_filter": {
    "sort": "asc",
    "owner_only": false,
    "page": null,
    "floors": { "raw": "1-5", "start": 1, "end": 5 }
  }
}
```

### `xhh search <query> --json`

```json
{
  "query": "黑神话",
  "topics": [ "..." ],
  "posts": [ "..." ],
  "suggestions": ["..."],
  "requires_login": true
}
```

### `xhh comment <link_id> "<text>" --json`

```json
{
  "status": "ok",
  "msg": "评论成功",
  "result": {
    "comment": {
      "comment": [
        { "commentid": 1004726662, "floor_num": 2, "text": "..." }
      ]
    }
  }
}
```

### `xhh publish ... --json`

```json
{
  "status": "ok",
  "link_id": "197309230",
  "share_url": "https://www.xiaoheihe.cn/app/bbs/link/197309230",
  "topic_id": "7214",
  "topic_name": "盒友杂谈",
  "msg": "发布成功",
  "result": {}
}
```

## Errors & Configuration

- **JSON error shape**: `{"error": "<message>"}` with non-zero exit code.
- **`login` / `relogin`**: Local session is missing or expired. Prompt the user to run `xhh login --browser`.
- **`show_captcha`**: Risk control triggered. Stop and ask the user to complete verification manually in browser/app before retrying.
- **`帖子标题最多 30 字`**: XiaoHeiHe image-text posts accept at most 30 characters in `-t/--title`. Shorten and re-run.
- **Topic not resolved**: Look up exact board ID first with `xhh topics -q "<keyword>" --json` or `xhh search "<keyword>" --json`, then pass `--topic <topic_id>`.
- **Session / config isolation**: Override directory via `XHH_CONFIG_DIR` (defaults to `~/.config/xhh`). Keep `session.json` private (`0600`).
