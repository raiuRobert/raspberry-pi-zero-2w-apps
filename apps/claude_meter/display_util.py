"""Small utilities shared by display.py and animations.py / main.py."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageFont


def pil_to_rgb565_bytes(img: Image.Image) -> bytes:
    """Pack a PIL RGB image into RGB565 big-endian bytes — the format
    WhisPlayBoard.draw_image and .fill_screen expect."""
    arr = np.asarray(img.convert("RGB"), dtype=np.uint16)
    r = (arr[..., 0] >> 3) & 0x1F
    g = (arr[..., 1] >> 2) & 0x3F
    b = (arr[..., 2] >> 3) & 0x1F
    px = (r << 11) | (g << 5) | b
    out = np.empty(px.size * 2, dtype=np.uint8)
    out[0::2] = (px >> 8).astype(np.uint8).ravel()
    out[1::2] = (px & 0xFF).astype(np.uint8).ravel()
    return out.tobytes()


_FONT_CACHE: dict[int, ImageFont.FreeTypeFont] = {}


def font(size: int, bold: bool = False, serif: bool = False) -> ImageFont.FreeTypeFont:
    key = size * 100 + (1 if bold else 0) + (10 if serif else 0)
    f = _FONT_CACHE.get(key)
    if f is not None:
        return f
    if serif:
        path = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf" if bold \
            else "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"
    else:
        path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold \
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    try:
        f = ImageFont.truetype(path, size)
    except OSError:
        f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


def fmt_eta(mins: int | None) -> str:
    if mins is None:
        return "--"
    if mins <= 0:
        return "now"
    h, m = divmod(int(mins), 60)
    if h >= 24:
        d, h = divmod(h, 24)
        return f"{d}d{h:02d}h"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"
