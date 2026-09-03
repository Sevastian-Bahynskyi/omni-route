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
        # Avoid keeping a dead session alive forever if its runner remains up.
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
            # A stale request from an older auth generation.
            clear_rotation_request(bridge_dir)
            continue

        retry_at = request.get("retry_at")
        retry_at_int = int(retry_at) if isinstance(retry_at, (int, float)) else None
        if retry_at_int is None:
            retry_at_int = await _read_current_retry_at(
                bridge_dir, rotate_at_percent=pool.config.rotate_at_percent
            )

        replay_required = bool(request.get("replay_required"))
        next_account = pool.rotate_session(
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

        # All configured Codex subscriptions are unavailable. Claude is an
        # OPTIONAL final fallback; a Codex-only install must remain valid.
        fallback_name = pool.config.claude_fallback_agent
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

        # A preflight request is still an Omnigent turn, even though it has not
        # reached Codex. switch-agent rejects running sessions. Publish a new
        # generation first so the waiting executor returns TurnComplete; then
        # wait for the server to become idle before switching to Claude.
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
        # The preflight executor returns as soon as it sees claude_pending.
        # Poll until the server's turn-status cache exposes idle.
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

        # switch-agent keeps this session's transcript/workspace. The message
        # below makes the fallback continue without human intervention.
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
