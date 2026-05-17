"""Claudepix splash-screen animator.

Loads sprite frames from `assets/sprites/` (produced by `tools/convert_sprites.py`),
selects an animation by mood group ('idle' / 'work' / 'expression' / 'dance'),
and advances frames according to each frame's hold-time. Pre-scales every
frame at load time so the per-tick draw cost on the Pi Zero is just a paste
(no resampling in the hot path).
"""

from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path
from typing import Optional

from PIL import Image

ASSETS_DIR = Path(__file__).resolve().parent / "assets" / "sprites"
INDEX_FILE = ASSETS_DIR / "index.json"

GROUP_ROTATE_S = 20.0  # how long to stay on one animation within the same group
NATIVE_SIZE = 20  # 20x20 source frames

log = logging.getLogger("clawdmeter.animations")


class Animator:
    """Sprite player that picks an animation by group and advances frames.

    Pre-scales every frame to `sprite_px` (square) on load using nearest-neighbor
    so playback is paste-only. Use `tick(now)` each render cycle; it returns
    the current PIL Image (always — caller can decide whether the frame has
    changed via `last_advance_ts`).
    """

    def __init__(self, sprite_px: int, assets_dir: Path = ASSETS_DIR):
        self.sprite_px = sprite_px
        self.assets_dir = assets_dir
        self._library: dict[str, dict] = {}    # name -> {frames: [Image], holds_ms: [int], group: str}
        self._by_group: dict[str, list[str]] = {}
        self._current_name: Optional[str] = None
        self._current_group: Optional[str] = None
        self._frame_idx = 0
        self._frame_started_at = 0.0
        self._anim_started_at = 0.0
        self.last_advance_ts: float = 0.0
        self._load()

    def _load(self) -> None:
        if not INDEX_FILE.exists():
            raise FileNotFoundError(
                f"sprite index not found at {INDEX_FILE} — run tools/convert_sprites.py first"
            )
        idx = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        for name, meta in idx.items():
            frames = []
            for rel in meta["frames"]:
                img = Image.open(self.assets_dir / rel).convert("RGB")
                if self.sprite_px != NATIVE_SIZE:
                    img = img.resize((self.sprite_px, self.sprite_px), Image.NEAREST)
                frames.append(img)
            self._library[name] = {
                "frames": frames,
                "holds_ms": list(meta["holds_ms"]),
                "group": meta["group"],
            }
            self._by_group.setdefault(meta["group"], []).append(name)
        log.info(
            "loaded %d animations across groups %s (%dpx)",
            len(self._library), sorted(self._by_group), self.sprite_px,
        )

    @property
    def groups(self) -> list[str]:
        return sorted(self._by_group)

    @property
    def current(self) -> Optional[str]:
        return self._current_name

    def set_mood(self, group: str, now: float | None = None) -> None:
        """Switch to a random animation in `group`. If already in this group
        and rotation interval hasn't elapsed, do nothing — keeps the current
        animation playing rather than jittering on every state read.
        """
        now = time.time() if now is None else now
        if group not in self._by_group:
            # Fall back to idle if requested group has no sprites.
            if "idle" in self._by_group:
                group = "idle"
            else:
                group = self.groups[0]

        if (
            self._current_group == group
            and self._current_name
            and (now - self._anim_started_at) < GROUP_ROTATE_S
        ):
            return

        choices = self._by_group[group]
        # Prefer not to repeat the current animation when we have alternatives.
        pool = [n for n in choices if n != self._current_name] or choices
        self._current_name = random.choice(pool)
        self._current_group = group
        self._frame_idx = 0
        self._frame_started_at = now
        self._anim_started_at = now
        self.last_advance_ts = now
        log.debug("mood=%s -> %s", group, self._current_name)

    def thumbnail(self, px: int, anim: str = "idle_breathe") -> Image.Image:
        """Return a small fixed-frame icon for use in chrome (e.g. header).
        Falls back to the first available animation if `anim` isn't present."""
        name = anim if anim in self._library else next(iter(self._library))
        img = self._library[name]["frames"][0]
        if img.size != (px, px):
            img = img.resize((px, px), Image.NEAREST)
        return img

    def tick(self, now: float | None = None) -> Image.Image:
        """Advance the frame pointer if the current frame's hold has elapsed,
        and return the current frame image. Always returns an Image — caller
        can check `last_advance_ts` to see if the frame just changed."""
        now = time.time() if now is None else now
        if self._current_name is None:
            # Lazy default: pick from idle if available.
            self.set_mood("idle", now=now)
        anim = self._library[self._current_name]
        hold_ms = anim["holds_ms"][self._frame_idx]
        if (now - self._frame_started_at) * 1000.0 >= hold_ms:
            self._frame_idx = (self._frame_idx + 1) % len(anim["frames"])
            self._frame_started_at = now
            self.last_advance_ts = now
        return anim["frames"][self._frame_idx]


# ---------- mood selection from usage state ----------

# Tunables for mapping (session %, rate %/min) -> mood group.
# Order matters: first match wins.
def select_mood(session_pct: Optional[int], rate_pct_per_min: float,
                status: str = "allowed", stale: bool = False) -> str:
    """Pick the animation group from current state.

    - error/stale/no data -> idle (the breathing/blinking sprites are calming)
    - limited (hit a cap) -> dance (frantic, signals the limit)
    - high session usage (>=90%) -> dance
    - high consumption rate (>=3 %/min) -> expression (surprised/wink)
    - any consumption (>=0.3 %/min) -> work (coding/thinking sprites)
    - otherwise -> idle
    """
    if stale or status in ("error", "waiting", "unknown") or session_pct is None:
        return "idle"
    if status == "limited":
        return "dance"
    if session_pct >= 90:
        return "dance"
    if rate_pct_per_min >= 3.0:
        return "expression"
    if rate_pct_per_min >= 0.3:
        return "work"
    return "idle"
