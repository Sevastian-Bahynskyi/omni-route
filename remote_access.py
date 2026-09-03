#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

DASHBOARD_TARGET = "http://127.0.0.1:8787"
TAILSCALE_HTTPS_PORT = 8443


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


def _run(*args: str, timeout: float = 4.0) -> subprocess.CompletedProcess[str] | None:
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
    except (OSError, subprocess.TimeoutExpired):
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


def _serve_state() -> tuple[bool, bool]:
    """Return (our_dashboard_enabled, https_port_in_use)."""
    result = _run("serve", "status", "--json")
    if result is not None and result.returncode == 0:
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            port_in_use = False
            web_maps: list[dict[str, Any]] = []
            web = payload.get("Web")
            if isinstance(web, dict):
                web_maps.append(web)
            services = payload.get("Services")
            if isinstance(services, dict):
                for service in services.values():
                    if isinstance(service, dict) and isinstance(service.get("Web"), dict):
                        web_maps.append(service["Web"])
            for web_map in web_maps:
                for host_port, config in web_map.items():
                    if not isinstance(host_port, str) or not host_port.endswith(f":{TAILSCALE_HTTPS_PORT}"):
                        continue
                    port_in_use = True
                    if DASHBOARD_TARGET in json.dumps(config, separators=(",", ":")):
                        return True, True
            if port_in_use:
                return False, True

    result = _run("serve", "status")
    if result is None or result.returncode != 0:
        return False, False
    text = result.stdout
    port_in_use = f":{TAILSCALE_HTTPS_PORT}" in text
    return DASHBOARD_TARGET in text and port_in_use, port_in_use


def status() -> dict[str, Any]:
    cli = tailscale_cli()
    if cli is None:
        return {
            "installed": False,
            "connected": False,
            "connectionState": "not_installed",
            "enabled": False,
            "url": None,
            "detail": "Tailscale is not installed",
        }
    connected, state, dns_name, detail = _connection()
    enabled, port_in_use = _serve_state()
    conflict = port_in_use and not enabled
    if conflict and detail is None:
        detail = f"Tailscale HTTPS port {TAILSCALE_HTTPS_PORT} is already used by another Serve target"
    url = f"https://{dns_name}:{TAILSCALE_HTTPS_PORT}/" if dns_name else None
    return {
        "installed": True,
        "connected": connected,
        "connectionState": state,
        "enabled": enabled,
        "portConflict": conflict,
        "url": url,
        "detail": detail,
    }


def enable() -> dict[str, Any]:
    before = status()
    if not before["installed"]:
        return {"ok": False, "error": "Tailscale is not installed", "remoteAccess": before}
    if not before["connected"]:
        return {
            "ok": False,
            "error": "Tailscale is installed but not connected. Sign in/connect Tailscale first.",
            "remoteAccess": before,
        }
    if before.get("portConflict"):
        return {
            "ok": False,
            "error": f"Tailscale HTTPS port {TAILSCALE_HTTPS_PORT} is already used by another Serve target.",
            "remoteAccess": before,
        }
    if before["enabled"]:
        return {"ok": True, "remoteAccess": before}
    result = _run(
        "serve",
        "--bg",
        "--yes",
        f"--https={TAILSCALE_HTTPS_PORT}",
        DASHBOARD_TARGET,
        timeout=25.0,
    )
    after = status()
    if result is None:
        return {"ok": False, "error": "Unable to run Tailscale Serve", "remoteAccess": after}
    if result.returncode != 0 or not after["enabled"]:
        detail = result.stdout.strip()[-1500:] or "Tailscale Serve did not become active"
        return {"ok": False, "error": detail, "remoteAccess": after}
    return {"ok": True, "remoteAccess": after}


def disable() -> dict[str, Any]:
    before = status()
    if not before["installed"] or not before["enabled"]:
        return {"ok": True, "remoteAccess": before}
    result = _run("serve", "--yes", f"--https={TAILSCALE_HTTPS_PORT}", "off", timeout=20.0)
    after = status()
    if not after["enabled"]:
        return {"ok": True, "remoteAccess": after}
    detail = result.stdout.strip()[-1500:] if result is not None else "Unable to run Tailscale Serve"
    return {"ok": False, "error": detail or "Tailscale Serve is still enabled", "remoteAccess": after}


def main() -> int:
    parser = argparse.ArgumentParser(description="Omni Route Tailscale remote-access controller")
    parser.add_argument("action", choices=("status", "enable", "disable"))
    args = parser.parse_args()
    if args.action == "enable":
        payload = enable()
    elif args.action == "disable":
        payload = disable()
    else:
        payload = status()
    print(json.dumps(payload, separators=(",", ":")))
    return 0 if not isinstance(payload, dict) or payload.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
