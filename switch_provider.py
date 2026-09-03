#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import time
import urllib.parse
from pathlib import Path
from typing import Any

BASE_URL = "http://127.0.0.1:6767"
BRIDGE_ROOT = Path.home() / ".omnigent" / "codex-native"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def latest_bridge() -> tuple[Path, dict[str, Any]] | None:
    candidates: list[tuple[float, Path, dict[str, Any]]] = []
    if not BRIDGE_ROOT.is_dir():
        return None
    for runtime_path in BRIDGE_ROOT.glob("*/codex-account-runtime.json"):
        runtime = _read_json(runtime_path)
        if not isinstance(runtime.get("session_id"), str):
            continue
        try:
            stamp = max(float(runtime.get("updated_at") or 0), runtime_path.stat().st_mtime)
        except OSError:
            stamp = float(runtime.get("updated_at") or 0)
        candidates.append((stamp, runtime_path.parent, runtime))
    if not candidates:
        return None
    _, bridge, runtime = max(candidates, key=lambda item: item[0])
    return bridge, runtime


async def _wait_session_idle(client: Any, session_id: str, timeout: float = 120.0) -> None:
    encoded = urllib.parse.quote(session_id, safe="")
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/v1/sessions/{encoded}", timeout=5.0)
        if response.status_code == 404:
            raise RuntimeError("routed Omnigent session no longer exists")
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("status") != "running":
            return
        await asyncio.sleep(0.35)
    raise RuntimeError("session is still running; provider switch timed out")


async def _switch_agent(client: Any, session_id: str, agent_name: str) -> None:
    await _wait_session_idle(client, session_id)
    response = await client.get("/v1/agents", params={"limit": 100}, timeout=10.0)
    response.raise_for_status()
    payload = response.json()
    agents = payload.get("data", []) if isinstance(payload, dict) else []
    target = next(
        (
            agent
            for agent in agents
            if isinstance(agent, dict)
            and agent.get("name") == agent_name
            and isinstance(agent.get("id"), str)
        ),
        None,
    )
    if target is None:
        raise RuntimeError(f"Omnigent agent {agent_name!r} is unavailable")
    encoded = urllib.parse.quote(session_id, safe="")
    switched = await client.post(
        f"/v1/sessions/{encoded}/switch-agent",
        json={"agent_id": target["id"]},
        timeout=20.0,
    )
    switched.raise_for_status()

async def switch_provider(provider: str) -> dict[str, Any]:
    from omnigent.codex_account_pool import (
        CodexAccountPool, ROTATE_FILE, _atomic_json, account_has_credential,
        read_runtime, record_runtime_account,
    )
    from omnigent.codex_account_rotation import _bind_manual_account

    found = latest_bridge()
    if found is None:
        raise RuntimeError("Start a routed session before switching accounts.")
    bridge, runtime = found
    session_id = runtime["session_id"]
    pool = CodexAccountPool.from_default()
    target = pool._by_name(provider)
    if target is None or not account_has_credential(target):
        raise RuntimeError("Choose a configured account with a valid subscription login.")
    if runtime.get("account_name") == provider and runtime.get("mode") == target.provider:
        return {"ok": True, "provider": provider, "changed": False}
    if runtime.get("mode") not in {"codex", "claude", "exhausted"}:
        raise RuntimeError("An account switch is already in progress.")
    request_path = bridge / ROTATE_FILE
    if request_path.exists():
        raise RuntimeError("An account switch is already pending.")
    if runtime.get("mode") == "codex" and target.provider == "codex":
        from omnigent.codex_native_bridge import read_bridge_state
        state = read_bridge_state(bridge)
        if not state or not Path(state.socket_path).exists():
            if not _bind_manual_account(pool, session_id=session_id, account_name=provider):
                raise RuntimeError("This account is unavailable.")
            record_runtime_account(bridge, session_id=session_id, account_name=provider)
            return {"ok": True, "provider": provider, "changed": True}
    if runtime.get("phase") == "selected" or (runtime.get("mode") == "exhausted" and not isinstance(current_account := runtime.get("account_name"), str)):
        from omnigent.cli_auth import open_server_client
        from omnigent.codex_account_rotation import switch_to_account
        async with open_server_client(BASE_URL) as client:
            await _wait_session_idle(client, session_id)
            if not _bind_manual_account(pool, session_id=session_id, account_name=provider):
                raise RuntimeError("This account is unavailable.")
            active_provider = runtime.get("mode")
            if active_provider == "exhausted":
                encoded = urllib.parse.quote(session_id, safe="")
                response = await client.get(f"/v1/sessions/{encoded}", timeout=5)
                response.raise_for_status()
                agent_name = response.json().get("agent_name", "")
                active_provider = "claude" if agent_name == "claude-native-ui" else "codex" if agent_name == "codex-native-ui" else None
                if active_provider is None:
                    raise RuntimeError("Session provider could not be resolved. Resume the session and retry.")
                if active_provider == target.provider:
                    terminals = await client.get(f"/v1/sessions/{encoded}/resources/terminals", params={"limit": 100}, timeout=10)
                    terminals.raise_for_status()
                    for terminal in terminals.json().get("data", []):
                        metadata = terminal.get("metadata", {})
                        if metadata.get("terminal_name") == active_provider and metadata.get("session_key") == "main":
                            terminal_id = urllib.parse.quote(terminal["id"], safe="")
                            closed = await client.delete(f"/v1/sessions/{encoded}/resources/terminals/{terminal_id}", timeout=10)
                            closed.raise_for_status()
            if active_provider != target.provider:
                await switch_to_account(session_id=session_id, bridge_dir=bridge, server_client=client,
                                        profile=target, continue_after_switch=False)
            else:
                record_runtime_account(bridge, session_id=session_id, account_name=provider,
                                       provider=target.provider, phase="selected")
    else:
        _atomic_json(request_path, {
            "session_id": session_id, "account_name": runtime.get("account_name"),
            "target_account": provider, "manual": True, "replay_required": False,
            "requested_at": int(time.time()),
        })
    generation = int(runtime.get("generation", 0))
    deadline = asyncio.get_running_loop().time() + 140
    while asyncio.get_running_loop().time() < deadline:
        updated = read_runtime(bridge) or {}
        if int(updated.get("generation", 0)) > generation:
            if updated.get("mode") == target.provider and updated.get("account_name") == provider:
                return {"ok": True, "provider": provider, "changed": True}
            if updated.get("mode") == "exhausted":
                raise RuntimeError("Account switch failed. Check sign-in and retry.")
        await asyncio.sleep(0.25)
    raise RuntimeError("Account switch timed out. Check the session and retry.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Switch provider/account for the latest routed Omni Route session")
    parser.add_argument("provider", help="Configured Codex or Claude account name")
    args = parser.parse_args()
    try:
        result = asyncio.run(switch_provider(args.provider))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
