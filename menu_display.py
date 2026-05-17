"""Menu screen renderer for the Whisplay HAT launcher.

Pure PIL — no board access. Called from launcher.py each tick.
"""

from __future__ import annotations

import time
from typing import Sequence

from PIL import Image, ImageDraw

from display_util import font

# Layout constants (exported so launcher.py can compute scroll bounds)
HEADER_H = 40
FOOTER_H = 20
ITEM_H = 46
ITEM_MARGIN_X = 8       # outer margin each side
ITEM_PAD_X = 12         # text padding inside highlight box
ITEM_FONT_SIZE = 18
# Usable text clip width inside the selected-item highlight box
ITEM_CLIP_W = 240 - 2 * ITEM_MARGIN_X - 2 * ITEM_PAD_X

BG = (10, 12, 18)
HEADER_COLOR = (240, 240, 240)
DIM_COLOR = (120, 122, 130)
HIGHLIGHT_BG = (38, 42, 56)
SELECTED_TEXT = (240, 240, 240)
UNSELECTED_TEXT = (150, 152, 162)
HINT_COLOR = (80, 82, 92)


_MEASURE_IMG = Image.new("RGB", (1, 1))
_MEASURE_DRAW = ImageDraw.Draw(_MEASURE_IMG)


def _measure(text: str, f) -> int:
    return int(_MEASURE_DRAW.textlength(text, font=f))


def _truncate(text: str, max_px: int, f) -> str:
    if _measure(text, f) <= max_px:
        return text
    while text and _measure(text + "…", f) > max_px:
        text = text[:-1]
    return text + "…"


def text_width(text: str, size: int = ITEM_FONT_SIZE) -> int:
    """Return pixel width of text at the item font size. Used by launcher for scroll bounds."""
    return _measure(text, font(size))


def render_menu(
    width: int,
    height: int,
    items: Sequence[dict],
    selected: int,
    scroll_px: float,
) -> Image.Image:
    """
    items: list of {"name": str, ...}
    selected: index of highlighted item
    scroll_px: horizontal scroll offset for the selected item's text (pixels left)
    """
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)

    # Header
    f_title = font(20, bold=False, serif=True)
    d.text((ITEM_MARGIN_X, 8), "Apps", font=f_title, fill=HEADER_COLOR)
    clock = time.strftime("%H:%M")
    f_clock = font(12)
    cw = _measure(clock, f_clock)
    d.text((width - cw - ITEM_MARGIN_X, 12), clock, font=f_clock, fill=DIM_COLOR)

    # Items
    f_item = font(ITEM_FONT_SIZE)
    list_top = HEADER_H
    list_bottom = height - FOOTER_H
    max_visible = (list_bottom - list_top) // ITEM_H

    # Centre the visible window around the selected item
    first = max(0, min(selected - max_visible // 2, len(items) - max_visible))
    first = max(0, first)

    for i, app in enumerate(items):
        slot = i - first
        if slot < 0 or slot >= max_visible:
            continue
        y = list_top + slot * ITEM_H
        item_x = ITEM_MARGIN_X
        item_w = width - 2 * ITEM_MARGIN_X
        item_y = y + 2
        item_h = ITEM_H - 4

        if i == selected:
            d.rounded_rectangle(
                (item_x, item_y, item_x + item_w, item_y + item_h),
                radius=10, fill=HIGHLIGHT_BG,
            )
            # Scrolling text: render into a temporary strip, then crop and paste
            name = app["name"]
            name_w = _measure(name, f_item)
            strip_w = max(name_w + 4, ITEM_CLIP_W)
            strip = Image.new("RGB", (strip_w, item_h), HIGHLIGHT_BG)
            sd = ImageDraw.Draw(strip)
            text_y = (item_h - ITEM_FONT_SIZE) // 2 - 1
            sd.text((0, text_y), name, font=f_item, fill=SELECTED_TEXT)
            # Crop to clip region, offset by scroll
            offset = min(int(scroll_px), max(0, strip_w - ITEM_CLIP_W))
            crop = strip.crop((offset, 0, offset + ITEM_CLIP_W, item_h))
            text_x = item_x + ITEM_PAD_X
            img.paste(crop, (text_x, item_y))
        else:
            text_y = item_y + (item_h - ITEM_FONT_SIZE) // 2 - 1
            label = _truncate(app["name"], item_w - 2 * ITEM_PAD_X, f_item)
            d.text((item_x + ITEM_PAD_X, text_y), label, font=f_item, fill=UNSELECTED_TEXT)

    # Footer hint
    f_hint = font(12)
    hint = "hold to open"
    hw = _measure(hint, f_hint)
    d.text(((width - hw) // 2, height - FOOTER_H + 3), hint, font=f_hint, fill=HINT_COLOR)

    return img
