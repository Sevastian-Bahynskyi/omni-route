#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
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


def latest_bridge(session_id: str | None = None) -> tuple[Path, dict[str, Any]] | None:
    candidates: list[tuple[float, Path, dict[str, Any]]] = []
    if not BRIDGE_ROOT.is_dir():
        return None
    for runtime_path in BRIDGE_ROOT.glob("*/codex-account-runtime.json"):
        runtime = _read_json(runtime_path)
        if not isinstance(runtime.get("session_id"), str):
            continue
        if session_id is not None and runtime["session_id"] != session_id:
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


async def _close_native_terminal(client: Any, session_id: str, provider: str) -> None:
    encoded = urllib.parse.quote(session_id, safe="")
    response = await client.get(
        f"/v1/sessions/{encoded}/resources/terminals",
        params={"limit": 100},
        timeout=10.0,
    )
    response.raise_for_status()
    payload = response.json()
    terminals = payload.get("data", []) if isinstance(payload, dict) else []
    for terminal in terminals:
        if not isinstance(terminal, dict):
            continue
        metadata = terminal.get("metadata")
        terminal_id = terminal.get("id")
        if (
            isinstance(metadata, dict)
            and metadata.get("terminal_name") == provider
            and metadata.get("session_key") == "main"
            and isinstance(terminal_id, str)
        ):
            closed = await client.delete(
                f"/v1/sessions/{encoded}/resources/terminals/"
                f"{urllib.parse.quote(terminal_id, safe='')}",
                timeout=10.0,
            )
            if closed.status_code != 404:
                closed.raise_for_status()


async def _launch_native_terminal(client: Any, session_id: str, provider: str) -> None:
    encoded = urllib.parse.quote(session_id, safe="")
    launched = await client.post(
        f"/v1/sessions/{encoded}/resources/terminals",
        json={
            "terminal": provider,
            "session_key": "main",
            "ensure_native_terminal": True,
        },
        timeout=30.0,
    )
    launched.raise_for_status()


async def _continue_provider_handoff(client: Any, session_id: str) -> None:
    encoded = urllib.parse.quote(session_id, safe="")
    body = {
        "type": "message",
        "data": {
            "role": "user",
            "content": [{
                "type": "input_text",
                "text": (
                    "Continue this task after the explicit provider handoff. "
                    "Use the existing conversation and workspace state, preserve completed work "
                    "and constraints, and continue from the latest unfinished objective."
                ),
            }],
        },
    }
    for _ in range(20):
        response = await client.post(
            f"/v1/sessions/{encoded}/events",
            json=body,
            timeout=20.0,
        )
        if response.status_code not in {409, 503}:
            response.raise_for_status()
            return
        await asyncio.sleep(0.5)
    response.raise_for_status()


async def switch_provider(provider: str, *, session_id: str | None = None) -> dict[str, Any]:
    from omnigent.codex_account_pool import (
        CodexAccountPool, account_has_credential,
        clear_rotation_request, record_runtime_account,
    )
    from omnigent.codex_account_rotation import _bind_manual_account

    found = latest_bridge(session_id)
    if found is None:
        raise RuntimeError("Start a routed session before switching accounts.")
    bridge, runtime = found
    session_id = runtime["session_id"]
    pool = CodexAccountPool.from_default()
    target = pool._by_name(provider)
    if target is None or not account_has_credential(target):
        raise RuntimeError("Choose a configured account with a valid subscription login.")
    if (
        runtime.get("account_name") == provider
        and runtime.get("mode") == target.provider
        and runtime.get("phase") == "active"
    ):
        return {"ok": True, "provider": provider, "changed": False}
    clear_rotation_request(bridge)
    from omnigent.cli_auth import open_server_client

    async with open_server_client(BASE_URL) as client:
        await _wait_session_idle(client, session_id)
        encoded = urllib.parse.quote(session_id, safe="")
        response = await client.get(f"/v1/sessions/{encoded}", timeout=5.0)
        response.raise_for_status()
        session = response.json()
        agent_name = session.get("agent_name") if isinstance(session, dict) else None
        actual_provider = (
            "claude" if agent_name == "claude-native-ui"
            else "codex" if agent_name == "codex-native-ui"
            else None
        )
        if actual_provider is None:
            raise RuntimeError("The selected session is not using a routed Codex or Claude agent.")
        if not _bind_manual_account(pool, session_id=session_id, account_name=provider):
            raise RuntimeError("This account is unavailable.")
        if actual_provider == target.provider:
            await _close_native_terminal(client, session_id, actual_provider)
            await _launch_native_terminal(client, session_id, actual_provider)
        else:
            await _switch_agent(client, session_id, f"{target.provider}-native-ui")
            await _continue_provider_handoff(client, session_id)
        record_runtime_account(
            bridge,
            session_id=session_id,
            account_name=provider,
            provider=target.provider,
            phase="selected",
        )
    return {
        "ok": True,
        "provider": provider,
        "sessionId": session_id,
        "changed": True,
        "phase": "selected",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Switch provider/account for the latest routed Omni Route session")
    parser.add_argument("provider", help="Configured Codex or Claude account name")
    parser.add_argument("--session", dest="session_id")
    args = parser.parse_args()
    try:
        result = asyncio.run(switch_provider(args.provider, session_id=args.session_id))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
