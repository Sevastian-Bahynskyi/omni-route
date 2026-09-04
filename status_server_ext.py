#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
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
from account_usage import claude_usage, codex_usage

BRIDGE_ROOT = base.HOME / ".omnigent" / "codex-native"
VENV_PYTHON = base.PATCHED_BASE / "omnigent" / ".venv" / "bin" / "python"
SWITCH_INSTALLED = base.PATCHED_BASE / "switch_provider.py"
SWITCH_LOCAL = Path(__file__).resolve().with_name("switch_provider.py")
_SWITCH_LOCK = threading.Lock()
_REMOTE_LOCK = threading.Lock()
_REMOTE_CACHE_LOCK = threading.Lock()
_REMOTE_CACHE_AT = 0.0
_REMOTE_CACHE: dict[str, Any] | None = None
_USAGE_LOCK = threading.Lock()
_USAGE_REFRESHING = False
_USAGE_CACHE_AT = 0.0
_USAGE_CACHE: dict[str, dict[str, Any]] = {}
SESSION_SELECTION_PATH = base.HOME / ".omnigent" / "dashboard-session-selection.json"


def _runtime_candidates() -> list[tuple[float, dict[str, Any]]]:
    candidates: list[tuple[float, dict[str, Any]]] = []
    if not BRIDGE_ROOT.is_dir():
        return []
    for path in BRIDGE_ROOT.glob("*/codex-account-runtime.json"):
        runtime = base._read_json(path)
        if not isinstance(runtime.get("session_id"), str):
            continue
        try:
            stamp = max(float(runtime.get("updated_at") or 0), path.stat().st_mtime)
        except OSError:
            stamp = float(runtime.get("updated_at") or 0)
        candidates.append((stamp, runtime))
    return sorted(candidates, key=lambda item: item[0], reverse=True)


def _selected_session_id() -> str | None:
    value = base._read_json(SESSION_SELECTION_PATH).get("session_id")
    return value if isinstance(value, str) else None


def _latest_runtime(valid_session_ids: set[str] | None = None) -> dict[str, Any]:
    candidates = _runtime_candidates()
    if valid_session_ids is not None:
        candidates = [
            candidate for candidate in candidates
            if candidate[1].get("session_id") in valid_session_ids
        ]
    selected = _selected_session_id()
    if selected:
        match = next((runtime for _, runtime in candidates if runtime.get("session_id") == selected), None)
        if match is not None:
            return match
    return candidates[0][1] if candidates else {}


async def _session_snapshots() -> list[dict[str, Any]]:
    from omnigent.cli_auth import open_server_client

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    async with open_server_client("http://127.0.0.1:6767") as client:
        for _, runtime in _runtime_candidates()[:50]:
            session_id = runtime.get("session_id")
            if not isinstance(session_id, str) or session_id in seen:
                continue
            seen.add(session_id)
            try:
                response = await client.get(f"/v1/sessions/{session_id}", timeout=2.0)
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                session = response.json()
            except Exception:
                continue
            agent = session.get("agent_name") if isinstance(session, dict) else None
            actual = "claude" if agent == "claude-native-ui" else "codex" if agent == "codex-native-ui" else None
            result.append({
                "sessionId": session_id,
                "title": session.get("title") if isinstance(session.get("title"), str) else session_id,
                "status": session.get("status"),
                "actualProvider": actual,
                "selectedAccount": runtime.get("account_name"),
                "selectedProvider": runtime.get("mode"),
                "phase": runtime.get("phase", "active"),
            })
    return result


def _routed_sessions() -> list[dict[str, Any]]:
    try:
        return asyncio.run(_session_snapshots())
    except Exception:
        return []


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


def _refresh_usage() -> None:
    global _USAGE_CACHE_AT, _USAGE_CACHE, _USAGE_REFRESHING
    configured = base._configured_accounts(base._read_json(base.CONFIG_PATH))
    refreshed: dict[str, dict[str, Any]] = {}
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def query(account: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        name = str(account.get("name"))
        try:
            if account.get("provider") == "codex":
                auth = Path(str(account.get("auth_json"))).expanduser()
                value = codex_usage(auth.parent)
            else:
                directory = Path(str(account.get("config_dir"))).expanduser()
                value = claude_usage(directory)
            return name, value
        except Exception as exc:
            return name, {"state": "error", "detail": str(exc)}

    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(query, account) for account in configured]
            for future in as_completed(futures):
                name, value = future.result()
                refreshed[name] = value
        with _USAGE_LOCK:
            _USAGE_CACHE = refreshed
            _USAGE_CACHE_AT = time.monotonic()
    finally:
        with _USAGE_LOCK:
            _USAGE_REFRESHING = False


def _usage_snapshot() -> dict[str, dict[str, Any]]:
    global _USAGE_REFRESHING
    with _USAGE_LOCK:
        stale = time.monotonic() - _USAGE_CACHE_AT > 300
        if stale and not _USAGE_REFRESHING:
            _USAGE_REFRESHING = True
            threading.Thread(target=_refresh_usage, name="omni-route-usage", daemon=True).start()
        return dict(_USAGE_CACHE)


_original_collect_status = base.collect_status


