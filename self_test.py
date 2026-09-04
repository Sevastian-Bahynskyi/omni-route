from __future__ import annotations

import asyncio
import importlib.util
import json
import tempfile
from pathlib import Path

from omnigent.codex_account_pool import (
    AccountProfile,
    CodexAccountPool,
    PoolConfig,
    bind_account_auth,
    decide_rate_limits,
    is_usage_limit_payload,
    read_runtime,
    record_runtime_account,
    record_runtime_fallback,
    request_rotation,
    read_rotation_request,
    wait_for_rotation,
)


def auth(path: Path, token: str) -> Path:
    path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": token,
                    "refresh_token": f"refresh-{token}",
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_pool(tmp: Path) -> None:
    a = auth(tmp / "a.json", "a")
    b = auth(tmp / "b.json", "b")
    pool = CodexAccountPool(
        PoolConfig(accounts=(AccountProfile("a", a), AccountProfile("b", b))),
        state_path=tmp / "state.json",
        now=lambda: 1000,
    )
    assert pool.account_for_session("s").name == "a"
    assert pool.rotate_session(
        "s", exhausted_account="a", retry_at=2000, reason="quota"
    ).name == "b"
    assert pool.account_for_session("s").name == "b"


def test_quota_shapes() -> None:
    current = decide_rate_limits(
        {
            "result": {
                "ordinaryUsageAllowed": True,
                "rateLimits": {
                    "primary": {"usedPercent": 99.1, "resetsAt": 2222},
                    "secondary": {"usedPercent": 45, "resetsAt": 9999},
                    "rateLimitReachedType": None,
                },
            }
        },
        rotate_at_percent=99,
    )
    assert current.rotate and current.retry_at == 2222

    denied = decide_rate_limits(
        {
            "result": {
                "ordinaryUsageAllowed": False,
                "rateLimits": {
                    "primary": {"usedPercent": 70, "resetsAt": 2222},
                    "secondary": {"usedPercent": 80, "resetsAt": 9999},
                },
            }
        },
        rotate_at_percent=99,
    )
    assert denied.rotate and denied.retry_at == 9999

    legacy = decide_rate_limits(
        {
            "result": {
                "rateLimits": {
                    "ordinaryUsageAllowed": True,
                    "primary": {"usedPercent": 12, "resetsAt": 2222},
                    "secondary": {"usedPercent": 100, "resetsAt": 9999},
                }
            }
        },
        rotate_at_percent=99,
    )
    assert legacy.rotate and legacy.retry_at == 9999


def test_error_variants() -> None:
    assert is_usage_limit_payload(
        {"turn": {"error": {"codexErrorInfo": "usageLimitExceeded"}}}
    )
    assert is_usage_limit_payload(
        {"turn": {"error": {"codexErrorInfo": "usage_limit_exceeded"}}}
    )
    assert not is_usage_limit_payload(
        {"turn": {"error": {"codexErrorInfo": "unauthorized"}}}
    )


def test_auth_and_runtime(tmp: Path) -> None:
    source = auth(tmp / "profile.json", "x")
    home = tmp / "home"
    bind_account_auth(home, source)
    assert (home / "auth.json").is_symlink()
    assert (home / "auth.json").resolve() == source.resolve()

    bridge = tmp / "bridge"
    bridge.mkdir()
    assert record_runtime_account(bridge, session_id="s", account_name="a") == 1
    request_rotation(
        bridge,
        session_id="s",
        account_name="a",
        retry_at=3333,
        reason="quota",
        replay_required=True,
    )
    req = read_rotation_request(bridge)
    assert req is not None and req["replay_required"] is True
    assert record_runtime_fallback(
        bridge,
        session_id="s",
        mode="claude_pending",
        detail="claude-native-ui",
    ) == 2
    assert read_runtime(bridge)["mode"] == "claude_pending"



def test_optional_claude_config(tmp: Path) -> None:
    config_path = tmp / "pool.json"
    a = auth(tmp / "only.json", "only")
    config_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "rotate_at_percent": 99,
                "claude_fallback_agent": None,
                "accounts": [{"name": "only", "auth_json": str(a)}],
            }
        ),
        encoding="utf-8",
    )
    loaded = PoolConfig.load(config_path)
    assert loaded.claude_fallback_agent is None
    assert loaded.enabled is True


def test_wait(tmp: Path) -> None:
    bridge = tmp / "wait-bridge"
    bridge.mkdir()
    record_runtime_account(bridge, session_id="s", account_name="a")

    async def scenario() -> None:
        async def bump() -> None:
            await asyncio.sleep(0.02)
            record_runtime_account(bridge, session_id="s", account_name="b")

        task = asyncio.create_task(bump())
        runtime = await wait_for_rotation(bridge, after_generation=1, timeout=1)
        await task
        assert runtime is not None and runtime["account_name"] == "b"

    asyncio.run(scenario())


