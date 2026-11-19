"""终端 Unicode 半高块二维码渲染器."""

from __future__ import annotations

import qrcode
from rich.text import Text


def render_qr_half_blocks(data: str, border: int = 2) -> str:
    """使用 Unicode 半高块字符 (█/▀/▄/ ) 将字符串编码为紧凑终端二维码."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=1,
        border=border,
    )
    qr.add_data(data)
    qr.make(fit=True)
    matrix: list[list[bool]] = qr.get_matrix()

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


def render_qr_rich(data: str, border: int = 2) -> Text:
    """返回白底黑码高对比度的 Rich Text，兼容深浅终端且便于手机扫码."""
    ascii_qr = render_qr_half_blocks(data, border=border)
    return Text(ascii_qr, style="black on white")
