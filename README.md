# Raspberry Pi Zero 2W Apps

Apps built for the **Raspberry Pi Zero 2W** paired with the **PiSugar Whisplay HAT** — a compact HAT with a 240×280 colour LCD, RGB LED, and a single button.

A launcher menu boots on startup. Each app runs as a subprocess under the launcher and returns to the menu when it exits.

---

## Hardware

| Component | Details |
|---|---|
| Board | Raspberry Pi Zero 2W |
| HAT | PiSugar Whisplay HAT |
| Display | 240×280 ST7789 SPI LCD |
| Input | Single push button |
| LED | RGB LED |

---

## Apps

### Claude Meter

Monitors your [Claude Code](https://github.com/anthropics/claude-code) token usage in real time directly on the display. No phone, no browser — just glance at the Pi.

**What it shows:**
- Session utilisation % (5-hour rolling window)
- Weekly utilisation %
- Time until each limit resets
- Live status (OK / Rate Limited / Auth error / Stale)

**How it works:**
- Polls the Anthropic API every 60 seconds using the OAuth token stored by Claude Code in `~/.claude/.credentials.json`
- Automatically refreshes expired tokens — no manual intervention needed
- Drives 13 sprite animations (idle → working → sizzling → almost done!) that reflect your current usage rate
- RGB LED colour tracks usage level (green → amber → red)

**Screens (toggle with button tap):**
- **Splash** — fullscreen animated Claude sprite
- **Usage** — card layout with progress bars and reset countdowns

**Sprites** are ported from [HermannBjorgvin/Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter) (original ESP32 + BLE version).

---

## Launcher

`launcher.py` is the boot entry point. It shows a scrollable app menu and manages launching/returning.

**Boot flow:**

1. systemd starts `launcher.py`
2. Launcher draws the app menu on screen and waits for button input
3. Short tap cycles through apps; hold 2s launches the selected one
4. Launcher calls `board.cleanup()` to release the SPI/GPIO hardware, then runs the app as a subprocess — blocking until it exits
5. The app inits its own board, runs, and exits when done
6. Launcher re-inits the board and shows the menu again

**Button controls:**

| Action | Result |
|---|---|
| Quick tap (< 2s) | Next item in menu |
| Hold >= 2s | Launch selected app (fires at the 2s mark, no need to release) |
| Hold >= 10s (inside any app) | Exit app and return to launcher menu |

Long names in the menu scroll horizontally when selected.

Each app gets exclusive ownership of the hardware for its lifetime. This keeps GPIO state clean between apps and means a crashing app can never affect the launcher.

---

## Project Structure

```
launcher.py              Boot entry point — menu UI and app lifecycle
menu_display.py          Renders the launcher menu screen (PIL)
requirements.txt         Python dependencies
apps/
  claude_meter/
    main.py              App entry point — display loop and poller supervisor
    api_poller.py        Polls Anthropic API, writes /tmp/clawdmeter_state.json
    display.py           Renders splash and usage screens (PIL)
    display_util.py      Shared PIL utilities (fonts, RGB565 conversion)
    animations.py        Sprite animation engine — loads and ticks frame sequences
    assets/sprites/      13 animations extracted from upstream Clawdmeter firmware
tools/
  convert_sprites.py     Extracts sprites from upstream .h file
  probe_driver.py        Tests the Whisplay HAT directly
  display_test.py        Smoke-tests the display
```

### How Claude Meter works internally

`main.py` and `api_poller.py` run as separate processes:

- `api_poller.py` is spawned as a child process by `main.py`. It polls the Anthropic API every 60s and writes the result to `/tmp/clawdmeter_state.json`
- `main.py` reads that file every 0.5s and redraws the screen — the display loop and network calls never block each other
- A supervisor in `main.py` watches the poller and restarts it on crash, giving up after 5 restarts in 60s to avoid a crash loop

---

## Setup

### Dependencies

```bash
sudo apt install python3-pip python3-numpy python3-pil fonts-dejavu
pip3 install requests spidev
```

The Whisplay HAT driver (`WhisPlay.py`) must be at `~/Whisplay/Driver/WhisPlay.py`.

### Deploy

```bash
# From your dev machine
scp -r launcher.py menu_display.py requirements.txt apps/ tools/ user@raspberrypi:~/clawdmeter/
```

### Run manually

```bash
python3 ~/clawdmeter/launcher.py
```

### Run on boot (systemd user service)

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/launcher.service << 'EOF'
[Unit]
Description=Pi Zero Apps Launcher
After=default.target

[Service]
Type=simple
WorkingDirectory=/home/rraiu/clawdmeter
ExecStart=/usr/bin/python3 /home/rraiu/clawdmeter/launcher.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable launcher.service
loginctl enable-linger $USER
```

### Authentication

Claude Meter reads the OAuth token that Claude Code manages. Just run Claude Code on the Pi at least once to log in:

```bash
claude
```

After that the poller refreshes tokens automatically — no further action needed even after reboots.

---

## Adding More Apps

Create a folder under `apps/` for your new app, then add it to the `APPS` list at the top of `launcher.py`:

```python
APPS = [
    {"name": "Claude Meter", "script": str(ROOT / "apps" / "claude_meter" / "main.py")},
    {"name": "Your App",     "script": str(ROOT / "apps" / "your_app" / "main.py")},
]
```

Each app should call `board.cleanup()` on exit so the launcher can re-initialise the display.

---

## Credits

Sprite artwork and animation data from [HermannBjorgvin/Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter) — ported from ESP32 firmware to Python.