def test_patcher_synthetic() -> None:
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("rotation_patcher", here / "apply_patch.py")
    assert spec is not None and spec.loader is not None
    patcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(patcher)

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        app = td / "app.py"
        app.write_text(
            '''from pathlib import Path\nfrom dataclasses import dataclass\n@dataclass\nclass X:\n    cwd: Path\n    bridge_dir: Path\n    developer_instructions: str | None = None\n\nasync def start(self):\n        await asyncio.to_thread(\n            _populate_codex_home_config,\n            self.codex_home,\n            config_source,\n            inject_hooks=self.router_hooks_registered,\n            extend_model_catalog=codex_extended_catalog_requested(self.env),\n        )\n        if self.trust_project:\n            pass\n\ndef build_codex_native_server(*,\n    trust_project: bool = False,\n    trust_all_hooks: bool = False,\n) -> CodexNativeAppServer:\n    if extra_config_overrides:\n        config_overrides.extend(extra_config_overrides)\n    return CodexNativeAppServer(\n        cwd=cwd,\n        bridge_dir=bridge_dir,\n        developer_instructions=developer_instructions,\n    )\n''',
            encoding="utf-8",
        )
        patcher.patch_app_server(app)
        text = app.read_text(encoding="utf-8")
        assert "bind_account_auth" in text
        assert "cli_auth_credentials_store" in text

        orchestration = td / "orchestration.py"
        orchestration.write_text(
            '''async def f():\n    bridge_dir = prepare_bridge_dir(session_id)\n    socket_path = socket_path_for_bridge_dir(bridge_dir)\n    codex_home = codex_home_for_bridge_dir(bridge_dir)\n    app_server = build_codex_native_server(\n        extra_config_overrides=[*_codex_launch.config_overrides, *mcp_overrides],\n        bridge_dir=bridge_dir,\n        ap_server_url=launch_config.policy_server_url,\n    )\n    await app_server.start()\n    _AUTO_CODEX_APP_SERVERS[session_id] = app_server\n    if ensure_comment_relay is not None:\n        await ensure_comment_relay(session_id, explicit_bridge_dir=bridge_dir, await_notify=False)\n\n    _logger.info(\n        "Auto-created codex terminal + forwarder for session %s",\n        session_id,\n    )\n    return terminal_view\n''',
            encoding="utf-8",
        )
        patcher.patch_orchestration(orchestration)
        text = orchestration.read_text(encoding="utf-8")
        assert "CodexAccountPool.from_default()" in text
        assert "ensure_rotation_monitor" in text

        executor = td / "executor.py"
        executor.write_text(
            '''async def f():\n                await client.connect()\n                try:\n                    if goal_objective is not None:\n                        await client.request(\n                            "thread/goal/set",\n                            {\n                                "threadId": state.thread_id,\n                                "objective": goal_objective,\n                            },\n                        )\n                    await _inject_codex_turn(\n                        client,\n                        bridge_dir=self._bridge_dir,\n                        state=state,\n                        input_items=input_items,\n                        settings_overrides=settings_overrides,\n                    )\n''',
            encoding="utf-8",
        )
        patcher.patch_executor(executor)
        assert "preflight_rotation_request" in executor.read_text(encoding="utf-8")

        forwarder = td / "forwarder.py"
        forwarder.write_text(
            '''async def a():\n        await _post_turn_status_edge(\n            client,\n            session_id,\n            _CodexTurnStatusEdge(\n                status="failed",\n                turn_id=turn_id,\n                source="error",\n                error=error,\n            ),\n        )\n\nasync def b():\n    handled = terminal.handled\n    if terminal.edge is not None:\n        await _post_turn_status_edge(client, session_id, terminal.edge)\n''',
            encoding="utf-8",
        )
        patcher.patch_forwarder(forwarder)
        assert "request_rotation_from_usage_error" in forwarder.read_text(encoding="utf-8")

        trusted_origins = td / "ws_origin.py"
        trusted_origins.write_text(
            'import logging\nimport os\n_ALLOWED_ORIGINS_ENV = "OMNIGENT_WS_ALLOWED_ORIGINS"\n\n'
            'def parse_allowed_origins() -> frozenset[str]:\n'
            '    raw = os.environ.get(_ALLOWED_ORIGINS_ENV, "")\n'
            '    return frozenset(part.strip() for part in raw.split(",") if part.strip())\n'
        )
        patcher.patch_trusted_origins(trusted_origins)
        assert "omni-route-trusted-origins.json" in trusted_origins.read_text(encoding="utf-8")


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_pool(tmp)
        test_quota_shapes()
        test_error_variants()
        test_auth_and_runtime(tmp)
        test_optional_claude_config(tmp)
        test_wait(tmp)
    test_patcher_synthetic()
    print("subscription-rotation self-test: PASS")


if __name__ == "__main__":
    main()
