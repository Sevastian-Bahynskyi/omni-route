#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import pty
import re
import select
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any


def _executable(name: str) -> str:
    search_path = os.pathsep.join(
        [
            str(Path.home() / ".local" / "bin"),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            os.environ.get("PATH", ""),
        ]
    )
    executable = shutil.which(name, path=search_path)
    if executable is None:
        raise RuntimeError(f"{name} CLI is unavailable")
    return executable


def _free_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def codex_usage(config_dir: Path) -> dict[str, Any]:
    async def read() -> dict[str, Any]:
        from omnigent.codex_native_app_server import client_for_transport

        url = f"ws://127.0.0.1:{_free_port()}"
        env = dict(os.environ)
        env["CODEX_HOME"] = str(config_dir)
        process = subprocess.Popen(
            [_executable("codex"), "app-server", "--listen", url, "-c", 'cli_auth_credentials_store="file"'],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            client = client_for_transport(url, client_name="omni-route-quota")
            for _ in range(40):
                try:
                    await client.connect()
                    break
                except Exception:
                    await asyncio.sleep(0.1)
            else:
                raise RuntimeError("Codex quota service did not start")
            try:
                response = await client.request("account/rateLimits/read", {})
            finally:
                await client.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        result = response.get("result") if isinstance(response, dict) else None
        limits = result.get("rateLimits") if isinstance(result, dict) else None
        if not isinstance(limits, dict):
            raise RuntimeError("Codex did not return usage limits")
        return {
            "state": "ready",
            "primary": _codex_window(limits.get("primary")),
            "secondary": _codex_window(limits.get("secondary")),
            "plan": limits.get("planType"),
        }

    return asyncio.run(read())


def _codex_window(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    used = value.get("usedPercent")
    resets = value.get("resetsAt")
    return {
        "usedPercent": float(used) if isinstance(used, (int, float)) else None,
        "remainingPercent": max(0.0, 100.0 - float(used)) if isinstance(used, (int, float)) else None,
        "resetsAt": int(resets) if isinstance(resets, (int, float)) else None,
        "windowMinutes": value.get("windowDurationMins"),
    }


def claude_usage(config_dir: Path) -> dict[str, Any]:
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    state_path = config_dir / ".claude.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            state["hasCompletedOnboarding"] = True
            state.setdefault("theme", "dark")
            state_path.write_text(json.dumps(state), encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass

    master, slave = pty.openpty()
    process = subprocess.Popen(
        [_executable("claude"), "--ax-screen-reader", "--no-chrome", "--permission-mode", "dontAsk"],
        env=env,
        cwd=str(Path.home()),
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    output = bytearray()
    started = time.monotonic()
    trusted = False
    chrome_answered = False
    requested = False
    try:
        while time.monotonic() - started < 24:
            elapsed = time.monotonic() - started
            plain = _plain(output.decode(errors="replace"))
            if not trusted and ("Quick safety check" in plain or "Enter y/n" in plain):
                os.write(master, b"y\r")
                trusted = True
            if not chrome_answered and "Claude in Chrome extension detected" in plain:
                os.write(master, b"n\r")
                chrome_answered = True
            if not requested and ("Claude Code v" in plain or elapsed > 8):
                os.write(master, b"/usage\r")
                requested = True
            readable, _, _ = select.select([master], [], [], 0.25)
            if readable:
                try:
                    output.extend(os.read(master, 8192))
                except OSError:
                    break
            plain = _plain(output.decode(errors="replace"))
            if requested and "Current week" in plain and "used" in plain:
                session = re.search(r"Current\s+session.*?(\d+(?:\.\d+)?)%\s+used", plain)
                week = re.search(r"Current\s+week.*?(\d+(?:\.\d+)?)%\s+used", plain, re.DOTALL)
                if session and week:
                    return {
                        "state": "ready",
                        "primary": _percent_window(float(session.group(1))),
                        "secondary": _percent_window(float(week.group(1))),
                        "plan": "subscription",
                    }
        raise RuntimeError("Claude usage view did not finish")
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
        os.close(master)


def _percent_window(used: float) -> dict[str, Any]:
    return {"usedPercent": used, "remainingPercent": max(0.0, 100.0 - used)}


def _plain(value: str) -> str:
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    value = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", value)
    value = value.replace("\r", "\n")
    return re.sub(r"[ \t\n]+", " ", value)
