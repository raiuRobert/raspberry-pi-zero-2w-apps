"""One-time sprite converter for Clawdmeter.

Downloads upstream HermannBjorgvin/Clawdmeter splash_animations.h, parses the
palettized 20x20 frame data, and writes:

    assets/sprites/<animation_name>/000.png ... NNN.png    (RGB)
    assets/sprites/index.json                              (metadata)

The header file format (one block per animation):
    static const uint16_t splash_<name>_palette[10]      = {0x.., ...};
    static const uint8_t  splash_<name>_frames[N][400]   = { {...}, ... };
    static const uint16_t splash_<name>_holds[N]         = {ms, ms, ...};

Run from the project root:
    python tools/convert_sprites.py

This is run once on the dev machine. The generated assets get shipped to the
Pi via the install script.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

from PIL import Image

UPSTREAM_URL = (
    "https://raw.githubusercontent.com/HermannBjorgvin/Clawdmeter/"
    "main/firmware/src/splash_animations.h"
)
ROOT = Path(__file__).resolve().parent.parent
SPRITES_DIR = ROOT / "assets" / "sprites"

FRAME_SIZE = 20  # 20x20
PALETTE_LEN = 10


def fetch_header() -> str:
    print(f"fetching {UPSTREAM_URL}")
    with urllib.request.urlopen(UPSTREAM_URL, timeout=30) as r:
        return r.read().decode("utf-8")


def rgb565_to_rgb(c: int) -> tuple[int, int, int]:
    r5 = (c >> 11) & 0x1F
    g6 = (c >> 5) & 0x3F
    b5 = c & 0x1F
    # Expand to 8-bit using bit replication (standard RGB565 -> RGB888).
    r = (r5 << 3) | (r5 >> 2)
    g = (g6 << 2) | (g6 >> 4)
    b = (b5 << 3) | (b5 >> 2)
    return r, g, b


_PALETTE_RE = re.compile(
    r"static\s+const\s+uint16_t\s+splash_(\w+?)_palette\s*\[\s*10\s*\]\s*="
    r"\s*\{([^}]*)\}\s*;",
    re.DOTALL,
)
_FRAMES_RE = re.compile(
    r"static\s+const\s+uint8_t\s+splash_(\w+?)_frames\s*\[\s*(\d+)\s*\]\s*\[\s*400\s*\]\s*="
    r"\s*\{(.*?)\}\s*;",
    re.DOTALL,
)
_HOLDS_RE = re.compile(
    r"static\s+const\s+uint16_t\s+splash_(\w+?)_holds\s*\[\s*\d+\s*\]\s*="
    r"\s*\{([^}]*)\}\s*;",
    re.DOTALL,
)
_FRAME_BODY_RE = re.compile(r"\{([^{}]*)\}")
_NUM_RE = re.compile(r"-?\w+")


def _parse_int_list(text: str) -> list[int]:
    return [int(t, 0) for t in _NUM_RE.findall(text)]


def parse_header(src: str) -> dict[str, dict]:
    palettes: dict[str, list[int]] = {}
    frames: dict[str, list[list[int]]] = {}
    holds: dict[str, list[int]] = {}

    for m in _PALETTE_RE.finditer(src):
        name = m.group(1)
        vals = _parse_int_list(m.group(2))
        if len(vals) != PALETTE_LEN:
            raise ValueError(f"{name}: palette has {len(vals)} entries, expected {PALETTE_LEN}")
        palettes[name] = vals

    for m in _FRAMES_RE.finditer(src):
        name, n_str, body = m.group(1), m.group(2), m.group(3)
        n = int(n_str)
        frame_blocks = _FRAME_BODY_RE.findall(body)
        if len(frame_blocks) != n:
            raise ValueError(f"{name}: declared {n} frames, parsed {len(frame_blocks)}")
        parsed: list[list[int]] = []
        for i, fb in enumerate(frame_blocks):
            vals = _parse_int_list(fb)
            if len(vals) != FRAME_SIZE * FRAME_SIZE:
                raise ValueError(f"{name} frame {i}: {len(vals)} pixels, expected 400")
            parsed.append(vals)
        frames[name] = parsed

    for m in _HOLDS_RE.finditer(src):
        name = m.group(1)
        holds[name] = _parse_int_list(m.group(2))

    out: dict[str, dict] = {}
    for name in sorted(palettes):
        if name not in frames or name not in holds:
            print(f"  WARN: {name} missing frames or holds, skipping", file=sys.stderr)
            continue
        if len(frames[name]) != len(holds[name]):
            print(
                f"  WARN: {name} has {len(frames[name])} frames but {len(holds[name])} holds, skipping",
                file=sys.stderr,
            )
            continue
        out[name] = {
            "palette": palettes[name],
            "frames": frames[name],
            "holds": holds[name],
        }
    return out


def category_of(name: str) -> str:
    return name.split("_", 1)[0]  # idle / expression / dance


def render_frame(frame_indices: list[int], palette_rgb565: list[int]) -> Image.Image:
    rgb_palette = [rgb565_to_rgb(c) for c in palette_rgb565]
    img = Image.new("RGB", (FRAME_SIZE, FRAME_SIZE), (0, 0, 0))
    px = img.load()
    for i, idx in enumerate(frame_indices):
        if 0 <= idx < PALETTE_LEN:
            px[i % FRAME_SIZE, i // FRAME_SIZE] = rgb_palette[idx]
    return img


def main() -> int:
    src = fetch_header()
    anims = parse_header(src)
    print(f"parsed {len(anims)} animations: {sorted(anims)}")

    SPRITES_DIR.mkdir(parents=True, exist_ok=True)
    index: dict[str, dict] = {}
    for name, data in anims.items():
        out_dir = SPRITES_DIR / name
        out_dir.mkdir(parents=True, exist_ok=True)
        frame_paths = []
        for i, frame in enumerate(data["frames"]):
            img = render_frame(frame, data["palette"])
            rel = f"{name}/{i:03d}.png"
            img.save(SPRITES_DIR / rel, "PNG")
            frame_paths.append(rel)
        index[name] = {
            "group": category_of(name),
            "frames": frame_paths,
            "holds_ms": data["holds"],
        }
        print(f"  {name}: {len(frame_paths)} frames, group={category_of(name)}")

    (SPRITES_DIR / "index.json").write_text(
        json.dumps(index, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"\nwrote {len(index)} animations to {SPRITES_DIR}")
    groups: dict[str, list[str]] = {}
    for name, meta in index.items():
        groups.setdefault(meta["group"], []).append(name)
    for g, names in sorted(groups.items()):
        print(f"  {g}: {len(names)} ({', '.join(names)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
