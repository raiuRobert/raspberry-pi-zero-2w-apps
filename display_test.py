"""Display test for the Whisplay HAT.

Targets the WhisPlay.WhisPlayBoard driver shipped with the HAT. Renders a mock
Clawdmeter dashboard at the board's native resolution (LCD_WIDTH x LCD_HEIGHT)
and sweeps the session percentage so we can confirm the refresh path works.

Run on the Pi:
    python3 ~/clawdmeter/display_test.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path.home() / "Whisplay" / "Driver"))
from WhisPlay import WhisPlayBoard  # noqa: E402


def pil_to_rgb565_bytes(img: Image.Image) -> bytes:
    """Pack a PIL RGB image into RGB565 big-endian bytes (the format
    WhisPlayBoard.draw_image / fill_screen expect)."""
    arr = np.asarray(img.convert("RGB"), dtype=np.uint16)
    r = (arr[..., 0] >> 3) & 0x1F
    g = (arr[..., 1] >> 2) & 0x3F
    b = (arr[..., 2] >> 3) & 0x1F
    rgb565 = (r << 11) | (g << 5) | b
    high = (rgb565 >> 8).astype(np.uint8)
    low = (rgb565 & 0xFF).astype(np.uint8)
    interleaved = np.empty(rgb565.size * 2, dtype=np.uint8)
    interleaved[0::2] = high.ravel()
    interleaved[1::2] = low.ravel()
    return interleaved.tobytes()


def _font(size: int) -> ImageFont.FreeTypeFont:
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _bar_color(pct: int, lo, mid, hi):
    return lo if pct < 75 else mid if pct < 90 else hi


def draw_bar(d, x, y, w, h, pct, label, color):
    pct = max(0, min(100, pct))
    d.rectangle((x, y, x + w, y + h), outline=(80, 80, 90), width=2)
    fill_w = int((w - 4) * pct / 100)
    if fill_w > 0:
        d.rectangle((x + 2, y + 2, x + 2 + fill_w, y + h - 2), fill=color)
    f = _font(16)
    d.text((x, y - 20), label, font=f, fill=(220, 220, 220))
    d.text((x + w - 50, y - 20), f"{pct}%", font=f, fill=(220, 220, 220))


def build_frame(width: int, height: int, s_pct: int, w_pct: int,
                s_reset_min: int, w_reset_min: int) -> Image.Image:
    img = Image.new("RGB", (width, height), (12, 14, 22))
    d = ImageDraw.Draw(img)
    d.text((10, 8), "Clawdmeter", font=_font(22), fill=(245, 180, 70))
    d.text((10, 36), "display test", font=_font(14), fill=(140, 140, 150))

    s_color = _bar_color(s_pct, (90, 200, 120), (230, 180, 60), (230, 80, 80))
    w_color = _bar_color(w_pct, (110, 160, 230), (230, 180, 60), (230, 80, 80))
    bar_w = width - 28
    draw_bar(d, 14, 100, bar_w, 22, s_pct, "session 5h", s_color)
    draw_bar(d, 14, 170, bar_w, 22, w_pct, "weekly 7d", w_color)

    def fmt(mins):
        h, m = divmod(max(0, mins), 60)
        return f"{h}h{m:02d}m" if h else f"{m}m"

    d.text((14, 200), f"resets in {fmt(s_reset_min)}", font=_font(14), fill=(160, 200, 170))
    d.text((14, 220), f"weekly  in {fmt(w_reset_min)}", font=_font(14), fill=(160, 180, 220))
    d.rectangle((0, height - 18, width, height), fill=(20, 22, 30))
    d.text((10, height - 16), "test frame", font=_font(14), fill=(120, 120, 130))
    return img


def main():
    board = WhisPlayBoard()
    W, H = board.LCD_WIDTH, board.LCD_HEIGHT
    print(f"display: {W}x{H}")

    print("backlight on")
    board.set_backlight(100)

    print("fill screen black")
    board.fill_screen(0x0000)

    print("rgb LED: blue (boot)")
    board.set_rgb(0, 0, 80)

    print("pushing static frame...")
    frame = build_frame(W, H, 45, 28, 120, 7200)
    board.draw_image(0, 0, W, H, pil_to_rgb565_bytes(frame))
    time.sleep(2)

    print("sweeping session 0 -> 100%...")
    for pct in range(0, 101, 10):
        led = (0, 200, 0) if pct < 75 else (200, 150, 0) if pct < 90 else (200, 0, 0)
        board.set_rgb(*led)
        frame = build_frame(W, H, pct, 28, max(0, 120 - pct), 7200)
        board.draw_image(0, 0, W, H, pil_to_rgb565_bytes(frame))
        time.sleep(0.3)

    print("done. holding final frame for 3s.")
    time.sleep(3)
    board.set_rgb(0, 0, 0)
    board.cleanup()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted")
