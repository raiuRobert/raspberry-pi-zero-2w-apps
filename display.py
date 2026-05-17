"""Dashboard and splash-screen renderers for the Whisplay HAT.

Decoupled from the main loop: this module is pure rendering + state I/O.
`main.py` owns the WhisPlayBoard and calls `render_usage` / `render_splash`
on each tick.

State file schema (see `api_poller.py`):
    {"s": int|None, "sr": int|None, "w": int|None, "wr": int|None,
     "st": "allowed"|"limited"|"error", "ok": bool, "ts": float,
     "err": str (optional)}
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw

from animations import Animator, select_mood
from display_util import fmt_eta, font

STATE_PATH = Path(os.environ.get("CLAWDMETER_STATE", "/tmp/clawdmeter_state.json"))
STALE_AFTER_S = 90

log = logging.getLogger("clawdmeter.display")


# ---------- state ----------

@dataclass
class State:
    s: Optional[int] = None
    sr: Optional[int] = None
    w: Optional[int] = None
    wr: Optional[int] = None
    st: str = "unknown"
    ok: bool = False
    ts: float = 0.0
    err: Optional[str] = None

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.ts) if self.ts else float("inf")

    @property
    def stale(self) -> bool:
        return self.age_s > STALE_AFTER_S


def read_state() -> State:
    try:
        d = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return State(err="waiting for poller", st="waiting")
    except (json.JSONDecodeError, OSError) as e:
        return State(err=f"state read: {e}", st="error")
    return State(
        s=d.get("s"), sr=d.get("sr"), w=d.get("w"), wr=d.get("wr"),
        st=d.get("st", "unknown"), ok=bool(d.get("ok", False)),
        ts=float(d.get("ts", 0.0)), err=d.get("err"),
    )


# ---------- rate-of-change tracker ----------

class RateTracker:
    """Sliding window of (ts, session_pct) samples for computing %/min rate.

    Keeps samples within the last `window_s` seconds. Rate is computed as the
    slope between the oldest and newest point, in %-per-minute.
    """

    def __init__(self, window_s: float = 300.0):
        self.window_s = window_s
        self._samples: deque[tuple[float, int]] = deque()

    def observe(self, st: State) -> None:
        if st.s is None or not st.ok:
            return
        now = time.time()
        self._samples.append((now, st.s))
        cutoff = now - self.window_s
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    @property
    def rate_pct_per_min(self) -> float:
        if len(self._samples) < 2:
            return 0.0
        t0, v0 = self._samples[0]
        t1, v1 = self._samples[-1]
        dt = t1 - t0
        if dt < 5.0:
            return 0.0
        return (v1 - v0) / (dt / 60.0)


# ---------- shared chrome ----------

def _status_pill(st: State) -> dict:
    if st.st == "limited":
        return {"text": "LIMITED", "bg": (230, 80, 80), "fg": (255, 255, 255)}
    if st.st == "error":
        err = (st.err or "").lower()
        if "401" in err or "authentication" in err or "invalid x-api-key" in err:
            return {"text": "AUTH", "bg": (200, 100, 40), "fg": (255, 255, 255)}
        return {"text": "ERROR", "bg": (180, 80, 80), "fg": (255, 255, 255)}
    if st.st == "waiting":
        return {"text": "WAIT", "bg": (90, 90, 110), "fg": (230, 230, 240)}
    if st.stale:
        return {"text": "STALE", "bg": (180, 130, 60), "fg": (30, 20, 0)}
    return {"text": "OK", "bg": (60, 130, 90), "fg": (240, 255, 240)}


def _draw_pill(d: ImageDraw.ImageDraw, st: State, x_right: int, y: int) -> None:
    pill = _status_pill(st)
    f = font(11, bold=True)
    text_w = int(d.textlength(pill["text"], font=f))
    pw = text_w + 12
    px = x_right - pw
    d.rounded_rectangle((px, y, px + pw, y + 18), radius=8, fill=pill["bg"])
    d.text((px + 6, y + 2), pill["text"], font=f, fill=pill["fg"])


def _draw_footer(d: ImageDraw.ImageDraw, st: State, width: int, height: int) -> None:
    d.rectangle((0, height - 20, width, height), fill=(20, 22, 30))
    clock = time.strftime("%H:%M")
    f = font(12)
    cw = int(d.textlength(clock, font=f))
    d.text((width - cw - 8, height - 17), clock, font=f, fill=(150, 150, 160))

    age = st.age_s
    if st.ts == 0 or age == float("inf"):
        foot = "no data"
    elif st.st == "error":
        if "401" in (st.err or "") or "authentication" in (st.err or "").lower():
            foot = "auth: run `claude`"
        else:
            foot = "error"
    elif age < 60:
        foot = f"updated {int(age)}s ago"
    elif age < 3600:
        foot = f"updated {int(age/60)}m ago"
    else:
        foot = f"updated {int(age/3600)}h ago"
    if st.stale and st.ts != 0:
        foot = f"STALE — {foot}"
    # Truncate so it doesn't overrun the clock zone.
    max_foot_w = width - cw - 18
    while int(d.textlength(foot, font=f)) > max_foot_w and len(foot) > 4:
        foot = foot[:-2] + "…"
    color = (220, 140, 140) if st.stale or st.st == "error" else (130, 130, 140)
    d.text((8, height - 17), foot, font=f, fill=color)


def _bar_color(pct: Optional[int], lo, mid, hi, none):
    if pct is None:
        return none
    if pct < 75:
        return lo
    if pct < 90:
        return mid
    return hi


def _draw_bar(d: ImageDraw.ImageDraw, x, y, w, h, pct: Optional[int],
              label: str, value_color, label_color=(220, 220, 220)) -> None:
    p = max(0, min(100, pct or 0))
    d.rectangle((x, y, x + w, y + h), outline=(70, 72, 84), width=2)
    fill_w = int((w - 4) * p / 100)
    if fill_w > 0:
        d.rectangle((x + 2, y + 2, x + 2 + fill_w, y + h - 2), fill=value_color)
    d.text((x, y - 22), label, font=font(14), fill=label_color)
    txt = f"{pct}%" if pct is not None else "--"
    tw = int(d.textlength(txt, font=font(16, bold=True)))
    d.text((x + w - tw, y - 24), txt, font=font(16, bold=True), fill=value_color)


def _center_message(d, width, height, msg, color, sub: str = ""):
    f = font(18, bold=True)
    tw = int(d.textlength(msg, font=f))
    d.text(((width - tw) / 2, height / 2 - 16), msg, font=f, fill=color)
    if sub:
        fs = font(12)
        sw = int(d.textlength(sub, font=fs))
        d.text(((width - sw) / 2, height / 2 + 10), sub, font=fs, fill=(160, 160, 170))


# ---------- render: usage screen ----------

CARD_BG = (28, 30, 38)
TEXT_PRIMARY = (240, 240, 240)
TEXT_DIM = (150, 152, 160)
BAR_TRACK = (60, 62, 72)
BAR_FILL = (148, 178, 86)   # olive/green from upstream


def _fmt_resets(mins: Optional[int]) -> str:
    """Format like upstream: "Resets in 2h 22m" / "Resets in 6d 19h" / "Resets in 4m"."""
    if mins is None:
        return "Resets in --"
    if mins <= 0:
        return "Resets now"
    h, m = divmod(int(mins), 60)
    if h >= 24:
        d, h = divmod(h, 24)
        return f"Resets in {d}d {h}h"
    if h:
        return f"Resets in {h}h {m:02d}m"
    return f"Resets in {m}m"


def _bar_fill_color(pct: Optional[int]) -> tuple[int, int, int]:
    if pct is None:
        return BAR_TRACK
    if pct < 75:
        return BAR_FILL
    if pct < 90:
        return (210, 170, 70)
    return (220, 80, 80)


def _draw_card(img: Image.Image, d: ImageDraw.ImageDraw, x, y, w, h,
               pct: Optional[int], pill_text: str, reset_mins: Optional[int],
               anim_mini: Image.Image | None = None) -> None:
    d.rounded_rectangle((x, y, x + w, y + h), radius=14, fill=CARD_BG)

    # Big percentage in serif (matches upstream Tiempos)
    f_pct = font(34, bold=False, serif=True)
    pct_txt = f"{pct}%" if pct is not None else "--%"
    d.text((x + 16, y + 8), pct_txt, font=f_pct, fill=TEXT_PRIMARY)

    # Pill on the right
    f_pill = font(12, bold=False)
    pw = int(d.textlength(pill_text, font=f_pill)) + 16
    px = x + w - pw - 12
    py = y + 16
    d.rounded_rectangle((px, py, px + pw, py + 22), radius=11, fill=(56, 58, 68))
    d.text((px + 8, py + 4), pill_text, font=f_pill, fill=(220, 222, 230))

    # Progress bar
    bar_x = x + 16
    bar_y = y + h - 40
    bar_w = w - 32
    bar_h = 6
    d.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + bar_h), radius=3, fill=BAR_TRACK)
    if pct is not None and pct > 0:
        fill_w = max(bar_h, int(bar_w * min(100, pct) / 100))
        d.rounded_rectangle(
            (bar_x, bar_y, bar_x + fill_w, bar_y + bar_h),
            radius=3, fill=_bar_fill_color(pct),
        )

    # Reset countdown
    f_sub = font(13, bold=False)
    d.text((bar_x, bar_y + 12), _fmt_resets(reset_mins), font=f_sub, fill=TEXT_DIM)


def _draw_usage_header(img: Image.Image, d: ImageDraw.ImageDraw, width: int,
                       anim_mini: Image.Image | None) -> None:
    if anim_mini is not None:
        img.paste(anim_mini, (10, 8))
    title = "Usage"
    f = font(28, bold=False, serif=True)
    tw = int(d.textlength(title, font=f))
    d.text(((width - tw) // 2, 6), title, font=f, fill=TEXT_PRIMARY)
    # Clock instead of battery (upstream has battery; we don't have a battery API yet).
    clock = time.strftime("%H:%M")
    fc = font(12)
    cw = int(d.textlength(clock, font=fc))
    d.text((width - cw - 12, 16), clock, font=fc, fill=TEXT_DIM)


def _usage_status_line(st: State) -> tuple[str, tuple[int, int, int]]:
    if st.st == "waiting":
        return "Waiting for poller…", (200, 200, 210)
    if st.st == "error":
        err = (st.err or "").lower()
        if "401" in err or "authentication" in err or "invalid x-api-key" in err:
            return "* Auth — run `claude`", (230, 160, 70)
        return "* Error — see logs", (230, 100, 100)
    if st.stale:
        return "* Stale", (220, 160, 80)
    if st.st == "limited":
        return "* Rate limited", (230, 90, 90)
    # Match upstream's playful status copy
    worst = max(st.s or 0, st.w or 0)
    if worst < 30:
        return "* Idle", (160, 200, 130)
    if worst < 70:
        return "* Cooking…", (210, 170, 90)
    if worst < 90:
        return "* Sizzling…", (220, 130, 60)
    return "* Almost done!", (230, 90, 70)


def render_usage(width: int, height: int, st: State,
                 animator: Animator | None = None) -> Image.Image:
    img = Image.new("RGB", (width, height), (10, 12, 18))
    d = ImageDraw.Draw(img)

    mini = animator.thumbnail(28) if animator is not None else None
    _draw_usage_header(img, d, width, mini)

    # Two cards
    card_x, card_w = 10, width - 20
    card_h = 96
    y1 = 48
    y2 = y1 + card_h + 8
    _draw_card(img, d, card_x, y1, card_w, card_h, st.s, "Current", st.sr)
    _draw_card(img, d, card_x, y2, card_w, card_h, st.w, "Weekly", st.wr)

    # Footer status line
    text, color = _usage_status_line(st)
    f = font(14, bold=False, serif=True)
    tw = int(d.textlength(text, font=f))
    d.text(((width - tw) // 2, height - 22), text, font=f, fill=color)
    return img


# ---------- render: splash screen ----------

def render_splash(width: int, height: int, st: State, animator: Animator,
                  now: float | None = None) -> Image.Image:
    """Just the sprite, fullscreen, on black. Status info is conveyed by the
    RGB LED — to keep this view as clean as the upstream's Clawd splash."""
    now = time.time() if now is None else now
    img = Image.new("RGB", (width, height), (0, 0, 0))
    sprite_img = animator.tick(now)
    sx = (width - sprite_img.width) // 2
    sy = (height - sprite_img.height) // 2
    img.paste(sprite_img, (sx, sy))
    return img


# ---------- LED color from state ----------

def led_for(st: State) -> tuple[int, int, int]:
    if st.st in ("waiting", "error") or not st.ok:
        return (40, 0, 40)
    if st.stale:
        return (60, 30, 0)
    worst = max(st.s or 0, st.w or 0)
    if st.st == "limited" or worst >= 95:
        return (220, 0, 0)
    if worst >= 90:
        return (220, 30, 0)
    if worst >= 75:
        return (200, 130, 0)
    if worst >= 50:
        return (60, 140, 30)
    return (0, 120, 0)


__all__ = [
    "State", "read_state", "RateTracker",
    "render_usage", "render_splash", "led_for",
    "select_mood",
]