def collect_status() -> dict[str, Any]:
    data = _original_collect_status()
    sessions = _routed_sessions()
    runtime = _latest_runtime({session["sessionId"] for session in sessions})
    mode = runtime.get("mode") if isinstance(runtime.get("mode"), str) else None
    session_id = runtime.get("session_id") if isinstance(runtime.get("session_id"), str) else None
    if mode in {"codex", "claude", "claude_pending", "switch_pending"} and isinstance(runtime.get("account_name"), str):
        provider = runtime["account_name"]
    elif mode in {"claude", "claude_pending"}:
        provider = next((account["name"] for account in data.get("accounts", []) if account.get("provider") == "claude"), None)
    else:
        provider = None

    router = data.setdefault("router", {})
    router["currentProvider"] = provider
    router["runtimeMode"] = mode
    router["sessionId"] = session_id
    router["phase"] = runtime.get("phase", "active")
    data["sessions"] = sessions
    current_session = next((session for session in sessions if session["sessionId"] == session_id), None)
    router["actualProvider"] = current_session.get("actualProvider") if current_session else None
    router["sessionTitle"] = current_session.get("title") if current_session else None

    usage = _usage_snapshot()
    for account in data.get("accounts", []):
        if not isinstance(account, dict):
            continue
        is_current = provider == account.get("name")
        account["current"] = is_current
        if is_current and account.get("status") == "ready":
            account["status"] = "active"
        elif not is_current and account.get("status") == "active":
            account["status"] = "ready"
        account["usage"] = usage.get(str(account.get("name")), {"state": "loading"})

    route = data.get("accounts", [])
    current_index = next((index for index, account in enumerate(route) if account.get("name") == provider), -1)
    current_kind = next((account.get("provider") for account in route if account.get("name") == provider), None)
    next_account = None
    if route:
        available = [
            route[(current_index + offset) % len(route)]
            for offset in range(1, len(route) + 1)
            if route[(current_index + offset) % len(route)].get("status") in {"ready", "active"}
            and route[(current_index + offset) % len(route)].get("name") != provider
        ]
        same_provider = [candidate for candidate in available if candidate.get("provider") == current_kind]
        candidates = same_provider or available
        if candidates:
            next_account = candidates[0].get("name")
    router["nextAccount"] = next_account

    install = data.setdefault("install", {})
    install["sessionImporter"] = (base.PATCHED_BASE / "import_sessions.py").is_file()
    install["tailscale"] = bool(_remote_status().get("installed"))
    data["remoteAccess"] = _remote_status()
    return data


base.collect_status = collect_status


def _run_switch(provider: str, session_id: str | None = None) -> dict[str, Any]:
    script = SWITCH_INSTALLED if SWITCH_INSTALLED.is_file() else SWITCH_LOCAL
    if not script.is_file():
        return {"ok": False, "error": "switch_provider.py not found"}
    python = str(VENV_PYTHON if VENV_PYTHON.is_file() else (shutil.which("python3") or "python3"))
    try:
        result = subprocess.run(
            [python, str(script), provider, *(["--session", session_id] if session_id else [])],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            timeout=150,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "provider switch timed out"}
    except OSError:
        return {"ok": False, "error": "could not start account switch"}
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
    return {"ok": False, "error": "account switch failed; run omni-rotate test for diagnostics"}


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
        if path == "/api/session/current":
            return self._select_session()
        if path != "/api/provider/current":
            return super().do_POST()
        if not self._same_origin():
            return self._send_json({"ok": False, "error": "forbidden"}, 403)
        try:
            request = self._json_body()
            provider = request.get("provider")
            session_id = request.get("sessionId")
            if not isinstance(provider, str) or not provider.strip():
                raise ValueError("provider must be a non-empty string")
            if session_id is not None and (not isinstance(session_id, str) or not session_id.strip()):
                raise ValueError("sessionId must be a non-empty string")
            configured = base._configured_accounts(base._read_json(base.CONFIG_PATH))
            if provider.strip() not in {account.get("name") for account in configured}:
                raise ValueError("unknown subscription account")
        except (ValueError, json.JSONDecodeError) as exc:
            return self._send_json({"ok": False, "error": str(exc)}, 400)
        if not _SWITCH_LOCK.acquire(blocking=False):
            return self._send_json({"ok": False, "error": "provider switch already in progress"}, 409)
        try:
            payload = _run_switch(provider.strip(), session_id.strip() if isinstance(session_id, str) else None)
        finally:
            _SWITCH_LOCK.release()
        self._send_json(payload, 200 if payload.get("ok") else 409)

    def _select_session(self) -> None:
        if not self._same_origin():
            return self._send_json({"ok": False, "error": "forbidden"}, 403)
        try:
            request = self._json_body()
            session_id = request.get("sessionId")
            if not isinstance(session_id, str) or not session_id.strip():
                raise ValueError("sessionId must be a non-empty string")
            if session_id not in {session["sessionId"] for session in _routed_sessions()}:
                raise ValueError("unknown routed session")
            base._atomic_write_json(SESSION_SELECTION_PATH, {"session_id": session_id})
        except (ValueError, json.JSONDecodeError) as exc:
            return self._send_json({"ok": False, "error": str(exc)}, 400)
        self._send_json({"ok": True, "status": collect_status()}, 200)

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
