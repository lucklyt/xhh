"""终端二维码渲染器（纯 ASCII 等宽色块 + 紧凑半高块 + 浏览器 SVG 导出）."""

from __future__ import annotations

import html
import io
import tempfile
from pathlib import Path

import qrcode
from qrcode.image.svg import SvgPathImage
from rich.text import Text


def parse_qr_matrix(data: str, border: int = 1) -> list[list[bool]]:
    """将文本编码为二维码布尔矩阵."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=1,
        border=border,
    )
    qr.add_data(data)
    qr.make(fit=True)
    return qr.get_matrix()


def render_qr_half_blocks(data: str, border: int = 1) -> str:
    """使用 Unicode 半高块字符 (█/▀/▄/ ) 将字符串编码为紧凑终端二维码."""
    matrix = parse_qr_matrix(data, border=border)

    lines: list[str] = []
    rows = len(matrix)
    cols = len(matrix[0]) if rows else 0

    for r in range(0, rows, 2):
        row_top = matrix[r]
        row_bottom = matrix[r + 1] if (r + 1) < rows else [False] * cols
        chars: list[str] = []
        for c in range(cols):
            top = row_top[c]
            bottom = row_bottom[c]
            if top and bottom:
                chars.append("█")
            elif top and not bottom:
                chars.append("▀")
            elif not top and bottom:
                chars.append("▄")
            else:
                chars.append(" ")
        lines.append("".join(chars))

    return "\n".join(lines)


def render_qr_rich(data: str, border: int = 1, compact: bool = False) -> Text:
    """渲染高对比度终端二维码.

    默认 (compact=False) 使用纯 ASCII 双空格色块，规避各终端/字体下 Unicode
    方框字符东亚宽度不一致导致的横向变形问题。
    """
    if compact:
        ascii_qr = render_qr_half_blocks(data, border=border)
        return Text(ascii_qr, style="#000000 on #ffffff", no_wrap=True)

    matrix = parse_qr_matrix(data, border=border)
    result = Text(no_wrap=True)
    dark_style = "#000000 on #000000"
    light_style = "#ffffff on #ffffff"

    for row_idx, row in enumerate(matrix):
        if row_idx > 0:
            result.append("\n")
        if not row:
            continue
        # 合并连续同色块以减少 Rich segment 数量
        cur_val = row[0]
        run_len = 1
        for cell in row[1:]:
            if cell == cur_val:
                run_len += 1
            else:
                result.append("  " * run_len, style=dark_style if cur_val else light_style)
                cur_val = cell
                run_len = 1
        result.append("  " * run_len, style=dark_style if cur_val else light_style)

    return result


def save_qr_html(data: str) -> Path:
    """生成矢量 SVG 二维码本地 HTML 页面，供浏览器扫码备用."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(image_factory=SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    svg_str = buf.getvalue().decode("utf-8")

    escaped_url = html.escape(data)
    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <title>小黑盒 CLI 扫码登录</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      background: #0d1117;
      color: #e6edf3;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .card {{
      background: #ffffff;
      padding: 24px;
      border-radius: 14px;
      box-shadow: 0 10px 30px rgba(0,0,0,0.45);
    }}
    .card svg {{
      width: 260px;
      height: 260px;
      display: block;
    }}
    h2 {{ margin: 0 0 16px; font-size: 18px; }}
    p {{ margin: 16px 0 0; font-size: 12px; color: #8b949e; max-width: 420px; word-break: break-all; text-align: center; }}
  </style>
</head>
<body>
  <h2>请使用小黑盒 App 扫描下方二维码登录</h2>
  <div class="card">{svg_str}</div>
  <p>{escaped_url}</p>
</body>
</html>
"""
    out_path = Path(tempfile.gettempdir()) / "xhh_qr_login.html"
    out_path.write_text(page, encoding="utf-8")
    return out_path
