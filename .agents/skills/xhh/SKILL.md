---
name: xhh
description: >-
  Interact with the XiaoHeiHe (小黑盒 / Heybox) gaming community via the `xhh` CLI
  in headless JSON mode. Activate when the user asks to browse 小黑盒 recommended feeds,
  hot rankings (热议/热搜/热门游戏), or game boards/topics; read a post body and
  specific comment floors; search posts or communities; check or trigger QR code login;
  publish image-text posts; or post comments and nested replies (楼中楼).
---

# XiaoHeiHe (`xhh`) Agent Skill

Use `xhh` subcommands with `--json` to read and write XiaoHeiHe (小黑盒) community data cleanly from the terminal without launching an interactive TUI.

## 1. Prerequisites & CLI Discovery

Resolve the `xhh` binary before running any command:

```bash
if command -v xhh >/dev/null 2>&1; then
  XHH_BIN="xhh"
elif [ -x "$HOME/.local/bin/xhh" ]; then
  XHH_BIN="$HOME/.local/bin/xhh"
elif [ -x "./xhh" ]; then
  XHH_BIN="./xhh"
else
  echo "xhh not found" >&2
fi
```

If `xhh` is not installed yet, install it once (creates an isolated virtual environment at `~/.local/share/xhh` and symlinks `~/.local/bin/xhh`):

```bash
curl -fsSL https://raw.githubusercontent.com/lucklyt/xhh/main/install.sh | bash
```

Or install via `uv`: `uv tool install git+https://github.com/lucklyt/xhh.git`.

## 2. Operating Rules for Agents

1. **Always use `--json`** on data and write commands (`feed`, `hot`, `topics`, `topic`, `post`, `search`, `comment`, `publish`) so output is structured and easy to parse.
2. **Never launch the interactive TUI** (`xhh` with no subcommand, or `xhh tui`) inside automated non-TTY agent execution unless the user explicitly asks to open the interactive UI.
3. **Authenticate safely**: Run `"$XHH_BIN" whoami` before write operations or full post search. Never read, print, or share raw credentials from `~/.config/xhh/session.json` (`pkey`, `cookies`).
4. **Never bypass security gates**: If login is missing/expired (`login` / `relogin`) or a risk-control check (`show_captcha`) triggers, stop and instruct the user to scan/verify themselves.
5. **Confirm before public writes**: Before running `publish` or `comment` publicly, confirm the target board/post, title, body/comment text, and image paths with the user unless the user already gave explicit instructions to publish immediately.
6. **Respect platform limits & verify writes**:
   - Post titles (`publish -t`) MUST be **2–30 characters**; longer titles fail validation.
   - Always follow every `publish` or `comment` with a readback check via `"$XHH_BIN" post <link_id> --floors ... --json` to confirm the live post, images, comment, or nested reply exists.

## 3. Core Workflows

### Workflow A: Check Auth & QR Login

```bash
"$XHH_BIN" whoami
```

If not logged in (exit code != 0), launch QR login with `--browser` so the user can scan either the terminal QR code or high-DPI SVG page:

```bash
"$XHH_BIN" login --browser
```

Wait for the user to scan with the XiaoHeiHe mobile app and confirm, then re-check `"$XHH_BIN" whoami`. To sign out: `"$XHH_BIN" logout`.

### Workflow B: Browse Feeds, Hot Rankings, Topics & Search

```bash
# Recommended feed (paginated with --offset)
"$XHH_BIN" feed -n 10 --offset 0 --json

# Rankings: all | news (黑盒热议) | fusion (热搜总榜) | gamev2 (热门游戏榜)
"$XHH_BIN" hot --tab all --json

# List or filter game/community boards (returns topic_id & name)
"$XHH_BIN" topics -q "CS2" --json

# Read posts inside a game/community board (accepts topic name or topic_id)
"$XHH_BIN" topic "CS2" --sort hot-rank -n 10 --json
"$XHH_BIN" topic "盒友杂谈" --hot --sort reply -n 10 --json

# Search boards and site-wide posts (full post search requires login)
"$XHH_BIN" search "黑神话" -n 10 --json
```

### Workflow C: Read a Post & Floor-Filtered Comments

If the user supplies a URL such as `https://www.xiaoheihe.cn/app/bbs/link/197309230`, extract the numeric `link_id` (`197309230`), then query:

```bash
# Read post body + images + default page 1 hot comments
"$XHH_BIN" post 197309230 --json

# Read specific comment floor range (defaults to --sort asc when --floors is set)
"$XHH_BIN" post 197309230 --floors 1-10 --json
"$XHH_BIN" post 197309230 --floors 8 --json
"$XHH_BIN" post 197309230 --floors 15- --max-pages 15 --json

# Author-only comments, or newest-first ordering
"$XHH_BIN" post 197309230 --owner --page 1 --json
"$XHH_BIN" post 197309230 --sort desc --page 1 --json
```

Use the `commentid` field on each item in `comments[]` or `comments[].replies[]` when replying to a specific comment.

### Workflow D: Publish an Image-Text Post

1. Check login (`"$XHH_BIN" whoami`).
2. Verify the board name or `topic_id` via `"$XHH_BIN" topics -q "<name>" --json` (defaults to `盒友杂谈` / `7214`).
3. Ensure title length is **2–30 characters**. Local image paths (`-i`) are automatically uploaded to XiaoHeiHe COS storage:

```bash
"$XHH_BIN" publish \
  --topic "盒友杂谈" \
  -t "不超过30字的帖子标题" \
  -c "正文内容..." \
  -i ./screenshot1.png \
  -i ./screenshot2.png \
  --json
```

4. Read back the returned `link_id` to confirm live visibility and uploaded images:

```bash
"$XHH_BIN" post <link_id> --json
```

### Workflow E: Comment on a Post

```bash
"$XHH_BIN" comment <link_id> "评论内容" --json
```

Read back with `"$XHH_BIN" post <link_id> --sort desc --page 1 --json` to confirm the new comment appears.

### Workflow F: Reply to a Comment (楼中楼)

1. Inspect the target post comments first to obtain exact IDs:

```bash
"$XHH_BIN" post <link_id> --floors 1-20 --json
```

2. Determine `--root-id` and `--reply-id`:
   - **Replying to a root floor comment**: pass the root comment's `commentid` to BOTH `--root-id` and `--reply-id`.
   - **Replying to a nested child reply inside `replies[]`**: pass the parent floor comment's `commentid` to `--root-id`, and the child reply's `commentid` to `--reply-id`.

```bash
"$XHH_BIN" comment <link_id> "回复内容" \
  --root-id "<root_commentid>" \
  --reply-id "<target_commentid>" \
  --json
```

3. Verify the reply appears under the root floor via `"$XHH_BIN" post <link_id> --floors <floor> --json`.

## 4. Reference

- See [CLI & JSON Schema Reference](./references/cli-reference.md) for every flag, exact JSON payload shape, and troubleshooting codes.
