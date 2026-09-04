#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import urllib.parse
import shutil
import subprocess
from pathlib import Path
from typing import Any

DASHBOARD_TARGET = "http://127.0.0.1:8787"
TAILSCALE_HTTPS_PORT = 8443
SERVER_HTTPS_PORT = 8444
SERVER_TARGET = "http://127.0.0.1:6767"
TRUSTED_ORIGINS_PATH = Path.home() / ".omnigent" / "omni-route-trusted-origins.json"
_APPROVAL_URL: str | None = None


def tailscale_cli() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    for candidate in (
        Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale"),
        Path.home() / "Applications/Tailscale.app/Contents/MacOS/Tailscale",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def _run(*args: str, timeout: float = 2.0) -> subprocess.CompletedProcess[str] | None:
    cli = tailscale_cli()
    if cli is None:
        return None
    try:
        return subprocess.run(
            [cli, *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
        return subprocess.CompletedProcess([cli, *args], 124, stdout=output)
    except OSError:
        return None


def _connection() -> tuple[bool, str, str | None, str | None]:
    result = _run("status", "--json")
    if result is None:
        return False, "unavailable", None, "Tailscale CLI is unavailable"
    if result.returncode != 0:
        detail = result.stdout.strip().splitlines()
        message = detail[-1] if detail else "Tailscale is not connected"
        lowered = message.casefold()
        state = "needs_login" if "logged out" in lowered or "login" in lowered else "offline"
        return False, state, None, message
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False, "offline", None, "Tailscale returned invalid status JSON"
    if not isinstance(payload, dict):
        return False, "offline", None, "Tailscale returned invalid status data"
    backend = str(payload.get("BackendState") or "unknown")
    self_info = payload.get("Self")
    self_info = self_info if isinstance(self_info, dict) else {}
    dns_name = self_info.get("DNSName")
    dns_name = dns_name.rstrip(".") if isinstance(dns_name, str) and dns_name.strip() else None
    online = self_info.get("Online")
    connected = backend.casefold() == "running" and online is not False
    if connected:
        return True, "connected", dns_name, None
    backend_key = backend.casefold().replace("_", "").replace("-", "")
    state = "needs_login" if backend_key in {"needslogin", "nologin"} else "offline"
    return False, state, dns_name, f"Backend state: {backend}"


def _serve_state(port: int = TAILSCALE_HTTPS_PORT, target: str = DASHBOARD_TARGET) -> tuple[bool, bool]:
    result = _run("serve", "status", "--json")
    if result is None or result.returncode != 0:
        return False, True
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False, True
    if not isinstance(payload, dict):
        return False, True
    web = payload.get("Web", {})
    if not isinstance(web, dict):
        return False, True
    matches = [value for host, value in web.items() if host.endswith(f":{port}")]
    if not matches:
        tcp = payload.get("TCP", {})
        return False, isinstance(tcp, dict) and str(port) in tcp
    expected = {"/": {"Proxy": target}}
    owned = len(matches) == 1 and isinstance(matches[0], dict) and matches[0].get("Handlers") == expected
    return owned, True


def status() -> dict[str, Any]:
    installed = tailscale_cli() is not None
    if installed:
        connected, state, dns_name, detail = _connection()
        dashboard, dashboard_used = _serve_state()
        server, server_used = _serve_state(SERVER_HTTPS_PORT, SERVER_TARGET)
    else:
        connected, state, dns_name, detail = False, "not_installed", None, "Tailscale is not installed"
        dashboard = dashboard_used = server = server_used = False
    conflict = (dashboard_used and not dashboard) or (server_used and not server)
    if conflict:
        detail = "A remote port is occupied by another target, or its configuration could not be checked."
    if _APPROVAL_URL and not (dashboard and server):
        detail = "Approve Tailscale Serve for your network, then click Enable again."
    return {
        "installed": installed, "connected": connected, "connectionState": state,
        "enabled": dashboard and server, "dashboardEnabled": dashboard, "serverEnabled": server,
        "portConflict": conflict,
        "url": f"https://{dns_name}:{TAILSCALE_HTTPS_PORT}/" if dns_name and dashboard and connected else None,
        "serverUrl": f"https://{dns_name}:{SERVER_HTTPS_PORT}/" if dns_name and server and connected else None,
        "approvalUrl": _APPROVAL_URL if not (dashboard and server) else None,
        "detail": detail,
    }


def sync_trusted_origin(
    remote_status: dict[str, Any] | None = None,
    *,
    clear: bool = False,
) -> str | None:
    state = remote_status or status()
    server_url = state.get("serverUrl")
    origin: str | None = None
    if isinstance(server_url, str):
        parsed = urllib.parse.urlsplit(server_url)
        if (
            parsed.scheme == "https"
            and isinstance(parsed.hostname, str)
            and parsed.hostname.endswith(".ts.net")
            and parsed.port == SERVER_HTTPS_PORT
        ):
            origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin is None and not clear:
        try:
            existing = json.loads(TRUSTED_ORIGINS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        origins = existing.get("origins", []) if isinstance(existing, dict) else []
        return origins[0] if isinstance(origins, list) and origins and isinstance(origins[0], str) else None
    TRUSTED_ORIGINS_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = TRUSTED_ORIGINS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps({"origins": [origin] if origin else []}), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(TRUSTED_ORIGINS_PATH)
    return origin


def _approval_link(output: str) -> str | None:
    for candidate in re.findall(r"https://[^\s]+", output):
        url = urllib.parse.urlsplit(candidate)
        if url.netloc == "login.tailscale.com" and url.path == "/f/serve":
            return candidate
    return None


def enable() -> dict[str, Any]:
    global _APPROVAL_URL
    before = status()
    if not before["connected"]:
        return {"ok": False, "error": "Connect Tailscale on this Mac first.", "remoteAccess": before}
    if before["portConflict"]:
        return {"ok": False, "error": before["detail"], "remoteAccess": before}
    for port, target, key in (
        (TAILSCALE_HTTPS_PORT, DASHBOARD_TARGET, "dashboardEnabled"),
        (SERVER_HTTPS_PORT, SERVER_TARGET, "serverEnabled"),
    ):
        if before[key]:
            continue
        result = _run("serve", "--bg", "--yes", f"--https={port}", target, timeout=8.0)
        _APPROVAL_URL = _approval_link(result.stdout) if result is not None else None
        after = status()
        if _APPROVAL_URL:
            return {"ok": False, "error": "Tailscale requires your approval. Open the approval link below, then retry Enable.", "remoteAccess": after}
        if not after[key]:
            error = "Tailscale Serve timed out. Check Tailscale and retry." if result is not None and result.returncode == 124 else "Tailscale Serve could not start. Check your network's Serve and HTTPS settings."
            return {"ok": False, "error": error, "remoteAccess": after}
    _APPROVAL_URL = None
    after = status()
    sync_trusted_origin(after)
    return {"ok": bool(after["enabled"]), "remoteAccess": after}


def disable() -> dict[str, Any]:
    for port, target in ((TAILSCALE_HTTPS_PORT, DASHBOARD_TARGET), (SERVER_HTTPS_PORT, SERVER_TARGET)):
        owned, _ = _serve_state(port, target)
        if owned:
            _run("serve", "--yes", f"--https={port}", "off", timeout=8.0)
    after = status()
    sync_trusted_origin(after, clear=True)
    ok = not after["dashboardEnabled"] and not after["serverEnabled"]
    return {"ok": ok, "remoteAccess": after, **({} if ok else {"error": "Remote access could not be disabled. Check Tailscale and retry."})}


def main() -> int:
    parser = argparse.ArgumentParser(description="Omni Route Tailscale remote-access controller")
    parser.add_argument("action", choices=("status", "enable", "disable", "sync-origin"))
    args = parser.parse_args()
    if args.action == "enable":
        payload = enable()
    elif args.action == "disable":
        payload = disable()
    elif args.action == "status":
        payload = status()
    else:
        payload = {"ok": True, "origin": sync_trusted_origin()}
    print(json.dumps(payload, separators=(",", ":")))
    return 0 if not isinstance(payload, dict) or payload.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
