"""Poll Anthropic API for Claude Code rate-limit headers.

Reads the OAuth access token from ~/.claude/.credentials.json on each poll
(so token refresh by Claude Code itself is picked up transparently), sends a
minimal 1-token request to claude-haiku-4-5, and parses the unified rate-limit
headers into a small JSON state file that other modules read.

Output state file (default /tmp/clawdmeter_state.json):
    {
        "s":  <int>      session utilization %
        "sr": <int>      session reset, minutes from now
        "w":  <int>      weekly utilization %
        "wr": <int>      weekly reset, minutes from now
        "st": <str>      "allowed" | "limited" | "error"
        "ok": <bool>
        "ts": <float>    unix time of this poll
        "err": <str?>    error message if ok is False
    }
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
STATE_PATH = Path(os.environ.get("CLAWDMETER_STATE", "/tmp/clawdmeter_state.json"))
POLL_INTERVAL_S = 60
API_URL = "https://api.anthropic.com/v1/messages"
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
MODEL = "claude-haiku-4-5"
REQUEST_TIMEOUT_S = 15

log = logging.getLogger("clawdmeter.poller")


def load_access_token() -> str:
    with CREDENTIALS_PATH.open("r", encoding="utf-8") as f:
        creds = json.load(f)
    oauth = creds.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        raise RuntimeError(f"no accessToken in {CREDENTIALS_PATH}")
    return token


def refresh_access_token() -> str:
    """Use the stored refresh token to get a new access token and save it."""
    with CREDENTIALS_PATH.open("r", encoding="utf-8") as f:
        creds = json.load(f)
    oauth = creds.get("claudeAiOauth") or {}
    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        raise RuntimeError("no refreshToken available — re-run `claude` to log in")

    resp = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID,
        },
        timeout=REQUEST_TIMEOUT_S,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"token refresh failed {resp.status_code}: {resp.text[:160]}")

    data = resp.json()
    new_token = data.get("access_token")
    if not new_token:
        raise RuntimeError(f"no access_token in refresh response: {data}")

    # Patch only the OAuth fields; leave the rest of credentials.json intact.
    oauth["accessToken"] = new_token
    if "expires_in" in data:
        oauth["expiresAt"] = int((time.time() + data["expires_in"]) * 1000)
    if "refresh_token" in data:
        oauth["refreshToken"] = data["refresh_token"]
    creds["claudeAiOauth"] = oauth

    tmp = CREDENTIALS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    tmp.replace(CREDENTIALS_PATH)
    log.info("access token refreshed")
    return new_token


def _minutes_until(iso_or_epoch: str | None) -> int | None:
    if not iso_or_epoch:
        return None
    try:
        if iso_or_epoch.isdigit():
            target = datetime.fromtimestamp(int(iso_or_epoch), tz=timezone.utc)
        else:
            target = datetime.fromisoformat(iso_or_epoch.replace("Z", "+00:00"))
    except ValueError:
        return None
    delta = (target - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(delta // 60))


def _parse_pct(value: str | None) -> int | None:
    """Parse a utilization header. The API returns fractions in [0,1] (e.g.
    "0.33"), but accept whole-number percents too for forward compatibility."""
    if value is None:
        return None
    try:
        v = float(value)
    except ValueError:
        return None
    if 0.0 <= v <= 1.0:
        v *= 100.0
    return int(round(v))


def parse_headers(headers) -> dict:
    """Pull the unified rate-limit fields out of response headers.

    Header names follow the documented `anthropic-ratelimit-unified-*` family.
    We accept both `5h` and `session`, and both `7d` and `weekly`, since the
    API has used both spellings across versions.
    """
    def first(*names):
        for n in names:
            v = headers.get(n)
            if v is not None:
                return v
        return None

    s = _parse_pct(first(
        "anthropic-ratelimit-unified-5h-utilization",
        "anthropic-ratelimit-unified-session-utilization",
    ))
    sr = _minutes_until(first(
        "anthropic-ratelimit-unified-5h-reset",
        "anthropic-ratelimit-unified-session-reset",
    ))
    w = _parse_pct(first(
        "anthropic-ratelimit-unified-7d-utilization",
        "anthropic-ratelimit-unified-weekly-utilization",
    ))
    wr = _minutes_until(first(
        "anthropic-ratelimit-unified-7d-reset",
        "anthropic-ratelimit-unified-weekly-reset",
    ))
    status = first("anthropic-ratelimit-unified-status") or "allowed"
    return {"s": s, "sr": sr, "w": w, "wr": wr, "st": status}


def poll_once(token: str) -> dict:
    """Make one minimal API call and return parsed state dict."""
    headers = {
        "authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
        "content-type": "application/json",
    }
    body = {
        "model": MODEL,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "."}],
    }
    resp = requests.post(API_URL, headers=headers, json=body, timeout=REQUEST_TIMEOUT_S)
    # The headers carry the data we want even on 429s, so parse before raising.
    parsed = parse_headers(resp.headers)
    if resp.status_code == 429:
        parsed["st"] = "limited"
        parsed["ok"] = True
    elif resp.status_code >= 400:
        return {**parsed, "ok": False, "st": "error",
                "err": f"http {resp.status_code}: {resp.text[:160]}"}
    else:
        parsed["ok"] = True
    return parsed


def write_state(state: dict) -> None:
    state = {**state, "ts": time.time()}
    tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(STATE_PATH)


def run_forever(interval_s: int = POLL_INTERVAL_S) -> None:
    log.info("polling every %ds, state -> %s", interval_s, STATE_PATH)
    while True:
        started = time.time()
        try:
            token = load_access_token()
            state = poll_once(token)
            if not state.get("ok") and "401" in state.get("err", ""):
                log.info("401 — refreshing token")
                token = refresh_access_token()
                state = poll_once(token)
        except Exception as e:
            log.warning("poll failed: %s", e)
            state = {"ok": False, "st": "error", "err": str(e),
                     "s": None, "sr": None, "w": None, "wr": None}
        write_state(state)
        log.info("state: %s", state)
        elapsed = time.time() - started
        time.sleep(max(1.0, interval_s - elapsed))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        run_forever()
    except KeyboardInterrupt:
        sys.exit(0)
