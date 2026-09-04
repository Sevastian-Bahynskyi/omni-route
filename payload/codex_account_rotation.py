"""Runner-side lifecycle supervisor for Codex subscription rotation."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import urllib.parse
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from omnigent.codex_account_pool import (
    CodexAccountPool,
    AccountProfile,
    account_has_credential,
    record_runtime_account,
    _atomic_json,
    clear_rotation_request,
    decide_rate_limits,
    read_rotation_request,
    read_runtime,
    record_runtime_fallback,
)

_logger = logging.getLogger(__name__)
_MONITORS: dict[str, asyncio.Task[None]] = {}
_CONTINUE_TEXT = (
    "Continue the interrupted task from the previous user request. "
    "Do not restart from scratch; inspect the existing workspace and conversation context."
)


def ensure_rotation_monitor(
    *,
    session_id: str,
    bridge_dir: Path,
    pool: CodexAccountPool,
    server_client: httpx.AsyncClient | None,
    relaunch: Callable[[], Awaitable[None]],
) -> None:
    """Keep exactly one account-rotation monitor per live Codex session."""
    if not pool.enabled:
        return
    current = _MONITORS.get(session_id)
    if current is not None and not current.done():
        return
    task = asyncio.create_task(
        _monitor(
            session_id=session_id,
            bridge_dir=bridge_dir,
            pool=pool,
            server_client=server_client,
            relaunch=relaunch,
        ),
        name=f"codex-account-rotation-{session_id}",
    )
    _MONITORS[session_id] = task

    def _done(done: asyncio.Task[None]) -> None:
        if _MONITORS.get(session_id) is done:
            _MONITORS.pop(session_id, None)
        if not done.cancelled() and (exc := done.exception()) is not None:
            _logger.error(
                "Codex account rotation monitor failed for %s",
                session_id,
                exc_info=exc,
            )

    task.add_done_callback(_done)


async def _wait_codex_idle(bridge_dir: Path, timeout: float = 120.0) -> bool:
    from omnigent.codex_native_bridge import read_bridge_state

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        state = read_bridge_state(bridge_dir)
        if state is not None and state.active_turn_id is None:
            return True
        await asyncio.sleep(0.25)
    return False


def _bind_manual_account(
    pool: CodexAccountPool,
    *,
    session_id: str,
    account_name: str,
) -> bool:
    """Bind one available configured account without marking any account exhausted."""
    profile = pool._by_name(account_name)
    if profile is None or not account_has_credential(profile):
        return False
    now = int(pool._now())
    with pool._locked_state() as state:
        pool._prune(state, now)
        if not pool._available(state, account_name, now):
            return False
        state.setdefault("session_bindings", {})[session_id] = account_name
        state["current_account"] = account_name
    return True


async def _monitor(
    *,
    session_id: str,
    bridge_dir: Path,
    pool: CodexAccountPool,
    server_client: httpx.AsyncClient | None,
    relaunch: Callable[[], Awaitable[None]],
) -> None:
    last_liveness_check = 0.0
    while True:
        now = time.monotonic()
        if server_client is not None and now - last_liveness_check >= 30.0:
            last_liveness_check = now
            try:
                response = await server_client.get(
                    f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
                    timeout=5.0,
                )
                if response.status_code == 404:
                    return
            except Exception:
                pass

        request = read_rotation_request(bridge_dir)
        if request is None:
            await asyncio.sleep(0.2)
            continue
        if request.get("session_id") != session_id:
            clear_rotation_request(bridge_dir)
            continue

        runtime = read_runtime(bridge_dir) or {}
        current_account = runtime.get("account_name")
        active_provider = runtime.get("active_provider", runtime.get("mode"))
        requested_account = request.get("account_name")
        if not isinstance(current_account, str):
            clear_rotation_request(bridge_dir)
            await asyncio.sleep(0.2)
            continue
        if isinstance(requested_account, str) and requested_account != current_account:
            clear_rotation_request(bridge_dir)
            continue

        live_pool = CodexAccountPool.from_default()

        manual_target = request.get("target_account") if request.get("manual") else None
        manual = isinstance(manual_target, str)
        if manual:
            if manual_target == current_account and runtime.get("mode") in {"codex", "claude"}:
                clear_rotation_request(bridge_dir)
                continue
            if not await _wait_idle(bridge_dir, active_provider, server_client, session_id):
                clear_rotation_request(bridge_dir)
                continue
            if not _bind_manual_account(live_pool, session_id=session_id, account_name=manual_target):
                clear_rotation_request(bridge_dir)
                continue
            next_account = live_pool._by_name(manual_target)
        else:
            retry_at = request.get("retry_at")
            retry_at_int = int(retry_at) if isinstance(retry_at, (int, float)) else None
            if retry_at_int is None and runtime.get("mode") == "codex":
                retry_at_int = await _read_current_retry_at(
                    bridge_dir, rotate_at_percent=live_pool.config.rotate_at_percent
                )
            next_account = live_pool.rotate_session(
                session_id, exhausted_account=current_account, retry_at=retry_at_int,
                reason="subscription quota reached",
                provider=active_provider if active_provider in {"codex", "claude"} else None,
            )
        clear_rotation_request(bridge_dir)
        if next_account is None:
            record_runtime_account(bridge_dir, session_id=session_id, account_name=current_account,
                                   provider="exhausted", phase="exhausted")
            exhausted = read_runtime(bridge_dir) or {}
            exhausted["active_provider"] = active_provider
            exhausted["detail"] = "All subscriptions for the active provider are cooling down or need sign-in."
            from omnigent.codex_account_pool import RUNTIME_FILE
            _atomic_json(bridge_dir / RUNTIME_FILE, exhausted)
            continue
        if next_account.provider == active_provider:
            await relaunch()
            if not manual and request.get("replay_required"):
                if next_account.provider == "codex":
                    await _continue_codex_after_relaunch(bridge_dir)
                elif server_client is not None:
                    await _continue_session(server_client, session_id)
            continue
        record_runtime_fallback(
            bridge_dir, session_id=session_id, mode=next_account.provider + "_pending",
        )
        if _MONITORS.get(session_id) is asyncio.current_task():
            _MONITORS.pop(session_id, None)
        await switch_to_account(
            session_id=session_id, bridge_dir=bridge_dir, server_client=server_client,
            profile=next_account, continue_after_switch=not manual,
        )
        return


async def _wait_idle(bridge_dir: Path, mode: object, client: httpx.AsyncClient | None, session_id: str) -> bool:
    if mode == "codex":
        return await _wait_codex_idle(bridge_dir)
    if client is None:
        return False
    encoded = urllib.parse.quote(session_id, safe="")
    for _ in range(480):
        response = await client.get(f"/v1/sessions/{encoded}", timeout=5.0)
        response.raise_for_status()
        if response.json().get("status") != "running":
            return True
        await asyncio.sleep(0.25)
    return False


async def _read_current_retry_at(
    bridge_dir: Path, *, rotate_at_percent: float
) -> int | None:
    from omnigent.codex_native_app_server import client_for_transport
    from omnigent.codex_native_bridge import read_bridge_state

    state = read_bridge_state(bridge_dir)
    if state is None:
        return None
    client = client_for_transport(
        state.socket_path, client_name="omnigent-account-rotation-quota"
    )
    try:
        await client.connect()
        payload = await client.request("account/rateLimits/read", {})
        return decide_rate_limits(
            payload, rotate_at_percent=rotate_at_percent
        ).retry_at
    except Exception:
        return None
    finally:
        with contextlib.suppress(Exception):
            await client.close()


async def _continue_codex_after_relaunch(bridge_dir: Path) -> None:
    from omnigent.codex_native_app_server import client_for_transport
    from omnigent.codex_native_bridge import read_bridge_state

    for _ in range(300):
        state = read_bridge_state(bridge_dir)
        runtime = read_runtime(bridge_dir)
        if (
            state is not None
            and runtime is not None
            and runtime.get("mode") == "codex"
            and state.active_turn_id is None
        ):
            client = client_for_transport(
                state.socket_path, client_name="omnigent-account-rotation-continue"
            )
            try:
                await client.connect()
                await client.request(
                    "turn/start",
                    {
                        "threadId": state.thread_id,
                        "input": [{"type": "text", "text": _CONTINUE_TEXT}],
                        "environments": [
                            {
                                "environmentId": "local",
                                "cwd": state.cwd or str(Path.cwd()),
                            }
                        ],
                    },
                )
                return
            finally:
                with contextlib.suppress(Exception):
                    await client.close()
        await asyncio.sleep(0.2)
    _logger.error("Timed out continuing Codex after account rotation: %s", bridge_dir)


async def _continue_session(client: httpx.AsyncClient, session_id: str) -> None:
    encoded = urllib.parse.quote(session_id, safe="")
    body = {"type": "message", "data": {"role": "user", "content": [{"type": "input_text", "text": _CONTINUE_TEXT}]}}
    for _ in range(20):
        response = await client.post(f"/v1/sessions/{encoded}/events", json=body, timeout=20.0)
        if response.status_code not in {409, 503}:
            response.raise_for_status()
            return
        await asyncio.sleep(0.5)
    response.raise_for_status()


async def switch_to_account(
    *, session_id: str, bridge_dir: Path, server_client: httpx.AsyncClient | None,
    profile: AccountProfile, continue_after_switch: bool = True,
) -> None:
    if server_client is None:
        record_runtime_fallback(bridge_dir, session_id=session_id, mode="exhausted", detail="Session service is unavailable.")
        return
    encoded = urllib.parse.quote(session_id, safe="")
    try:
        if not await _wait_idle(bridge_dir, "switch", server_client, session_id):
            raise RuntimeError("Session did not become idle.")
        response = await server_client.get("/v1/agents", params={"limit": 100}, timeout=10.0)
        response.raise_for_status()
        agents = response.json().get("data", [])
        target = next((a for a in agents if a.get("name") == profile.provider + "-native-ui"), None)
        if target is None or not isinstance(target.get("id"), str):
            raise RuntimeError("Requested provider is unavailable.")
        switched = await server_client.post(
            f"/v1/sessions/{encoded}/switch-agent", json={"agent_id": target["id"]}, timeout=20.0,
        )
        switched.raise_for_status()
        record_runtime_account(bridge_dir, session_id=session_id, account_name=profile.name,
                               provider=profile.provider, phase="selected")
        if continue_after_switch:
            await _continue_session(server_client, session_id)
    except Exception:
        _logger.error("Subscription switch failed")
        record_runtime_fallback(
            bridge_dir, session_id=session_id, mode="exhausted",
            detail="Account switch failed. Check account sign-in and session service, then retry.",
        )
