#!/usr/bin/env python3
"""Minimal Codex app-server client.

Speaks newline-delimited JSON-RPC to `codex app-server` over stdio. This
replaces the previous dependency on ``omnigent.codex_native_app_server`` so
account and quota reads work without an Omnigent checkout.

The app-server is started with ``cli_auth_credentials_store="file"`` so pooled
account profiles are read from ``$CODEX_HOME/auth.json`` and the macOS Keychain
cannot silently override the selected account.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

CLIENT_NAME = "omni-route"
CLIENT_VERSION = "0.1.0"
DEFAULT_TIMEOUT = 30.0


class AppServerError(RuntimeError):
    """The app-server could not be started or returned an error."""


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
    found = shutil.which(name, path=search_path)
    if found is None:
        raise AppServerError(f"{name} CLI is unavailable")
    return found


class AppServerClient:
    """Context manager around a short-lived ``codex app-server`` process."""

    def __init__(self, config_dir: Path, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._config_dir = Path(config_dir)
        self._timeout = timeout
        self._process: subprocess.Popen[str] | None = None
        self._replies: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._next_id = 0
        self._reader: threading.Thread | None = None
        self._stderr: list[str] = []

    def __enter__(self) -> "AppServerClient":
        env = dict(os.environ)
        env["CODEX_HOME"] = str(self._config_dir)
        self._process = subprocess.Popen(
            [
                _executable("codex"),
                "app-server",
                "-c",
                'cli_auth_credentials_store="file"',
            ],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": CLIENT_NAME,
                    "title": "Omni Route",
                    "version": CLIENT_VERSION,
                }
            },
        )
        self.notify("initialized", {})
        return self

    def __exit__(self, *_exc: object) -> None:
        process = self._process
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _read_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            message_id = message.get("id")
            # Server-initiated notifications carry no id and are ignored.
            if not isinstance(message_id, int):
                continue
            with self._lock:
                pending = self._replies.get(message_id)
            if pending is not None:
                pending.put(message)

    def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        for line in self._process.stderr:
            self._stderr.append(line.rstrip())
            del self._stderr[:-40]

    def _write(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise AppServerError("app-server is not running")
        try:
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise AppServerError(self._failure_detail()) from exc

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            message_id = self._next_id
            inbox: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            self._replies[message_id] = inbox
        try:
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "method": method,
                    "params": params,
                }
            )
            try:
                message = inbox.get(timeout=self._timeout)
            except queue.Empty:
                raise AppServerError(
                    f"timed out after {self._timeout:g}s waiting for {method}"
                    f"{self._failure_detail(prefix='; ')}"
                ) from None
        finally:
            with self._lock:
                self._replies.pop(message_id, None)
        if "error" in message:
            error = message["error"]
            detail = error.get("message") if isinstance(error, dict) else error
            raise AppServerError(f"{method} failed: {detail}")
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    def _failure_detail(self, *, prefix: str = "") -> str:
        process = self._process
        if process is not None and process.poll() is not None:
            tail = " | ".join(self._stderr[-3:])
            detail = f"app-server exited with code {process.returncode}"
            return f"{prefix}{detail}: {tail}" if tail else f"{prefix}{detail}"
        return ""


def read_account(config_dir: Path, *, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Return the account the given CODEX_HOME is authenticated as."""
    with AppServerClient(config_dir, timeout=timeout) as client:
        result = client.request("account/read", {})
    account = result.get("account")
    if not isinstance(account, dict):
        raise AppServerError("app-server did not report an account")
    return account


def read_rate_limits(config_dir: Path, *, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Return raw rate-limit windows for the given CODEX_HOME."""
    with AppServerClient(config_dir, timeout=timeout) as client:
        result = client.request("account/rateLimits/read", {})
    limits = result.get("rateLimits")
    if not isinstance(limits, dict):
        raise AppServerError("Codex did not return usage limits")
    return limits


if __name__ == "__main__":
    import sys

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / ".codex"
    print(json.dumps({"account": read_account(target)}, indent=2))
