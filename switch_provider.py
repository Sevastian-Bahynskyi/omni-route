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

def _bind_pool_account(pool: Any, session_id: str, provider: str) -> None:
    from omnigent.codex_account_pool import auth_json_has_credential

    profile = pool._by_name(provider)
    if profile is None:
        raise RuntimeError(f"unknown Codex account {provider!r}")
    if not auth_json_has_credential(profile.auth_json):
        raise RuntimeError(f"Codex account {provider!r} has no usable auth")
    now = int(pool._now())
    with pool._locked_state() as state:
        pool._prune(state, now)
        if not pool._available(state, provider, now):
            raise RuntimeError(f"Codex account {provider!r} is cooling down")
        state.setdefault("session_bindings", {})[session_id] = provider
        state["current_account"] = provider



async def switch_provider(provider: str) -> dict[str, Any]:
    from omnigent.cli_auth import open_server_client
    from omnigent.codex_account_pool import (
        CodexAccountPool,
        ROTATE_FILE,
        read_runtime,
        record_runtime_account,
        record_runtime_fallback,
    )

    found = latest_bridge()
    if found is None:
        raise RuntimeError("no routed session exists yet; run `omni-rotate start` first")
    bridge, runtime = found
    session_id = runtime.get("session_id")
    mode = runtime.get("mode")
    current_account = runtime.get("account_name")
    if not isinstance(session_id, str):
        raise RuntimeError("latest routed runtime has no session id")

    pool = CodexAccountPool.from_default()
    account_names = {account.name for account in pool.config.accounts}
    target_is_claude = provider == "claude"
    if not target_is_claude and provider not in account_names:
        raise RuntimeError(f"unknown Codex provider {provider!r}")
    if target_is_claude and pool.config.claude_fallback_agent is None:
        raise RuntimeError("Claude fallback is not configured")

    if mode == "codex" and isinstance(current_account, str):
        if not target_is_claude and provider == current_account:
            return {"ok": True, "provider": provider, "sessionId": session_id, "changed": False}
        if not target_is_claude:
            from omnigent.codex_native_bridge import read_bridge_state
            bridge_state = read_bridge_state(bridge)
            socket_live = bool(bridge_state and Path(bridge_state.socket_path).exists())
            if not socket_live:
                _bind_pool_account(pool, session_id, provider)
                record_runtime_account(bridge, session_id=session_id, account_name=provider)
                return {"ok": True, "provider": provider, "sessionId": session_id, "changed": True}

        request_path = bridge / ROTATE_FILE
        if request_path.exists():
            raise RuntimeError("an account rotation/switch is already pending")
        request = {
            "session_id": session_id,
            "account_name": current_account,
            "target_account": provider,
            "manual": True,
            "retry_at": None,
            "reason": "manual_provider_switch",
            "replay_required": False,
            "requested_at": int(time.time()),
        }
        tmp = request_path.with_suffix(".manual.tmp")
        tmp.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(request_path)

        generation = int(runtime.get("generation", 0))
        deadline = asyncio.get_running_loop().time() + 140.0
        while asyncio.get_running_loop().time() < deadline:
            updated = read_runtime(bridge)
            if updated and int(updated.get("generation", 0)) > generation:
                if provider == "claude" and updated.get("mode") == "claude":
                    return {"ok": True, "provider": provider, "sessionId": session_id, "changed": True}
                if (
                    provider != "claude"
                    and updated.get("mode") == "codex"
                    and updated.get("account_name") == provider
                ):
                    return {"ok": True, "provider": provider, "sessionId": session_id, "changed": True}
            await asyncio.sleep(0.25)
        raise RuntimeError("provider switch did not complete within 140 seconds")

    if mode in {"claude", "claude_pending"}:
        if target_is_claude:
            return {"ok": True, "provider": "claude", "sessionId": session_id, "changed": False}
        # Bind the requested account before switching the agent back to Codex.
        _bind_pool_account(pool, session_id, provider)
        async with open_server_client(BASE_URL) as client:
            await _switch_agent(client, session_id, "codex-native-ui")
        record_runtime_account(bridge, session_id=session_id, account_name=provider)
        return {"ok": True, "provider": provider, "sessionId": session_id, "changed": True}

    raise RuntimeError(f"cannot switch provider while routed runtime mode is {mode!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Switch provider/account for the latest routed Omni Route session")
    parser.add_argument("provider", help="Codex account name or 'claude'")
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
