"""Whisplay HAT app launcher.

Shows a scrollable menu on boot. Navigate with short press (quick tap),
launch with long press (hold 0.7s). Apps run as subprocesses; when they
exit the menu reappears.

Run on the Pi:
    python3 ~/clawdmeter/launcher.py
"""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
import threading  # used for threading.Lock and threading.Event
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(Path.home() / "Whisplay" / "Driver"))
sys.path.insert(0, str(ROOT / "apps" / "clawdmeter"))
from WhisPlay import WhisPlayBoard

from animations import Animator
from display import State
from display_util import pil_to_rgb565_bytes
from menu_display import ITEM_CLIP_W, render_menu, text_width

# ---------- app registry ----------
APPS = [
    {"name": "Claude Meter", "script": str(ROOT / "apps" / "clawdmeter" / "main.py")},
    # Add more apps here: {"name": "...", "script": str(ROOT / "other.py")}
]

# ---------- tunables ----------
BACKLIGHT = 100
TICK_S = 0.06           # ~16 fps menu refresh
HOLD_S = 2.0            # seconds to hold for long press (launch)
SPRITE_PX = 240         # splash sprite size
SCROLL_SPEED = 40.0     # px/s scroll speed for long item names
SCROLL_PAUSE_S = 1.0    # pause at each end of scroll

log = logging.getLogger("launcher")


class Launcher:
    def __init__(self):
        self.board = WhisPlayBoard()
        self.W, self.H = self.board.LCD_WIDTH, self.board.LCD_HEIGHT
        self.animator = Animator(sprite_px=SPRITE_PX)

        self.selected = 0
        self._scroll_px = 0.0
        self._scroll_dir = 1            # +1 scroll left, -1 scroll right
        self._scroll_paused_until = 0.0

        self._stopping = False
        self._lock = threading.Lock()
        self._press_start: float | None = None
        self._short_pending = threading.Event()

    # ---- button ----
    # Long press fires at the HOLD_S mark while button is still held (not on release).
    # Short press fires on release only if the hold was shorter than HOLD_S.

    def _on_btn_press(self, *_):
        with self._lock:
            self._press_start = time.time()

    def _on_btn_release(self, *_):
        with self._lock:
            if self._press_start is None:
                return  # long press already fired mid-hold; ignore this release
            self._press_start = None
        self._short_pending.set()

    def _apply_button(self, now: float) -> None:
        # Check if we've crossed the HOLD_S threshold this tick.
        with self._lock:
            held_long = (
                self._press_start is not None
                and now - self._press_start >= HOLD_S
            )
            if held_long:
                self._press_start = None  # prevent release from also firing short press

        if held_long:
            log.info("long press — launching")
            self._launch(APPS[self.selected])
            return

        if self._short_pending.is_set():
            self._short_pending.clear()
            self.selected = (self.selected + 1) % len(APPS)
            self._reset_scroll()
            log.info("short press — selected %d (%s)", self.selected, APPS[self.selected]["name"])

    # ---- scroll ----

    def _reset_scroll(self) -> None:
        self._scroll_px = 0.0
        self._scroll_dir = 1
        self._scroll_paused_until = time.time() + SCROLL_PAUSE_S

    def _update_scroll(self, now: float) -> None:
        name_w = text_width(APPS[self.selected]["name"])
        if name_w <= ITEM_CLIP_W:
            self._scroll_px = 0.0
            return
        if now < self._scroll_paused_until:
            return
        self._scroll_px += self._scroll_dir * SCROLL_SPEED * TICK_S
        max_off = float(name_w - ITEM_CLIP_W)
        if self._scroll_px >= max_off:
            self._scroll_px = max_off
            self._scroll_dir = -1
            self._scroll_paused_until = now + SCROLL_PAUSE_S
        elif self._scroll_px <= 0.0:
            self._scroll_px = 0.0
            self._scroll_dir = 1
            self._scroll_paused_until = now + SCROLL_PAUSE_S

    # ---- launch transition ----

    def _launch(self, app: dict) -> None:
        log.info("launching '%s'", app["name"])

        try:
            self.board.fill_screen(0x0000)
            self.board.cleanup()
        except Exception as e:
            log.warning("board pre-launch cleanup: %s", e)

        try:
            subprocess.run([sys.executable, app["script"]], check=False)
        except Exception as e:
            log.error("app '%s' error: %s", app["name"], e)

        log.info("'%s' exited, returning to menu", app["name"])

        # Re-initialise board after the app's cleanup.
        try:
            self.board = WhisPlayBoard()
            self.board.set_backlight(BACKLIGHT)
            self.board.fill_screen(0x0000)
            self.board.on_button_press(self._on_btn_press)
            self.board.on_button_release(self._on_btn_release)
        except Exception as e:
            log.error("board re-init failed: %s", e)

        self._reset_scroll()

    # ---- main loop ----

    def run(self) -> None:
        log.info("launcher start — %d app(s)", len(APPS))
        self.board.set_backlight(BACKLIGHT)
        self.board.fill_screen(0x0000)
        try:
            self.board.on_button_press(self._on_btn_press)
            self.board.on_button_release(self._on_btn_release)
        except Exception as e:
            log.warning("button registration failed: %s", e)

        while not self._stopping:
            now = time.time()
            self._apply_button(now)
            self._update_scroll(now)
            img = render_menu(self.W, self.H, APPS, self.selected, self._scroll_px)
            self.board.draw_image(0, 0, self.W, self.H, pil_to_rgb565_bytes(img))
            time.sleep(TICK_S)

    def shutdown(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        log.info("launcher shutdown")
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
    launcher = Launcher()

    def _stop(*_):
        launcher.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        launcher.run()
    finally:
        launcher.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
