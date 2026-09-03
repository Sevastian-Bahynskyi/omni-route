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
    auth_json_has_credential,
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
    if profile is None or not auth_json_has_credential(profile.auth_json):
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
        requested_account = request.get("account_name")
        if not isinstance(current_account, str):
            clear_rotation_request(bridge_dir)
            await asyncio.sleep(0.2)
            continue
        if isinstance(requested_account, str) and requested_account != current_account:
            clear_rotation_request(bridge_dir)
            continue

        live_pool = CodexAccountPool.from_default()

        # Dashboard account switch: do not mark the current account exhausted and
        # do not disturb a running turn. The selected binding is applied only
        # once the current Codex turn is idle, then the same Omnigent session is
        # relaunched on that account.
        manual_target = request.get("target_account") if request.get("manual") else None
        if isinstance(manual_target, str):
            if manual_target == current_account:
                clear_rotation_request(bridge_dir)
                continue
            if not await _wait_codex_idle(bridge_dir):
                _logger.warning(
                    "Timed out waiting for %s to become idle for manual provider switch",
                    session_id,
                )
                clear_rotation_request(bridge_dir)
                continue
            if manual_target == "claude":
                fallback_name = live_pool.config.claude_fallback_agent
                clear_rotation_request(bridge_dir)
                if fallback_name is None:
                    _logger.warning("Manual Claude switch requested but Claude is not configured")
                    continue
                record_runtime_fallback(
                    bridge_dir,
                    session_id=session_id,
                    mode="claude_pending",
                    detail=fallback_name,
                )
                await _fallback_to_claude_when_idle(
                    session_id=session_id,
                    bridge_dir=bridge_dir,
                    server_client=server_client,
                    fallback_name=fallback_name,
                    continue_after_switch=False,
                )
                return
            if not _bind_manual_account(
                live_pool,
                session_id=session_id,
                account_name=manual_target,
            ):
                _logger.warning(
                    "Manual Codex provider switch rejected for %s -> %s",
                    session_id,
                    manual_target,
                )
                clear_rotation_request(bridge_dir)
                continue
            clear_rotation_request(bridge_dir)
            _logger.info(
                "Manually switching Codex subscription for %s: %s -> %s",
                session_id,
                current_account,
                manual_target,
            )
            await relaunch()
            continue

        retry_at = request.get("retry_at")
        retry_at_int = int(retry_at) if isinstance(retry_at, (int, float)) else None
        if retry_at_int is None:
            retry_at_int = await _read_current_retry_at(
                bridge_dir, rotate_at_percent=live_pool.config.rotate_at_percent
            )

        replay_required = bool(request.get("replay_required"))
        next_account = live_pool.rotate_session(
            session_id,
            exhausted_account=current_account,
            retry_at=retry_at_int,
            reason=str(request.get("reason") or "usage limit"),
        )
        clear_rotation_request(bridge_dir)

        if next_account is not None:
            _logger.info(
                "Rotating Codex subscription for %s: %s -> %s",
                session_id,
                current_account,
                next_account.name,
            )
            await relaunch()
            if replay_required:
                await _continue_codex_after_relaunch(bridge_dir)
            continue

        fallback_name = live_pool.config.claude_fallback_agent
        if fallback_name is None:
            record_runtime_fallback(
                bridge_dir,
                session_id=session_id,
                mode="exhausted",
                detail=(
                    "All configured Codex subscriptions are currently exhausted "
                    "and no Claude fallback is configured"
                ),
            )
            return

        record_runtime_fallback(
            bridge_dir,
            session_id=session_id,
            mode="claude_pending",
            detail=fallback_name,
        )
        await _fallback_to_claude_when_idle(
            session_id=session_id,
            bridge_dir=bridge_dir,
            server_client=server_client,
            fallback_name=fallback_name,
        )
        return


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


async def _fallback_to_claude_when_idle(
    *,
    session_id: str,
    bridge_dir: Path,
    server_client: httpx.AsyncClient | None,
    fallback_name: str,
    continue_after_switch: bool = True,
) -> None:
    if server_client is None:
        record_runtime_fallback(
            bridge_dir,
            session_id=session_id,
            mode="exhausted",
            detail="No server client available for Claude fallback",
        )
        return

    encoded = urllib.parse.quote(session_id, safe="")
    try:
        idle = False
        for _ in range(120):
            response = await server_client.get(f"/v1/sessions/{encoded}", timeout=5.0)
            if response.status_code == 404:
                raise RuntimeError("session disappeared before Claude fallback")
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("status") != "running":
                idle = True
                break
            await asyncio.sleep(0.25)
        if not idle:
            raise RuntimeError("session did not become idle for Claude fallback")

        response = await server_client.get("/v1/agents", params={"limit": 100}, timeout=10.0)
        response.raise_for_status()
        payload = response.json()
        agents = payload.get("data", []) if isinstance(payload, dict) else []
        target = next(
            (
                agent
                for agent in agents
                if isinstance(agent, dict) and agent.get("name") == fallback_name
            ),
            None,
        )
        if target is None or not isinstance(target.get("id"), str):
            raise RuntimeError(f"Claude fallback agent {fallback_name!r} was not found")

        switched = await server_client.post(
            f"/v1/sessions/{encoded}/switch-agent",
            json={"agent_id": target["id"]},
            timeout=20.0,
        )
        switched.raise_for_status()
        record_runtime_fallback(
            bridge_dir,
            session_id=session_id,
            mode="claude",
            detail=fallback_name,
        )
        if not continue_after_switch:
            return

        body = {
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": _CONTINUE_TEXT}],
            },
        }
        last_error: Exception | None = None
        for _ in range(20):
            try:
                resumed = await server_client.post(
                    f"/v1/sessions/{encoded}/events",
                    json=body,
                    timeout=10.0,
                )
                if resumed.status_code < 400:
                    return
                last_error = RuntimeError(
                    f"Claude continuation returned HTTP {resumed.status_code}"
                )
            except Exception as exc:  # noqa: BLE001
                last_error = exc
            await asyncio.sleep(0.5)
        raise RuntimeError(f"Claude switched but continuation failed: {last_error}")
    except Exception as exc:  # noqa: BLE001
        _logger.exception("Claude fallback failed for %s", session_id)
        record_runtime_fallback(
            bridge_dir,
            session_id=session_id,
            mode="exhausted",
            detail=str(exc),
        )
