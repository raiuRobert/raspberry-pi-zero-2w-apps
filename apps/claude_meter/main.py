"""Clawdmeter orchestrator.

Owns the Whisplay LCD, the button, the RGB LED, and supervises the
api_poller subprocess. Single process so the display refresh and the
animation tick share one event loop (avoids contention on the SPI bus
the WhisPlay driver uses).

Run on the Pi:
    python3 ~/clawdmeter/main.py            # foreground, Ctrl+C to stop
    python3 ~/clawdmeter/main.py --once     # one frame, then exit (smoke test)
    python3 ~/clawdmeter/main.py --screen=splash --once
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from enum import Enum
from pathlib import Path

sys.path.insert(0, str(Path.home() / "Whisplay" / "Driver"))
from WhisPlay import WhisPlayBoard  # noqa: E402

from animations import Animator, select_mood
from display import (
    RateTracker, State, led_for, read_state, render_splash, render_usage,
)
from display_util import pil_to_rgb565_bytes

ROOT = Path(__file__).resolve().parent
SCREEN_STATE_PATH = Path(os.environ.get("CLAWDMETER_SCREEN", "/tmp/clawdmeter_screen"))
BACKLIGHT = int(os.environ.get("CLAWDMETER_BACKLIGHT", "100"))

ANIM_TICK_S = 0.06          # splash screen tick rate (~16 fps cap)
USAGE_TICK_S = 1.0          # usage screen redraw cadence
SPRITE_PX = 240             # 20x20 native -> 240x240 on display (12x nearest, fills width)
INTRO_DURATION_S = 1.5      # sprite animation before fading into usage screen
INTRO_FADE_STEPS = 8        # frames in the sprite → usage cross-fade

# Poller supervision (Pi safety rule #3): bounded restart attempts.
POLLER_MAX_RESTARTS = 5
POLLER_WINDOW_S = 60.0
POLLER_BACKOFF_S = 3.0

log = logging.getLogger("clawdmeter.main")


class Screen(str, Enum):
    SPLASH = "splash"
    USAGE = "usage"

    @classmethod
    def load(cls) -> "Screen":
        try:
            return cls(SCREEN_STATE_PATH.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError):
            return cls.SPLASH

    def save(self) -> None:
        tmp = SCREEN_STATE_PATH.with_suffix(".tmp")
        tmp.write_text(self.value, encoding="utf-8")
        tmp.replace(SCREEN_STATE_PATH)


# ---------- poller supervisor ----------

class PollerSupervisor:
    """Spawn and watchdog api_poller.py. After too many restarts in a short
    window, give up — never crash-loop."""

    def __init__(self, script: Path):
        self.script = script
        self._proc: subprocess.Popen | None = None
        self._restarts: deque[float] = deque()
        self.given_up = False

    def start(self) -> None:
        if self.given_up:
            return
        log.info("starting poller: %s", self.script)
        self._proc = subprocess.Popen(
            [sys.executable, str(self.script)],
            stdout=subprocess.DEVNULL,  # poller writes its own state file + uses its own logger
            stderr=subprocess.DEVNULL,
        )

    def check(self) -> None:
        if self.given_up or self._proc is None:
            return
        rc = self._proc.poll()
        if rc is None:
            return
        log.warning("poller exited rc=%s", rc)
        now = time.time()
        self._restarts.append(now)
        cutoff = now - POLLER_WINDOW_S
        while self._restarts and self._restarts[0] < cutoff:
            self._restarts.popleft()
        if len(self._restarts) > POLLER_MAX_RESTARTS:
            log.error("poller restarted %d times in %.0fs — giving up",
                      len(self._restarts), POLLER_WINDOW_S)
            self.given_up = True
            self._proc = None
            return
        time.sleep(POLLER_BACKOFF_S)
        self.start()

    def stop(self) -> None:
        if self._proc is None:
            return
        log.info("stopping poller pid=%s", self._proc.pid)
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, ProcessLookupError):
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
        self._proc = None


# ---------- main loop ----------

class App:
    def __init__(self, args):
        self.args = args
        self.board = WhisPlayBoard()
        self.W, self.H = self.board.LCD_WIDTH, self.board.LCD_HEIGHT
        self.animator = Animator(sprite_px=SPRITE_PX)
        self.rate = RateTracker()
        self.screen = Screen(args.screen) if args.screen else Screen.load()
        self._last_state_read = 0.0
        self._last_usage_draw = 0.0
        self._last_anim_draw = 0.0
        self._last_led: tuple[int, int, int] | None = None
        self._current_state: State = State()
        self._stopping = False
        self._toggle_pending = False
        self._force_exit = False
        self._btn_press_time: float | None = None
        self._screen_lock = threading.Lock()
        self.poller = PollerSupervisor(ROOT / "api_poller.py")
        # Paint the first frame immediately to cover the board's init fill_screen(0).
        self._render_first_frame()

    # ---- button ----
    # Short release (< FORCE_EXIT_S): toggle splash ↔ usage.
    # Hold ≥ FORCE_EXIT_S: exit app and return to launcher menu.

    FORCE_EXIT_S = 10.0

    def _on_button_press(self, *_args):
        with self._screen_lock:
            self._btn_press_time = time.time()

    def _on_button_release(self, *_args):
        with self._screen_lock:
            if self._btn_press_time is None:
                return  # force-exit already fired mid-hold; ignore release
            self._btn_press_time = None
        self._toggle_pending = True

    def _apply_pending_toggle(self) -> None:
        with self._screen_lock:
            # Fire force-exit at exactly FORCE_EXIT_S while button is still held.
            now = time.time()
            if self._btn_press_time is not None and now - self._btn_press_time >= self.FORCE_EXIT_S:
                self._btn_press_time = None
                self._force_exit = True
                log.info("10s hold — returning to launcher menu")
                return
            if not self._toggle_pending:
                return
            self._toggle_pending = False
        self.screen = Screen.USAGE if self.screen == Screen.SPLASH else Screen.SPLASH
        self.screen.save()
        log.info("screen -> %s", self.screen.value)
        # Force a redraw on this tick.
        self._last_usage_draw = 0.0
        self._last_anim_draw = 0.0

    # ---- render ----

    def _refresh_state(self, now: float) -> None:
        if now - self._last_state_read < 0.5:
            return
        self._last_state_read = now
        st = read_state()
        self._current_state = st
        self.rate.observe(st)
        mood = select_mood(
            st.s, self.rate.rate_pct_per_min,
            status=st.st, stale=st.stale,
        )
        self.animator.set_mood(mood, now=now)
        # LED update is cheap; do it each state refresh.
        led = led_for(st)
        if led != self._last_led:
            self.board.set_rgb(*led)
            self._last_led = led

    def _draw_splash(self, now: float) -> None:
        # Animation-driven: re-render only when the animator advanced.
        prev_advance = self.animator.last_advance_ts
        # Force at least one render per second so the clock and status tick.
        force = (now - self._last_anim_draw) >= 1.0
        img = render_splash(self.W, self.H, self._current_state, self.animator, now=now)
        if self.animator.last_advance_ts != prev_advance or force or self._last_anim_draw == 0.0:
            self.board.draw_image(0, 0, self.W, self.H, pil_to_rgb565_bytes(img))
            self._last_anim_draw = now

    def _draw_usage(self, now: float) -> None:
        if now - self._last_usage_draw < USAGE_TICK_S and self._last_usage_draw != 0.0:
            return
        img = render_usage(self.W, self.H, self._current_state, self.animator)
        self.board.draw_image(0, 0, self.W, self.H, pil_to_rgb565_bytes(img))
        self._last_usage_draw = now

    # ---- top-level ----

    def _render_first_frame(self) -> None:
        """Paint one splash frame immediately to cover the board's init fill_screen(0)."""
        self.board.set_backlight(BACKLIGHT)
        now = time.time()
        self._refresh_state(now)
        img = render_splash(self.W, self.H, self._current_state, self.animator, now=now)
        self.board.draw_image(0, 0, self.W, self.H, pil_to_rgb565_bytes(img))
        self._last_anim_draw = now

    def _play_intro(self) -> None:
        """Animate the sprite then fade into the usage screen."""
        from PIL import Image as _Image
        start = time.time()
        last_sprite = None
        while time.time() - start < INTRO_DURATION_S:
            now = time.time()
            self._refresh_state(now)
            img = render_splash(self.W, self.H, self._current_state, self.animator, now=now)
            self.board.draw_image(0, 0, self.W, self.H, pil_to_rgb565_bytes(img))
            last_sprite = img
            time.sleep(ANIM_TICK_S)
        if last_sprite is None:
            last_sprite = _Image.new("RGB", (self.W, self.H), (0, 0, 0))
        target = render_usage(self.W, self.H, self._current_state, self.animator)
        for step in range(1, INTRO_FADE_STEPS + 1):
            frame = _Image.blend(last_sprite, target, step / INTRO_FADE_STEPS)
            self.board.draw_image(0, 0, self.W, self.H, pil_to_rgb565_bytes(frame))
            time.sleep(ANIM_TICK_S)
        self.screen = Screen.USAGE
        self.screen.save()
        self._last_usage_draw = time.time()

    def run_once(self) -> None:
        """Render one frame for the configured screen and exit."""
        self._refresh_state(time.time())
        # Force a draw regardless of cadence.
        self._last_anim_draw = 0.0
        self._last_usage_draw = 0.0
        if self.screen == Screen.SPLASH:
            self._draw_splash(time.time())
        else:
            self._draw_usage(time.time())

    def run(self) -> None:
        log.info("display %dx%d, screen=%s", self.W, self.H, self.screen.value)
        try:
            self.board.on_button_press(self._on_button_press)
            self.board.on_button_release(self._on_button_release)
        except Exception as e:
            log.warning("button registration failed (continuing): %s", e)

        if not self.args.once and not self.args.screen:
            self._play_intro()

        self.poller.start()
        tick = 0
        while not self._stopping and not self._force_exit:
            now = time.time()
            self._apply_pending_toggle()
            self._refresh_state(now)
            if self.screen == Screen.SPLASH:
                self._draw_splash(now)
            else:
                self._draw_usage(now)
            # Pi-safety rule #6: never tight-loop on the CPU.
            time.sleep(ANIM_TICK_S)
            tick += 1
            if tick % 30 == 0:
                self.poller.check()

    def shutdown(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        log.info("shutting down")
        try:
            self.poller.stop()
        except Exception as e:
            log.warning("poller stop: %s", e)
        try:
            self.board.set_rgb(0, 0, 0)
            self.board.fill_screen(0x0000)
            self.board.cleanup()
        except Exception as e:
            log.warning("board cleanup: %s", e)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="clawdmeter")
    p.add_argument("--once", action="store_true",
                   help="render one frame and exit (smoke test)")
    p.add_argument("--screen", choices=[s.value for s in Screen], default=None,
                   help="force a specific screen on startup")
    p.add_argument("--no-poller", action="store_true",
                   help="don't spawn api_poller (assume something else writes the state file)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    app = App(args)

    def _stop(*_):
        app.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        if args.once:
            if not args.no_poller:
                # In --once mode we don't run the poller; assume state file exists or we'll show waiting.
                pass
            app.run_once()
            time.sleep(0.5)  # let LCD finish drawing
            app.shutdown()
            return 0
        if args.no_poller:
            app.poller.given_up = True  # disable spawn
        app.run()
    finally:
        app.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
