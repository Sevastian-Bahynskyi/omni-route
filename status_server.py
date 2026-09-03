#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HOST = "127.0.0.1"
DEFAULT_PORT = 8787
HOME = Path.home()
CONFIG_PATH = HOME / ".omnigent" / "codex-account-pool.json"
STATE_PATH = HOME / ".omnigent" / "codex-account-pool-state.json"
PATCHED_BASE = HOME / ".local" / "share" / "omnigent-subscription-rotation"
PATCHED_LAUNCHER = HOME / ".local" / "bin" / "omni-rotate"
DIAG_INSTALLED = PATCHED_BASE / "diagnose.py"
DIAG_LOCAL = Path(__file__).resolve().with_name("diagnose.py")
_DIAG_LOCK = threading.Lock()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _jwt_payload(token: object) -> dict[str, Any] | None:
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    try:
        segment = token.split(".")[1]
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        value = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _find_email(value: object) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold().replace("-", "_") in {"email", "user_email"}:
                if isinstance(item, str) and item.strip():
                    return item.strip()
        for item in value.values():
            found = _find_email(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_email(item)
            if found:
                return found
    return None


def _email_from_auth(path: Path | None) -> str | None:
    if path is None:
        return None
    data = _read_json(path)
    email = _find_email(data)
    if email:
        return email

    def walk(value: object) -> str | None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).casefold().replace("-", "_")
                if normalized in {"id_token", "access_token"}:
                    payload = _jwt_payload(item)
                    found = _find_email(payload) if payload else None
                    if found:
                        return found
                found = walk(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = walk(item)
                if found:
                    return found
        return None

    return walk(data)


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def update_route_order(names: list[str]) -> dict[str, Any]:
    config = _read_json(CONFIG_PATH)
    accounts = config.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("no Codex accounts are configured")

    configured: dict[str, dict[str, Any]] = {}
    for item in accounts:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("account pool configuration is invalid")
        name = item["name"]
        if name in configured:
            raise ValueError("account names are not unique")
        configured[name] = item

    if len(names) != len(configured) or len(set(names)) != len(names):
        raise ValueError("route order must contain every configured account exactly once")
    if set(names) != set(configured):
        raise ValueError("route order contains unknown or missing accounts")

    config["accounts"] = [configured[name] for name in names]
    _atomic_write_json(CONFIG_PATH, config)
    # Runtime selection reads this ordered list directly. Existing bound sessions
    # stay on their current account until they rotate.
    return collect_status()


def _command_ok(command: list[str], timeout: float = 2.0) -> bool | None:
    if shutil.which(command[0]) is None:
        return None
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode == 0


def _desktop_installed() -> bool:
    candidates = list(Path("/Applications").glob("Omnigent*.app"))
    candidates.extend((HOME / "Applications").glob("Omnigent*.app"))
    return any(path.is_dir() for path in candidates)


def collect_status() -> dict[str, Any]:
    config = _read_json(CONFIG_PATH)
    state = _read_json(STATE_PATH)
    now = int(time.time())
    accounts_raw = config.get("accounts")
    accounts_raw = accounts_raw if isinstance(accounts_raw, list) else []
    cooldowns = state.get("cooldowns")
    cooldowns = cooldowns if isinstance(cooldowns, dict) else {}
    bindings = state.get("session_bindings")
    bindings = bindings if isinstance(bindings, dict) else {}
    current = state.get("current_account") if isinstance(state.get("current_account"), str) else None

    accounts: list[dict[str, Any]] = []
    for index, item in enumerate(accounts_raw, start=1):
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        auth_json = item.get("auth_json")
        if not isinstance(name, str) or not name:
            continue
        auth_path = Path(auth_json).expanduser() if isinstance(auth_json, str) and auth_json else None
        auth_present = bool(auth_path and auth_path.is_file() and auth_path.stat().st_size > 0)
        cooldown = cooldowns.get(name)
        cooldown = cooldown if isinstance(cooldown, dict) else {}
        retry_at = cooldown.get("retry_at")
        retry_at = int(retry_at) if isinstance(retry_at, (int, float)) else None
        cooling_down = retry_at is not None and retry_at > now
        sessions = sum(1 for value in bindings.values() if value == name)

        if not auth_present:
            status = "missing_auth"
        elif cooling_down:
            status = "cooldown"
        elif current == name:
            status = "active"
        else:
            status = "ready"

        accounts.append(
            {
                "index": index,
                "name": name,
                "email": _email_from_auth(auth_path) if auth_present else None,
                "status": status,
                "authPresent": auth_present,
                "current": current == name,
                "sessions": sessions,
                "retryAt": retry_at,
                "cooldownReason": cooldown.get("reason") if isinstance(cooldown.get("reason"), str) else None,
            }
        )

    claude_agent = config.get("claude_fallback_agent")
    claude_configured = isinstance(claude_agent, str) and bool(claude_agent.strip())
    claude_cli = shutil.which("claude") is not None
    claude_auth = _command_ok(["claude", "auth", "status"]) if claude_configured and claude_cli else None

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "router": {
            "configured": bool(accounts),
            "enabled": bool(config.get("enabled", True)) and bool(accounts),
            "threshold": config.get("rotate_at_percent", 99),
            "currentAccount": current,
            "accountCount": len(accounts),
            "activeBindings": len(bindings),
        },
        "accounts": accounts,
        "claude": {
            "configured": claude_configured,
            "agent": claude_agent if claude_configured else None,
            "cliInstalled": claude_cli,
            "authenticated": claude_auth,
        },
        "install": {
            "patchedRuntime": PATCHED_BASE.is_dir(),
            "patchedLauncher": PATCHED_LAUNCHER.is_file(),
            "normalOmniCli": shutil.which("omni") is not None,
            "desktopApp": _desktop_installed(),
        },
    }


