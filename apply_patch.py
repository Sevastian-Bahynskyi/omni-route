from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PINNED = "2b13f2d7d85431c06e510d3c707c0c6d9a191a44"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one anchor, found {count}")
    return text.replace(old, new, 1)


def patch_app_server(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    text = replace_once(
        text,
        "    cwd: Path\n    bridge_dir: Path\n    developer_instructions: str | None = None\n",
        "    cwd: Path\n    bridge_dir: Path\n    auth_json_source: Path | None = None\n    developer_instructions: str | None = None\n",
        "CodexNativeAppServer.auth_json_source",
    )

    old = '''        await asyncio.to_thread(\n            _populate_codex_home_config,\n            self.codex_home,\n            config_source,\n            inject_hooks=self.router_hooks_registered,\n            extend_model_catalog=codex_extended_catalog_requested(self.env),\n        )\n        if self.trust_project:\n'''
    new = '''        await asyncio.to_thread(\n            _populate_codex_home_config,\n            self.codex_home,\n            config_source,\n            inject_hooks=self.router_hooks_registered,\n            extend_model_catalog=codex_extended_catalog_requested(self.env),\n        )\n        if self.auth_json_source is not None:\n            from omnigent.codex_account_pool import bind_account_auth\n\n            await asyncio.to_thread(\n                bind_account_auth,\n                self.codex_home,\n                self.auth_json_source,\n            )\n        if self.trust_project:\n'''
    text = replace_once(text, old, new, "bind selected Codex account")

    text = replace_once(
        text,
        "    trust_project: bool = False,\n    trust_all_hooks: bool = False,\n) -> CodexNativeAppServer:\n",
        "    trust_project: bool = False,\n    trust_all_hooks: bool = False,\n    auth_json_source: Path | None = None,\n) -> CodexNativeAppServer:\n",
        "build_codex_native_server auth parameter",
    )

    text = replace_once(
        text,
        "    if extra_config_overrides:\n        config_overrides.extend(extra_config_overrides)\n",
        "    if extra_config_overrides:\n"
        "        config_overrides.extend(extra_config_overrides)\n"
        "    if auth_json_source is not None:\n"
        "        # Account profiles are ordinary Codex file-backed OAuth stores.\n"
        "        # Pin file mode so macOS Keychain cannot silently override them.\n"
        "        config_overrides.append('cli_auth_credentials_store=\"file\"')\n",
        "force file auth storage for pooled accounts",
    )

    text = replace_once(
        text,
        "        cwd=cwd,\n        bridge_dir=bridge_dir,\n        developer_instructions=developer_instructions,\n",
        "        cwd=cwd,\n        bridge_dir=bridge_dir,\n        auth_json_source=auth_json_source,\n        developer_instructions=developer_instructions,\n",
        "builder forwards selected account",
    )

    path.write_text(text, encoding="utf-8")


def patch_orchestration(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    anchor = '''    bridge_dir = prepare_bridge_dir(session_id)\n    socket_path = socket_path_for_bridge_dir(bridge_dir)\n    codex_home = codex_home_for_bridge_dir(bridge_dir)\n'''
    insertion = anchor + '''    from omnigent.codex_account_pool import CodexAccountPool\n\n    _account_pool = CodexAccountPool.from_default()\n    _account_profile = (\n        _account_pool.account_for_session(session_id)\n        if _account_pool.enabled\n        else None\n    )\n    if _account_pool.enabled and _account_profile is None:\n        raise RuntimeError(\n            "All configured subscription accounts are currently unavailable."\n        )\n'''
    insertion += '    if _account_profile is not None and _account_profile.provider != "codex":\n        raise RuntimeError("Selected account requires the Claude provider; restart through omni-route.")\n'
    text = replace_once(text, anchor, insertion, "select pooled Codex account")

    text = replace_once(
        text,
        "        extra_config_overrides=[*_codex_launch.config_overrides, *mcp_overrides],\n        bridge_dir=bridge_dir,\n        ap_server_url=launch_config.policy_server_url,\n",
        "        extra_config_overrides=[*_codex_launch.config_overrides, *mcp_overrides],\n"
        "        bridge_dir=bridge_dir,\n"
        "        auth_json_source=(\n"
        "            _account_profile.auth_json if _account_profile is not None else None\n"
        "        ),\n"
        "        ap_server_url=launch_config.policy_server_url,\n",
        "pass selected account to app-server",
    )

    text = replace_once(
        text,
        "    await app_server.start()\n    _AUTO_CODEX_APP_SERVERS[session_id] = app_server\n",
        "    await app_server.start()\n"
        "    _AUTO_CODEX_APP_SERVERS[session_id] = app_server\n"
        "    if _account_profile is not None:\n"
        "        from omnigent.codex_account_pool import record_runtime_account\n\n"
        "        record_runtime_account(\n"
        "            bridge_dir,\n"
        "            session_id=session_id,\n"
        "            account_name=_account_profile.name,\n"
        "        )\n",
        "record active pooled account",
    )

    old_end = '''    if ensure_comment_relay is not None:\n        await ensure_comment_relay(session_id, explicit_bridge_dir=bridge_dir, await_notify=False)\n\n    _logger.info(\n        "Auto-created codex terminal + forwarder for session %s",\n        session_id,\n    )\n    return terminal_view\n'''
    new_end = '''    if ensure_comment_relay is not None:\n        await ensure_comment_relay(session_id, explicit_bridge_dir=bridge_dir, await_notify=False)\n\n    if _account_profile is not None:\n        from omnigent.codex_account_rotation import ensure_rotation_monitor\n\n        async def _relaunch_for_account_rotation() -> None:\n            terminal_registry = resource_registry.terminal_registry\n            if terminal_registry is not None:\n                await terminal_registry.close(session_id, "codex", "main")\n            await _auto_create_codex_terminal(\n                session_id,\n                resource_registry,\n                publish_event,\n                bundle_dir=bundle_dir,\n                skills_filter=skills_filter,\n                agent_spec=agent_spec,\n                server_client=server_client,\n                ensure_comment_relay=ensure_comment_relay,\n            )\n\n        ensure_rotation_monitor(\n            session_id=session_id,\n            bridge_dir=bridge_dir,\n            pool=_account_pool,\n            server_client=server_client,\n            relaunch=_relaunch_for_account_rotation,\n        )\n\n    _logger.info(\n        "Auto-created codex terminal + forwarder for session %s",\n        session_id,\n    )\n    return terminal_view\n'''
    text = replace_once(text, old_end, new_end, "start account rotation monitor")

    path.write_text(text, encoding="utf-8")


def patch_claude_orchestration(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    anchor = '    claude_config: ClaudeNativeUcodeConfig | None = None\n'
    insert = """
    from omnigent.codex_account_pool import CodexAccountPool
    from omnigent.claude_account_integration import prepare_account_environment
    from omnigent.codex_native_bridge import bridge_dir_for_bridge_id as _account_bridge_dir

    _account_pool = CodexAccountPool.from_default()
    _account_profile = _account_pool.account_for_session(session_id) if _account_pool.enabled else None
    if _account_pool.enabled and _account_profile is None:
        raise RuntimeError("All configured subscription accounts are currently unavailable.")
    if _account_profile is not None and _account_profile.provider != "claude":
        raise RuntimeError("Selected subscription account requires a different provider.")
""" + anchor
    text = replace_once(text, anchor, insert, "select pooled Claude account")
    text = replace_once(text, "        if resolve_launch_config is not None:\n", "        if _account_profile is not None:\n            claude_config = None\n        elif resolve_launch_config is not None:\n", "use subscription Claude authentication")
    anchor = '    claude_terminal_env_unset = _claude_terminal_env_unset(claude_config)\n'
    insert = anchor + """    _account_env = build_native_claude_terminal_env(claude_config)
    if _account_profile is not None:
        _account_env, _account_unset = prepare_account_environment(_account_profile, _account_env)
        claude_terminal_env_unset = list(set(claude_terminal_env_unset + _account_unset))
"""
    text = replace_once(text, anchor, insert, "select pooled Claude account")
    text = replace_once(text, '        env=build_native_claude_terminal_env(claude_config),\n', '        env=_account_env,\n', "isolate Claude account environment")
    anchor = '    _register_auto_forwarder_task(session_id, _forwarder_task)\n    _logger.info(\n        "Auto-created claude terminal + forwarder for session %s; "'
    insert = """    _register_auto_forwarder_task(session_id, _forwarder_task)
    if _account_profile is not None:
        from omnigent.codex_account_pool import record_runtime_account
        from omnigent.codex_account_rotation import ensure_rotation_monitor

        _runtime_bridge = _account_bridge_dir(session_id)
        record_runtime_account(_runtime_bridge, session_id=session_id,
                               account_name=_account_profile.name, provider="claude")

        async def _relaunch_for_account_rotation() -> None:
            terminal_registry = resource_registry.terminal_registry
            if terminal_registry is not None:
                await terminal_registry.close(session_id, "claude", "main")
            await _auto_create_claude_terminal(
                session_id, resource_registry, publish_event,
                server_client=server_client, bundle_dir=bundle_dir,
                agent_name=agent_name, agent_spec=agent_spec,
                skills_filter=skills_filter,
                auth_token_factory=auth_token_factory,
                resolve_launch_config=resolve_launch_config,
                record_launch_config=record_launch_config,
            )

        ensure_rotation_monitor(session_id=session_id, bridge_dir=_runtime_bridge,
                                pool=_account_pool, server_client=server_client,
                                relaunch=_relaunch_for_account_rotation)
    _logger.info(
        "Auto-created claude terminal + forwarder for session %s; \""""
    text = replace_once(text, anchor, insert, "start Claude account rotation monitor")
    path.write_text(text, encoding="utf-8")


def patch_claude_bridge(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    text = replace_once(text, '    event_name: str | None\n    recorded_at:',
                        '    event_name: str | None\n    account_error: str | None = None\n    recorded_at:', "Claude hook structured error field")
    text = replace_once(text, '    return ClaudeHookRecord(\n',
                        '    return ClaudeHookRecord(\n        account_error=(payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), str) else None),\n', "parse Claude hook structured error")
    path.write_text(text, encoding="utf-8")


def patch_claude_forwarder(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    anchor = '        if status is None:\n            # Compaction boundary'
    insert = """        if record.event_name == "StopFailure" and record.account_error in {
            "rate_limit", "rate_limit_error", "usage_limit_exceeded",
        }:
            from omnigent.claude_account_integration import request_claude_rotation

            if request_claude_rotation(session_id):
                status = "idle"
        if status is None:
            # Compaction boundary"""
    text = replace_once(text, anchor, insert, "rotate Claude on structured quota failure")
    path.write_text(text, encoding="utf-8")


def patch_executor(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    old = '''                await client.connect()\n                try:\n                    if goal_objective is not None:\n                        await client.request(\n                            "thread/goal/set",\n                            {\n                                "threadId": state.thread_id,\n                                "objective": goal_objective,\n                            },\n                        )\n                    await _inject_codex_turn(\n                        client,\n                        bridge_dir=self._bridge_dir,\n                        state=state,\n                        input_items=input_items,\n                        settings_overrides=settings_overrides,\n                    )\n'''
    new = '''                await client.connect()\n                try:\n                    skip_codex_turn = False\n                    if state.active_turn_id is None:\n                        from omnigent.codex_account_pool import (\n                            preflight_rotation_request,\n                            wait_for_rotation,\n                        )\n\n                        requested, generation = await preflight_rotation_request(\n                            client,\n                            self._bridge_dir,\n                            session_id=state.session_id,\n                        )\n                        if requested:\n                            await client.close()\n                            runtime = await wait_for_rotation(\n                                self._bridge_dir,\n                                after_generation=generation,\n                            )\n                            if runtime is None:\n                                raise RuntimeError(\n                                    "Timed out waiting for Codex subscription rotation"\n                                )\n                            mode = runtime.get("mode")\n                            if mode == "codex":\n                                state = read_bridge_state(self._bridge_dir)\n                                if state is None:\n                                    raise RuntimeError(\n                                        "Codex bridge did not recover after account rotation"\n                                    )\n                                client = client_for_transport(\n                                    state.socket_path,\n                                    client_name="omnigent-codex-native",\n                                )\n                                await client.connect()\n                            elif mode in {"claude_pending", "claude"}:\n                                # The supervisor will switch this SAME Omnigent\n                                # session after this native turn returns idle.\n                                skip_codex_turn = True\n                            else:\n                                raise RuntimeError(\n                                    "No subscription fallback is available: "\n                                    f"{runtime.get('detail') or mode}"\n                                )\n\n                    if not skip_codex_turn and goal_objective is not None:\n                        await client.request(\n                            "thread/goal/set",\n                            {\n                                "threadId": state.thread_id,\n                                "objective": goal_objective,\n                            },\n                        )\n                    if not skip_codex_turn:\n                        await _inject_codex_turn(\n                            client,\n                            bridge_dir=self._bridge_dir,\n                            state=state,\n                            input_items=input_items,\n                            settings_overrides=settings_overrides,\n                        )\n'''
    text = replace_once(text, old, new, "Codex quota preflight and rotation wait")
    path.write_text(text, encoding="utf-8")


def patch_forwarder(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    old = '''        await _post_turn_status_edge(\n            client,\n            session_id,\n            _CodexTurnStatusEdge(\n                status="failed",\n                turn_id=turn_id,\n                source="error",\n                error=error,\n            ),\n        )\n'''
    new = '''        from omnigent.codex_account_pool import request_rotation_from_usage_error\n\n        _rotating_account = request_rotation_from_usage_error(\n            bridge_dir,\n            session_id=session_id,\n            payload=params,\n        )\n        await _post_turn_status_edge(\n            client,\n            session_id,\n            _CodexTurnStatusEdge(\n                status="idle" if _rotating_account else "failed",\n                turn_id=turn_id,\n                source="error:account-rotation" if _rotating_account else "error",\n                error=None if _rotating_account else error,\n            ),\n        )\n'''
    text = replace_once(text, old, new, "structured standalone quota error rotation")

    old2 = '''    handled = terminal.handled\n    if terminal.edge is not None:\n        await _post_turn_status_edge(client, session_id, terminal.edge)\n'''
    new2 = '''    handled = terminal.handled\n    _rotating_account = False\n    if terminal.edge is not None and terminal.edge.error is not None:\n        from omnigent.codex_account_pool import request_rotation_from_usage_error\n\n        _rotating_account = request_rotation_from_usage_error(\n            bridge_dir,\n            session_id=session_id,\n            payload=params,\n        )\n    if terminal.edge is not None:\n        if _rotating_account:\n            await _post_turn_status_edge(\n                client,\n                session_id,\n                _CodexTurnStatusEdge(\n                    status="idle",\n                    turn_id=terminal.edge.turn_id,\n                    source=f"{terminal.edge.source}:account-rotation",\n                ),\n            )\n        else:\n            await _post_turn_status_edge(client, session_id, terminal.edge)\n'''
    text = replace_once(text, old2, new2, "structured terminal quota error rotation")

    path.write_text(text, encoding="utf-8")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_patch.py /path/to/omnigent")
    root = Path(sys.argv[1]).expanduser().resolve()
    if not (root / ".git").exists():
        raise RuntimeError(f"{root} is not a git checkout")

    head = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    if head != PINNED:
        raise RuntimeError(
            f"Refusing to patch incompatible Omnigent source. Expected {PINNED}, found {head}."
        )

    here = Path(__file__).resolve().parent
    shutil.copy2(here / "payload" / "codex_account_pool.py", root / "omnigent" / "codex_account_pool.py")
    shutil.copy2(here / "payload" / "codex_account_rotation.py", root / "omnigent" / "codex_account_rotation.py")

    patch_app_server(root / "omnigent" / "codex_native_app_server.py")
    patch_orchestration(root / "omnigent" / "runner" / "native" / "orchestration.py")
    shutil.copy2(here / "payload" / "claude_account_integration.py", root / "omnigent" / "claude_account_integration.py")
    patch_claude_orchestration(root / "omnigent" / "runner" / "native" / "orchestration.py")
    patch_claude_bridge(root / "omnigent" / "claude_native_bridge.py")
    patch_claude_forwarder(root / "omnigent" / "claude_native_forwarder.py")
    patch_executor(root / "omnigent" / "inner" / "codex_native_executor.py")
    patch_forwarder(root / "omnigent" / "codex_native_forwarder.py")

    print("Omnigent subscription-rotation patch applied successfully.")


if __name__ == "__main__":
    main()
