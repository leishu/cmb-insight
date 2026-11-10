"""零依赖 PNG 柱状图渲染器（仅标准库）。

为了在无字体环境下也能出带标注的图，内置了一套 5x7 点阵字模，
只覆盖数字与 '-' '.' ','——足够画坐标轴与金额标签。
"""

from __future__ import annotations

import struct
import zlib

FONT = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11111", "00010", "00100", "00010", "00001", "10001", "01110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    ",": ["00000", "00000", "00000", "00000", "01100", "01100", "00100"],
    " ": ["00000", "00000", "00000", "00000", "00000", "00000", "00000"],
}

BG = (29, 29, 32)
GRID = (58, 58, 64)
BAR = (143, 136, 224)
BAR_NEG = (226, 104, 106)
AXIS = (150, 150, 158)


class Canvas:
    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.buf = bytearray(BG * (w * h))

    def rect(self, x: int, y: int, w: int, h: int, color) -> None:
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.w, x + w), min(self.h, y + h)
        if x1 <= x0 or y1 <= y0:
            return
        px = bytes(color)
        row = px * (x1 - x0)
        for yy in range(y0, y1):
            off = (yy * self.w + x0) * 3
            self.buf[off:off + len(row)] = row

    def text(self, x: int, y: int, s: str, scale: int = 2, color=AXIS, center=False) -> int:
        width = len(s) * 6 * scale - scale
        if center:
            x = int(x - width / 2)
        for ch in s:
            glyph = FONT.get(ch, FONT[" "])
            for r, bits in enumerate(glyph):
                for c, bit in enumerate(bits):
                    if bit == "1":
                        self.rect(x + c * scale, y + r * scale, scale, scale, color)
            x += 6 * scale
        return width

    def png(self) -> bytes:
        raw = bytearray()
        stride = self.w * 3
        for y in range(self.h):
            raw.append(0)
            raw += self.buf[y * stride:(y + 1) * stride]

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        ihdr = struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def _fmt(v: float) -> str:
    a = abs(v)
    if a >= 10000:
        return f"{v/10000:.1f}"
    if a >= 100:
        return f"{v:.0f}"
    return f"{v:.1f}"


def bar_chart(items: list[tuple[str, float]], w: int = 900, h: int = 380) -> bytes:
    """items: [(标签, 数值)]，标签请用数字/短横线，中文需调用方自行改写成序号。"""
    cv = Canvas(w, h)
    pad_l, pad_r, pad_t, pad_b = 116, 22, 42, 52
    inner_w, inner_h = w - pad_l - pad_r, h - pad_t - pad_b
    if not items:
        cv.text(w // 2, h // 2, "0", 3, center=True)
        return cv.png()

    vmax = max(abs(v) for _, v in items) or 1.0
    # 网格与 y 轴刻度（单位：原始数值，千分位）
    for i in range(5):
        y = pad_t + inner_h * i // 4
        cv.rect(pad_l, y, inner_w, 1, GRID)
        tick = f"{vmax * (4 - i) / 4:,.0f}"
        tw = len(tick) * 6 * 2 - 2
        cv.text(pad_l - 16 - tw, y - 7, tick, 2, AXIS)
    cv.rect(pad_l, h - pad_b, inner_w, 1, AXIS)

    n = len(items)
    step = inner_w / n
    bw = max(2, min(46, int(step * 0.66)))
    label_every = max(1, int(52 / step) + 1)
    for i, (label, value) in enumerate(items):
        bh = max(1, int(abs(value) / vmax * inner_h))
        x = int(pad_l + step * i + (step - bw) / 2)
        y = h - pad_b - bh
        cv.rect(x, y, bw, bh, BAR_NEG if value < 0 else BAR)
        if i % label_every == 0 and len(label) * 12 <= step * label_every:
            cv.text(int(x + bw / 2), h - pad_b + 8, label, 2, AXIS, center=True)
        vtext = f"{value:,.0f}"
        if len(vtext) * 12 <= step * 0.82 and (x + bw / 2 - len(vtext) * 6) >= pad_l - 30:
            cv.text(int(x + bw / 2), y - 18, vtext, 2, (206, 206, 214), center=True)
    return cv.png()