def run_diagnostics() -> dict[str, Any]:
    script = DIAG_INSTALLED if DIAG_INSTALLED.is_file() else DIAG_LOCAL
    if not script.is_file():
        return {"exitCode": 127, "durationMs": 0, "output": "[FAIL] diagnostic script :: diagnose.py not found\nRESULT: NOT READY\n"}
    started = time.monotonic()
    if not _DIAG_LOCK.acquire(blocking=False):
        return {"exitCode": 75, "durationMs": 0, "output": "[WARN] diagnostics :: already running\n"}
    try:
        try:
            diag_env = os.environ.copy()
            diag_env.pop("FORCE_COLOR", None)
            diag_env["NO_COLOR"] = "1"
            result = subprocess.run(
                [shutil.which("python3") or "python3", "-S", str(script)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                timeout=45,
                check=False,
                env=diag_env,
            )
            output = result.stdout
            code = result.returncode
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout if isinstance(exc.stdout, str) else ""
            output = partial + "\n[FAIL] diagnostics :: timed out after 45s\nRESULT: NOT READY\n"
            code = 124
        except OSError as exc:
            output = f"[FAIL] diagnostics :: {exc}\nRESULT: NOT READY\n"
            code = 127
    finally:
        _DIAG_LOCK.release()
    return {"exitCode": code, "durationMs": round((time.monotonic() - started) * 1000), "output": output[-60000:]}


DASHBOARD_INSTALLED = PATCHED_BASE / "dashboard.html"
DASHBOARD_LOCAL = Path(__file__).resolve().with_name("dashboard.html")


def _load_dashboard() -> str:
    path = DASHBOARD_INSTALLED if DASHBOARD_INSTALLED.is_file() else DASHBOARD_LOCAL
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return "<!doctype html><title>Omni Route</title><pre>dashboard.html missing</pre>"


class StatusHandler(BaseHTTPRequestHandler):
    server_version = "OmniRouteStatus/1.2"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _headers(self, content_type: str, length: int, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'none'; frame-ancestors 'none'")
        self.end_headers()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in {"/", "/index.html"}:
            body = _load_dashboard().encode("utf-8")
            self._headers("text/html; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        if path == "/api/status":
            body = json.dumps(collect_status(), separators=(",", ":")).encode("utf-8")
            self._headers("application/json; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        if path == "/api/diagnostics":
            body = json.dumps(run_diagnostics(), separators=(",", ":")).encode("utf-8")
            self._headers("application/json; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        body = b"not found\n"
        self._headers("text/plain; charset=utf-8", len(body), 404)
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in {"/", "/index.html"}:
            self._headers("text/html; charset=utf-8", len(_load_dashboard().encode("utf-8")))
        elif path in {"/api/status", "/api/diagnostics"}:
            self._headers("application/json; charset=utf-8", 0)
        else:
            self._headers("text/plain; charset=utf-8", 0, 404)

    def _json_body(self, max_bytes: int = 16384) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0 or length > max_bytes:
            raise ValueError("invalid request size")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return origin in {f"http://{host}", f"http://localhost:{self.server.server_port}", f"http://127.0.0.1:{self.server.server_port}"}

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path != "/api/route/order":
            body = b"method not allowed\n"
            self._headers("text/plain; charset=utf-8", len(body), 405)
            self.wfile.write(body)
            return
        if not self._same_origin():
            body = b"forbidden\n"
            self._headers("text/plain; charset=utf-8", len(body), 403)
            self.wfile.write(body)
            return
        try:
            request = self._json_body()
            names = request.get("accounts")
            if not isinstance(names, list) or not all(isinstance(name, str) and name for name in names):
                raise ValueError("accounts must be a list of account names")
            payload = {"ok": True, "status": update_route_order(names)}
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self._headers("application/json; charset=utf-8", len(body), 200)
        except (ValueError, json.JSONDecodeError) as exc:
            body = json.dumps({"ok": False, "error": str(exc)}, separators=(",", ":")).encode("utf-8")
            self._headers("application/json; charset=utf-8", len(body), 400)
        self.wfile.write(body)

    def _method_not_allowed(self) -> None:
        body = b"method not allowed\n"
        self._headers("text/plain; charset=utf-8", len(body), 405)
        self.wfile.write(body)

    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed


def main() -> None:
    parser = argparse.ArgumentParser(description="Omni Route localhost route dashboard")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically")
    args = parser.parse_args()
    if not (1 <= args.port <= 65535):
        parser.error("--port must be between 1 and 65535")

    server = ThreadingHTTPServer((HOST, args.port), StatusHandler)
    url = f"http://{HOST}:{args.port}/"
    print(f"Omni Route status: {url}")
    print("Local route dashboard. Ctrl+C to stop.")
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
