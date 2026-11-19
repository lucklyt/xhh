"""图片缓存、终端真彩色半高块渲染与 macOS QuickLook 原图预览."""

from __future__ import annotations

import hashlib
import io
import subprocess
import sys
import tempfile
import urllib.parse
import webbrowser
from pathlib import Path

import httpx
from PIL import Image
from rich.text import Text


def get_image_cache_dir() -> Path:
    """获取图片本地缓存目录."""
    cache_dir = Path(tempfile.gettempdir()) / "xhh_img_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _guess_extension(url: str, content_type: str = "") -> str:
    ct = content_type.lower()
    if "png" in ct:
        return ".png"
    if "webp" in ct:
        return ".webp"
    if "gif" in ct:
        return ".gif"
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    path = urllib.parse.urlparse(url).path.lower()
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        if path.endswith(ext):
            return ext
    return ".jpg"


async def fetch_image_to_cache(url: str, timeout: float = 10.0) -> Path:
    """异步下载图片到本地缓存并返回文件路径."""
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()
    cache_dir = get_image_cache_dir()
    existing = list(cache_dir.glob(f"{digest}.*"))
    for p in existing:
        if p.is_file() and p.stat().st_size > 0:
            return p

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.xiaoheihe.cn/",
    }
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        ext = _guess_extension(url, resp.headers.get("content-type", ""))
        out_path = cache_dir / f"{digest}{ext}"
        out_path.write_bytes(resp.content)
        return out_path


def render_image_bytes_rich(
    raw_bytes: bytes,
    max_width: int = 46,
    max_height: int = 18,
) -> Text:
    """使用全统一 `'▀'` 真彩色上下像素算法将图片字节渲染为 Rich Text."""
    with Image.open(io.BytesIO(raw_bytes)) as img:
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (13, 17, 23))
            bg.paste(rgba, mask=rgba.split()[3])
            rgb_img = bg
        else:
            rgb_img = img.convert("RGB")

        src_w, src_h = rgb_img.size
        max_w = max(4, max_width)
        max_px_h = max(4, max_height * 2)

        scale = min(max_w / max(1, src_w), max_px_h / max(1, src_h))
        target_w = max(2, int(round(src_w * scale)))
        target_px_h = max(2, int(round(src_h * scale)))
        if target_px_h % 2 == 1:
            target_px_h = max(2, target_px_h - 1 if target_px_h > 2 else target_px_h + 1)

        resized = rgb_img.resize((target_w, target_px_h), Image.Resampling.LANCZOS)
        pixels = resized.load()

    result = Text(no_wrap=True)
    for y in range(0, target_px_h, 2):
        if y > 0:
            result.append("\n")
        cur_style: str | None = None
        run_len = 0
        for x in range(target_w):
            r1, g1, b1 = pixels[x, y]
            if y + 1 < target_px_h:
                r2, g2, b2 = pixels[x, y + 1]
            else:
                r2, g2, b2 = r1, g1, b1
            st = f"#{r1:02x}{g1:02x}{b1:02x} on #{r2:02x}{g2:02x}{b2:02x}"
            if st == cur_style:
                run_len += 1
            else:
                if cur_style is not None:
                    result.append("▀" * run_len, style=cur_style)
                cur_style = st
                run_len = 1
        if cur_style is not None:
            result.append("▀" * run_len, style=cur_style)

    return result


def render_image_file_rich(
    path: Path,
    max_width: int = 46,
    max_height: int = 18,
) -> Text:
    """读取本地图片文件并渲染为终端内嵌真彩色图像."""
    return render_image_bytes_rich(path.read_bytes(), max_width=max_width, max_height=max_height)


def open_image_quicklook(path: Path) -> bool:
    """在 macOS 下使用 QuickLook 悬浮预览原图，其他平台用浏览器打开."""
    ql_bin = Path("/usr/bin/qlmanage")
    if sys.platform == "darwin" and ql_bin.exists():
        subprocess.Popen(
            [str(ql_bin), "-p", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    webbrowser.open(path.as_uri())
    return False
