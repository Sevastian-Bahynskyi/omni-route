#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import remote_access
import status_server as base

BRIDGE_ROOT = base.HOME / ".omnigent" / "codex-native"
VENV_PYTHON = base.PATCHED_BASE / "omnigent" / ".venv" / "bin" / "python"
SWITCH_INSTALLED = base.PATCHED_BASE / "switch_provider.py"
SWITCH_LOCAL = Path(__file__).resolve().with_name("switch_provider.py")
_SWITCH_LOCK = threading.Lock()
_REMOTE_LOCK = threading.Lock()
_REMOTE_CACHE_LOCK = threading.Lock()
_REMOTE_CACHE_AT = 0.0
_REMOTE_CACHE: dict[str, Any] | None = None


def _latest_runtime() -> dict[str, Any]:
    candidates: list[tuple[float, dict[str, Any]]] = []
    if not BRIDGE_ROOT.is_dir():
        return {}
    for path in BRIDGE_ROOT.glob("*/codex-account-runtime.json"):
        runtime = base._read_json(path)
        if not isinstance(runtime.get("session_id"), str):
            continue
        try:
            stamp = max(float(runtime.get("updated_at") or 0), path.stat().st_mtime)
        except OSError:
            stamp = float(runtime.get("updated_at") or 0)
        candidates.append((stamp, runtime))
    return max(candidates, key=lambda item: item[0])[1] if candidates else {}


def _remote_status(*, force: bool = False) -> dict[str, Any]:
    global _REMOTE_CACHE_AT, _REMOTE_CACHE
    now = time.monotonic()
    with _REMOTE_CACHE_LOCK:
        if not force and _REMOTE_CACHE is not None and now - _REMOTE_CACHE_AT < 2.5:
            return dict(_REMOTE_CACHE)
        value = remote_access.status()
        _REMOTE_CACHE = dict(value)
        _REMOTE_CACHE_AT = time.monotonic()
        return value


_original_collect_status = base.collect_status


def collect_status() -> dict[str, Any]:
    data = _original_collect_status()
    runtime = _latest_runtime()
    mode = runtime.get("mode") if isinstance(runtime.get("mode"), str) else None
    session_id = runtime.get("session_id") if isinstance(runtime.get("session_id"), str) else None
    if mode == "codex" and isinstance(runtime.get("account_name"), str):
        provider = runtime["account_name"]
    elif mode in {"claude", "claude_pending"}:
        provider = "claude"
    else:
        provider = None

    router = data.setdefault("router", {})
    router["currentProvider"] = provider
    router["runtimeMode"] = mode
    router["sessionId"] = session_id

    for account in data.get("accounts", []):
        if not isinstance(account, dict):
            continue
        is_current = provider == account.get("name")
        account["current"] = is_current
        if is_current and account.get("status") == "ready":
            account["status"] = "active"
        elif not is_current and account.get("status") == "active":
            account["status"] = "ready"

    claude = data.setdefault("claude", {})
    claude["current"] = provider == "claude"
    install = data.setdefault("install", {})
    install["sessionImporter"] = (base.PATCHED_BASE / "import_sessions.py").is_file()
    install["tailscale"] = bool(_remote_status().get("installed"))
    data["remoteAccess"] = _remote_status()
    return data


base.collect_status = collect_status


def _run_switch(provider: str) -> dict[str, Any]:
    script = SWITCH_INSTALLED if SWITCH_INSTALLED.is_file() else SWITCH_LOCAL
    if not script.is_file():
        return {"ok": False, "error": "switch_provider.py not found"}
    python = str(VENV_PYTHON if VENV_PYTHON.is_file() else (shutil.which("python3") or "python3"))
    try:
        result = subprocess.run(
            [python, str(script), provider],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            timeout=150,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "provider switch timed out"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if lines:
        try:
            payload = json.loads(lines[-1])
            if isinstance(payload, dict):
                if payload.get("ok"):
                    payload["status"] = collect_status()
                return payload
        except json.JSONDecodeError:
            pass
    return {"ok": False, "error": result.stdout.strip()[-1000:] or f"exit {result.returncode}"}


class ControlHandler(base.StatusHandler):
    server_version = "OmniRouteStatus/1.4"

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return origin in {
            f"http://{host}",
            f"https://{host}",
            f"http://localhost:{self.server.server_port}",
            f"http://127.0.0.1:{self.server.server_port}",
        }

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in {"/api/remote-access/enable", "/api/remote-access/disable"}:
            return self._remote_access_action(path)
        if path != "/api/provider/current":
            return super().do_POST()
        if not self._same_origin():
            return self._send_json({"ok": False, "error": "forbidden"}, 403)
        try:
            request = self._json_body()
            provider = request.get("provider")
            if not isinstance(provider, str) or not provider.strip():
                raise ValueError("provider must be a non-empty string")
        except (ValueError, json.JSONDecodeError) as exc:
            return self._send_json({"ok": False, "error": str(exc)}, 400)
        if not _SWITCH_LOCK.acquire(blocking=False):
            return self._send_json({"ok": False, "error": "provider switch already in progress"}, 409)
        try:
            payload = _run_switch(provider.strip())
        finally:
            _SWITCH_LOCK.release()
        self._send_json(payload, 200 if payload.get("ok") else 409)

    def _remote_access_action(self, path: str) -> None:
        if not self._same_origin():
            return self._send_json({"ok": False, "error": "forbidden"}, 403)
        if not _REMOTE_LOCK.acquire(blocking=False):
            return self._send_json({"ok": False, "error": "remote access update already in progress"}, 409)
        try:
            payload = remote_access.enable() if path.endswith("/enable") else remote_access.disable()
            remote = payload.get("remoteAccess") if isinstance(payload, dict) else None
            if isinstance(remote, dict):
                global _REMOTE_CACHE_AT, _REMOTE_CACHE
                with _REMOTE_CACHE_LOCK:
                    _REMOTE_CACHE = dict(remote)
                    _REMOTE_CACHE_AT = time.monotonic()
            if isinstance(payload, dict) and payload.get("ok"):
                payload["status"] = collect_status()
        finally:
            _REMOTE_LOCK.release()
        self._send_json(payload, 200 if payload.get("ok") else 409)

    def _send_json(self, payload: dict[str, Any], status: int) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._headers("application/json; charset=utf-8", len(body), status)
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Omni Route localhost control dashboard")
    parser.add_argument("--port", type=int, default=base.DEFAULT_PORT)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    if not (1 <= args.port <= 65535):
        parser.error("--port must be between 1 and 65535")
    server = ThreadingHTTPServer((base.HOST, args.port), ControlHandler)
    url = f"http://{base.HOST}:{args.port}/"
    print(f"Omni Route control dashboard: {url}")
    print("Ctrl+C to stop.")
    if not args.no_open:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
