#!/usr/bin/env python3
"""Quota readings for pooled accounts.

Codex is read over the app-server JSON-RPC protocol. Claude has no equivalent
interface, so its usage is scraped from the CLI's `/usage` view, which is
fragile by nature. Every reading is therefore wrapped: a failure yields
state "unknown", never a number, and never triggers a rotation.

The 5-hour window is the primary signal. The weekly window is a backstop so a
week-long exhaustion cannot strand a session that looks fine hour to hour.
"""
from __future__ import annotations

import json
import os
import pty
import re
import select
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codex_app_client import AppServerError, read_rate_limits

SESSION_WINDOW_MINUTES = 300  # the 5-hour window
CLAUDE_SCRAPE_TIMEOUT = 40.0


@dataclass
class Window:
    used_percent: float | None = None
    window_minutes: int | None = None
    resets_at: int | None = None

    @property
    def known(self) -> bool:
        return self.used_percent is not None


@dataclass
class Usage:
    """A quota reading, or an explicit statement that there isn't one."""

    state: str = "unknown"  # ready | unknown
    session: Window = field(default_factory=Window)
    weekly: Window = field(default_factory=Window)
    plan: str | None = None
    detail: str | None = None
    read_at: int = field(default_factory=lambda: int(time.time()))

    @property
    def known(self) -> bool:
        return self.state == "ready" and (self.session.known or self.weekly.known)

    @property
    def decisive_percent(self) -> float | None:
        """The number a threshold is judged against.

        The 5-hour window leads. The weekly window still counts, because being
        out of weekly quota strands the session just as effectively.
        """
        values = [w.used_percent for w in (self.session, self.weekly) if w.known]
        return max(values) if values else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "plan": self.plan,
            "detail": self.detail,
            "readAt": self.read_at,
            "decisivePercent": self.decisive_percent,
            "session": {
                "usedPercent": self.session.used_percent,
                "windowMinutes": self.session.window_minutes,
                "resetsAt": self.session.resets_at,
            },
            "weekly": {
                "usedPercent": self.weekly.used_percent,
                "windowMinutes": self.weekly.window_minutes,
                "resetsAt": self.weekly.resets_at,
            },
        }


def _window(value: object) -> Window:
    if not isinstance(value, dict):
        return Window()
    used = value.get("usedPercent", value.get("used_percent"))
    minutes = value.get("windowDurationMins", value.get("windowMinutes"))
    resets = value.get("resetsAt")
    return Window(
        used_percent=float(used) if isinstance(used, (int, float)) else None,
        window_minutes=int(minutes) if isinstance(minutes, (int, float)) else None,
        resets_at=int(resets) if isinstance(resets, (int, float)) else None,
    )


def codex_usage(config_dir: Path, *, timeout: float = 30.0) -> Usage:
    try:
        limits = read_rate_limits(Path(config_dir), timeout=timeout)
    except (AppServerError, OSError) as exc:
        return Usage(state="unknown", detail=str(exc)[:200])
    primary, secondary = _window(limits.get("primary")), _window(limits.get("secondary"))
    # Identify windows by duration rather than position, so a change in ordering
    # cannot silently swap the 5-hour and weekly readings.
    session, weekly = primary, secondary
    if secondary.window_minutes == SESSION_WINDOW_MINUTES and primary.window_minutes != SESSION_WINDOW_MINUTES:
        session, weekly = secondary, primary
    plan = limits.get("planType")
    return Usage(
        state="ready",
        session=session,
        weekly=weekly,
        plan=plan if isinstance(plan, str) else None,
    )


def _plain(value: str) -> str:
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    value = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", value)
    return re.sub(r"[ \t\n]+", " ", value.replace("\r", "\n"))


def _executable(name: str) -> str:
    search = os.pathsep.join([
        str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin",
        "/usr/local/bin", "/usr/bin", "/bin", os.environ.get("PATH", ""),
    ])
    found = shutil.which(name, path=search)
    if found is None:
        raise RuntimeError(f"{name} CLI is unavailable")
    return found


def claude_usage(config_dir: Path, *, timeout: float = CLAUDE_SCRAPE_TIMEOUT) -> Usage:
    """Read Claude usage by driving the CLI's /usage view.

    There is no documented interface for this, so the scrape is treated as
    best-effort: any failure returns state "unknown" rather than raising, and
    the supervisor holds its last good reading instead of acting on a guess.
    """
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    state_path = Path(config_dir) / ".claude.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(state, dict) and not state.get("hasCompletedOnboarding"):
            state["hasCompletedOnboarding"] = True
            state_path.write_text(json.dumps(state), encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass

    try:
        executable = _executable("claude")
    except RuntimeError as exc:
        return Usage(state="unknown", detail=str(exc))

    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            [executable, "--ax-screen-reader", "--no-chrome", "--permission-mode", "dontAsk"],
            env=env, cwd=str(Path.home()),
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        )
    except OSError as exc:
        os.close(master), os.close(slave)
        return Usage(state="unknown", detail=str(exc))
    os.close(slave)

    output = bytearray()
    started = time.monotonic()
    trusted = chrome_answered = requested = False
    try:
        while time.monotonic() - started < timeout:
            plain = _plain(output.decode(errors="replace"))
            if not trusted and ("Quick safety check" in plain or "Enter y/n" in plain):
                os.write(master, b"y\r"); trusted = True
            if not chrome_answered and "Claude in Chrome extension detected" in plain:
                os.write(master, b"n\r"); chrome_answered = True
            if not requested and ("Claude Code v" in plain or time.monotonic() - started > 8):
                os.write(master, b"/usage\r"); requested = True
            readable, _, _ = select.select([master], [], [], 0.25)
            if readable:
                try:
                    output.extend(os.read(master, 8192))
                except OSError:
                    break
            plain = _plain(output.decode(errors="replace"))
            if requested and "Current week" in plain:
                session = re.search(r"Current\s+session.*?(\d+(?:\.\d+)?)%\s+used", plain)
                week = re.search(r"Current\s+week.*?(\d+(?:\.\d+)?)%\s+used", plain, re.DOTALL)
                if session and week:
                    return Usage(
                        state="ready",
                        session=Window(float(session.group(1)), SESSION_WINDOW_MINUTES),
                        weekly=Window(float(week.group(1)), 10080),
                        plan="subscription",
                    )
        return Usage(state="unknown", detail="usage view did not report in time")
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
        os.close(master)


def read_usage(profile: Any, *, timeout: float | None = None) -> Usage:
    """Read usage for an AccountProfile, whichever provider it is."""
    if profile.provider == "codex":
        return codex_usage(profile.auth_json.parent if profile.auth_json else Path.home() / ".codex",
                           timeout=timeout or 30.0)
    if profile.config_dir is None:
        return Usage(state="unknown", detail="claude account has no config_dir")
    return claude_usage(profile.config_dir, timeout=timeout or CLAUDE_SCRAPE_TIMEOUT)
