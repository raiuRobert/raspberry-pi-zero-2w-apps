"""GPIO Monitor — real-time 40-pin header visualiser for the Whisplay HAT.

Shows the full Pi Zero 2W 40-pin header as a 2-column grid. Pins light up
green when HIGH. Power, GND, and HAT-claimed pins are colour-coded.

Run on the Pi:
    python3 ~/claudemeter/apps/gpio_monitor/main.py
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from pathlib import Path

import gpiod
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path.home() / "Whisplay" / "Driver"))
from WhisPlay import WhisPlayBoard  # noqa: E402

log = logging.getLogger("gpio_monitor.main")

# ---------- pin tables ----------

POWER_33 = {1, 17}
POWER_5V = {2, 4}
GND      = {6, 9, 14, 20, 25, 30, 34, 39}
HAT_PINS = {7, 11, 13, 15, 16, 18, 19, 21, 22, 23, 24}

BOARD_TO_BCM = {
    3: 2,  5: 3,  7: 4,  8: 14, 10: 15, 11: 17, 12: 18, 13: 27,
    15: 22, 16: 23, 18: 24, 19: 10, 21: 9,  22: 25, 23: 11,
    24: 8,  26: 7,  29: 5,  31: 6,  32: 12, 33: 13, 35: 19,
    36: 16, 37: 26, 38: 20, 40: 21,
}

ALL_BOARD_PINS = list(range(1, 41))

# Pins we can actually read (not power, not GND, not claimed by HAT)
READABLE_PINS = [
    p for p in ALL_BOARD_PINS
    if p not in POWER_33 and p not in POWER_5V
    and p not in GND and p not in HAT_PINS
    and p in BOARD_TO_BCM
]

# ---------- colours ----------

BG          = (10, 12, 20)
COL_33V     = (210, 100, 50)
COL_5V      = (210, 55,  55)
COL_GND     = (55,  55,  65)
COL_HAT     = (70,  70,  190)
COL_LOW     = (40,  42,  52)
COL_HIGH    = (70,  210, 90)
COL_TEXT    = (220, 220, 220)
COL_DIM     = (100, 102, 110)

# ---------- helpers ----------

def _pil_to_rgb565(img: Image.Image) -> bytes:
    arr = np.asarray(img.convert("RGB"), dtype=np.uint16)
    r = (arr[..., 0] >> 3) & 0x1F
    g = (arr[..., 1] >> 2) & 0x3F
    b = (arr[..., 2] >> 3) & 0x1F
    rgb565 = (r << 11) | (g << 5) | b
    hi = (rgb565 >> 8).astype(np.uint8)
    lo = (rgb565 & 0xFF).astype(np.uint8)
    out = np.empty(rgb565.size * 2, dtype=np.uint8)
    out[0::2] = hi.ravel()
    out[1::2] = lo.ravel()
    return out.tobytes()


def _font(size: int) -> ImageFont.FreeTypeFont:
    for p in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _pin_label(board_pin: int) -> str:
    if board_pin in POWER_33:
        return "3V3"
    if board_pin in POWER_5V:
        return "5V "
    if board_pin in GND:
        return "GND"
    if board_pin in HAT_PINS:
        bcm = BOARD_TO_BCM.get(board_pin)
        return f"G{bcm:02d}" if bcm is not None else "HAT"
    bcm = BOARD_TO_BCM.get(board_pin)
    return f"G{bcm:02d}" if bcm is not None else "?  "


def _pin_color(board_pin: int, state: dict[int, bool]) -> tuple:
    if board_pin in POWER_33:
        return COL_33V
    if board_pin in POWER_5V:
        return COL_5V
    if board_pin in GND:
        return COL_GND
    if board_pin in HAT_PINS:
        return COL_HAT
    return COL_HIGH if state.get(board_pin, False) else COL_LOW


# ---------- GPIO reader ----------

class GpioReader:
    def __init__(self):
        self._lines: dict[int, gpiod.Line] = {}  # board_pin -> line
        self._chip: gpiod.Chip | None = None
        self._open()

    def _open(self) -> None:
        try:
            self._chip = gpiod.Chip("/dev/gpiochip0")
        except Exception as e:
            log.warning("gpiochip0 open failed: %s", e)
            return
        for board_pin in READABLE_PINS:
            bcm = BOARD_TO_BCM[board_pin]
            try:
                line = self._chip.get_line(bcm)
                line.request(consumer="gpio_monitor", type=gpiod.LINE_REQ_DIR_IN)
                self._lines[board_pin] = line
            except Exception as e:
                log.debug("pin %d (BCM %d) not available: %s", board_pin, bcm, e)

        log.info("opened %d readable GPIO lines", len(self._lines))

    def read(self) -> dict[int, bool]:
        state: dict[int, bool] = {}
        for board_pin, line in self._lines.items():
            try:
                state[board_pin] = bool(line.get_value())
            except Exception:
                state[board_pin] = False
        return state

    def close(self) -> None:
        for line in self._lines.values():
            try:
                line.release()
            except Exception:
                pass
        self._lines.clear()
        if self._chip:
            try:
                self._chip.close()
            except Exception:
                pass


# ---------- renderer ----------

HEADER_H  = 20
FOOTER_H  = 16
DOT_R     = 3   # dot radius
ROWS      = 20  # 40 pins / 2 columns


def render(W: int, H: int, state: dict[int, bool], fnt_sm, fnt_xs) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Header
    d.rectangle((0, 0, W, HEADER_H - 1), fill=(18, 20, 32))
    d.text((6, 2), "GPIO Monitor", font=fnt_sm, fill=COL_TEXT)

    # Grid area
    grid_h = H - HEADER_H - FOOTER_H
    row_h  = grid_h / ROWS
    col_w  = W / 2

    for row in range(ROWS):
        odd_pin  = 2 * row + 1   # left column: pins 1,3,5,...
        even_pin = 2 * row + 2   # right column: pins 2,4,6,...
        y = HEADER_H + row * row_h
        cy = int(y + row_h / 2)

        for col, board_pin in enumerate((odd_pin, even_pin)):
            x0 = int(col * col_w)
            x1 = int(x0 + col_w)
            color = _pin_color(board_pin, state)
            label = _pin_label(board_pin)

            # Row background (subtle alternating shade)
            if row % 2 == 0:
                d.rectangle((x0, int(y), x1 - 1, int(y + row_h) - 1),
                             fill=(14, 16, 26))

            # Dot
            dot_x = x0 + 8 if col == 0 else x1 - 8
            d.ellipse((dot_x - DOT_R, cy - DOT_R, dot_x + DOT_R, cy + DOT_R),
                      fill=color)

            # Pin number (dim)
            num_str = str(board_pin)
            if col == 0:
                d.text((x0 + 14, cy - 5), num_str, font=fnt_xs, fill=COL_DIM)
                d.text((x0 + 30, cy - 5), label, font=fnt_xs, fill=COL_TEXT)
            else:
                d.text((x1 - 14 - len(num_str) * 5, cy - 5), num_str,
                       font=fnt_xs, fill=COL_DIM)
                d.text((x1 - 30 - 18, cy - 5), label, font=fnt_xs, fill=COL_TEXT)

    # Centre divider line
    d.line((W // 2, HEADER_H, W // 2, H - FOOTER_H), fill=(30, 32, 45), width=1)

    # Footer legend
    fy = H - FOOTER_H + 2
    d.ellipse((4, fy + 3, 4 + DOT_R * 2, fy + 3 + DOT_R * 2), fill=COL_HIGH)
    d.text((12, fy), "HIGH", font=fnt_xs, fill=COL_DIM)
    d.ellipse((52, fy + 3, 52 + DOT_R * 2, fy + 3 + DOT_R * 2), fill=COL_LOW)
    d.text((60, fy), "LOW", font=fnt_xs, fill=COL_DIM)
    d.ellipse((94, fy + 3, 94 + DOT_R * 2, fy + 3 + DOT_R * 2), fill=COL_HAT)
    d.text((102, fy), "HAT", font=fnt_xs, fill=COL_DIM)
    d.ellipse((136, fy + 3, 136 + DOT_R * 2, fy + 3 + DOT_R * 2), fill=COL_33V)
    d.text((144, fy), "3V3", font=fnt_xs, fill=COL_DIM)
    d.ellipse((178, fy + 3, 178 + DOT_R * 2, fy + 3 + DOT_R * 2), fill=COL_GND)
    d.text((186, fy), "GND", font=fnt_xs, fill=COL_DIM)

    return img


# ---------- app ----------

TICK_S       = 0.06
POLL_EVERY_S = 0.05
FORCE_EXIT_S = 10.0


class App:
    def __init__(self):
        self.board = WhisPlayBoard()
        self.W, self.H = self.board.LCD_WIDTH, self.board.LCD_HEIGHT
        self.gpio = GpioReader()
        self._state: dict[int, bool] = {}
        self._stopping = False
        self._force_exit = False
        self._lock = threading.Lock()
        self._btn_press_time: float | None = None
        self._fnt_sm = _font(11)
        self._fnt_xs = _font(9)
        self._last_poll = 0.0

        self.board.set_backlight(100)
        try:
            self.board.on_button_press(self._on_press)
            self.board.on_button_release(self._on_release)
        except Exception as e:
            log.warning("button registration failed: %s", e)

        self._draw()

    def _on_press(self, *_):
        with self._lock:
            self._btn_press_time = time.time()

    def _on_release(self, *_):
        with self._lock:
            self._btn_press_time = None  # short tap — nothing to do

    def _check_hold(self) -> None:
        with self._lock:
            if self._btn_press_time is not None:
                if time.time() - self._btn_press_time >= FORCE_EXIT_S:
                    self._btn_press_time = None
                    self._force_exit = True
                    log.info("10s hold — returning to launcher")

    def _poll(self, now: float) -> None:
        if now - self._last_poll < POLL_EVERY_S:
            return
        self._last_poll = now
        self._state = self.gpio.read()

    def _draw(self) -> None:
        img = render(self.W, self.H, self._state, self._fnt_sm, self._fnt_xs)
        self.board.draw_image(0, 0, self.W, self.H, _pil_to_rgb565(img))

    def run(self) -> None:
        log.info("gpio monitor start — %d readable pins", len(READABLE_PINS))
        while not self._stopping and not self._force_exit:
            now = time.time()
            self._check_hold()
            self._poll(now)
            self._draw()
            time.sleep(TICK_S)

    def shutdown(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        log.info("gpio monitor shutdown")
        self.gpio.close()
        try:
            self.board.set_rgb(0, 0, 0)
            self.board.fill_screen(0x0000)
            self.board.cleanup()
        except Exception as e:
            log.warning("board cleanup: %s", e)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = App()

    def _stop(*_):
        app.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        app.run()
    finally:
        app.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
