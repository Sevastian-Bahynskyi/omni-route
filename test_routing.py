from __future__ import annotations

# omni-route: skip when the legacy runtime is absent. These cover the
# Omnigent-coupled path; test_routing_native.py covers the same routing
# behaviour without omnigent.
import importlib.util as _importlib_util
import sys as _sys

if _importlib_util.find_spec("omnigent") is None:
    print("skipped: omnigent is not installed; the legacy path is not present")
    _sys.exit(0)


import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from omnigent import codex_account_pool as accounts
from omnigent import codex_account_rotation as rotation
import switch_provider as manual_switch


class RoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.a = accounts.AccountProfile('codex-a', self.root / 'a.json')
        self.d = accounts.AccountProfile('codex-d', self.root / 'd.json')
        self.b = accounts.AccountProfile('claude-b', provider='claude', config_dir=self.root / 'b')
        self.c = accounts.AccountProfile('claude-c', provider='claude', config_dir=self.root / 'c')
        self.pool = accounts.CodexAccountPool(accounts.PoolConfig((self.a, self.b, self.d, self.c)), state_path=self.root / 'state.json', now=lambda: 1000)
        self.auth = patch.object(accounts, 'account_has_credential', return_value=True)
        self.auth.start()
        self.addCleanup(self.auth.stop)

    def test_mixed_order_cooldown_and_recovery(self) -> None:
        self.assertEqual(self.pool.account_for_session('s'), self.a)

    def test_new_session_selects_first_account_for_requested_provider(self) -> None:
        self.assertEqual(self.pool.account_for_session('claude-session', provider='claude'), self.b)
        self.assertEqual(self.pool.account_for_session('codex-session', provider='codex'), self.a)

    def test_requested_provider_rebinds_an_incompatible_session(self) -> None:
        self.assertEqual(self.pool.account_for_session('switching-agent'), self.a)
        self.assertEqual(
            self.pool.account_for_session('switching-agent', provider='claude'),
            self.b,
        )
        self.assertEqual(self.pool.rotate_session('s', exhausted_account=self.a.name, retry_at=2000, reason='quota'), self.d)
        self.assertEqual(self.pool.rotate_session('s', exhausted_account=self.b.name, retry_at=2000, reason='quota'), self.c)
        self.assertIsNone(self.pool.rotate_session('s', exhausted_account=self.c.name, retry_at=2000, reason='quota'))
        self.pool._now = lambda: 2001
        self.assertEqual(self.pool.account_for_session('s'), self.a)

    def test_claude_only_and_legacy_load(self) -> None:
        config = self.root / 'config.json'
        config.write_text(json.dumps({'accounts': [{'name': 'claude-b', 'provider': 'claude', 'config_dir': str(self.root / 'b')}]}))
        loaded = accounts.PoolConfig.load(config)
        self.assertTrue(loaded.enabled)
        self.assertEqual(loaded.accounts[0].provider, 'claude')
        config.write_text(json.dumps({'accounts': [], 'claude_fallback_agent': 'claude-native-ui'}))
        self.assertEqual(accounts.PoolConfig.load(config).accounts[0].provider, 'claude')

    def test_subscription_environment_excludes_overrides(self) -> None:
        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'not-real', 'CLAUDE_SECURESTORAGE_CONFIG_DIR': '/wrong'}):
            env = accounts.claude_account_env(self.b)
        self.assertNotIn('ANTHROPIC_API_KEY', env)
        self.assertNotIn('CLAUDE_SECURESTORAGE_CONFIG_DIR', env)
        self.assertEqual(env['CLAUDE_CONFIG_DIR'], str(self.b.config_dir))

    def test_parallel_sessions_keep_independent_provider_bindings(self) -> None:
        self.assertEqual(self.pool.account_for_session('codex-session', provider='codex'), self.a)
        self.assertEqual(self.pool.account_for_session('claude-session', provider='claude'), self.b)
        self.assertEqual(self.pool.rotate_session('codex-session', exhausted_account=self.a.name, retry_at=2000, reason='quota'), self.d)
        snapshot = self.pool.snapshot()['session_bindings']
        self.assertEqual(snapshot['codex-session'], self.d.name)
        self.assertEqual(snapshot['claude-session'], self.b.name)

    def test_automatic_rotation_changes_provider_after_same_provider_is_exhausted(self) -> None:
        bridge = self.root / 'automatic-cross-provider'
        accounts.record_runtime_account(
            bridge, session_id='s', account_name=self.a.name, provider='codex'
        )
        accounts.request_rotation(
            bridge,
            session_id='s',
            account_name=self.a.name,
            retry_at=2000,
            reason='quota',
            replay_required=True,
        )
        with self.pool._locked_state() as state:
            state['cooldowns'] = {
                self.d.name: {'retry_at': 2000, 'reason': 'quota', 'marked_at': 1000}
            }
        with patch.object(
            accounts.CodexAccountPool, 'from_default', return_value=self.pool
        ), patch.object(rotation, 'switch_to_account', new_callable=AsyncMock) as switch:
            asyncio.run(
                rotation._monitor(
                    session_id='s',
                    bridge_dir=bridge,
                    pool=self.pool,
                    server_client=None,
                    relaunch=AsyncMock(),
                )
            )
        self.assertEqual(switch.await_args.kwargs['profile'], self.b)
        self.assertTrue(switch.await_args.kwargs['continue_after_switch'])

    def test_claude_to_claude_manual_does_not_cool_or_continue(self) -> None:
        async def scenario() -> None:
            bridge = self.root / 'manual'
            accounts.record_runtime_account(bridge, session_id='s', account_name=self.b.name, provider='claude')
            accounts._atomic_json(bridge / accounts.ROTATE_FILE, {'session_id': 's', 'account_name': self.b.name, 'manual': True, 'target_account': self.c.name})
            relaunched = asyncio.Event()
            async def relaunch() -> None:
                relaunched.set()
            with patch.object(accounts.CodexAccountPool, 'from_default', return_value=self.pool), patch.object(rotation, 'account_has_credential', return_value=True), patch.object(rotation, '_wait_idle', new_callable=AsyncMock, return_value=True), patch.object(rotation, '_continue_session', new_callable=AsyncMock) as continuation:
                task = asyncio.create_task(rotation._monitor(session_id='s', bridge_dir=bridge, pool=self.pool, server_client=None, relaunch=relaunch))
                await asyncio.wait_for(relaunched.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                continuation.assert_not_awaited()
            self.assertEqual(self.pool.snapshot()['session_bindings']['s'], self.c.name)
            self.assertFalse(self.pool.snapshot()['cooldowns'])
        asyncio.run(scenario())

    def test_recovered_exhausted_account_can_be_selected_again(self) -> None:
        async def scenario() -> None:
            bridge = self.root / 'recovered'
            accounts.record_runtime_account(bridge, session_id='s', account_name=self.b.name, provider='exhausted')
            runtime = accounts.read_runtime(bridge)
            runtime['active_provider'] = 'claude'
            accounts._atomic_json(bridge / accounts.RUNTIME_FILE, runtime)
            accounts._atomic_json(bridge / accounts.ROTATE_FILE, {'session_id': 's', 'account_name': self.b.name, 'manual': True, 'target_account': self.b.name})
            relaunched = asyncio.Event()
            async def relaunch() -> None:
                relaunched.set()
            with patch.object(accounts.CodexAccountPool, 'from_default', return_value=self.pool), patch.object(rotation, 'account_has_credential', return_value=True), patch.object(rotation, '_wait_idle', new_callable=AsyncMock, return_value=True):
                task = asyncio.create_task(rotation._monitor(session_id='s', bridge_dir=bridge, pool=self.pool, server_client=None, relaunch=relaunch))
                await asyncio.wait_for(relaunched.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        asyncio.run(scenario())

    def test_disabled_pool_does_not_launch_unpooled_cli(self) -> None:
        import routed_start
        disabled = accounts.CodexAccountPool(accounts.PoolConfig((self.a,), enabled=False), state_path=self.root / 'disabled.json')
        with patch.object(accounts.CodexAccountPool, 'from_default', return_value=disabled), patch('os.execv') as launch, patch('sys.stderr'):
            self.assertEqual(routed_start.main(), 1)
            launch.assert_not_called()

    def test_switch_continuation_cold_starts_new_provider(self) -> None:
        async def scenario() -> None:
            response = unittest.mock.Mock()
            response.status_code = 200
            response.json.return_value = {'data': [{'name': 'claude-native-ui', 'id': 'test-agent'}]}
            client = AsyncMock()
            client.get.return_value = response
            client.post.return_value = response
            with patch.object(rotation, '_wait_idle', new_callable=AsyncMock, return_value=True):
                await rotation.switch_to_account(session_id='s', bridge_dir=self.root, server_client=client, profile=self.b)
            self.assertEqual(client.post.await_count, 2)
            self.assertTrue(client.post.await_args_list[0].args[0].endswith('/switch-agent'))
            self.assertTrue(client.post.await_args_list[1].args[0].endswith('/events'))
            self.assertEqual(accounts.read_runtime(self.root)['phase'], 'selected')
        asyncio.run(scenario())

    def test_manual_same_provider_relaunches_native_terminal(self) -> None:
        async def scenario() -> None:
            response = unittest.mock.Mock()
            response.status_code = 200
            response.json.return_value = {
                'data': [{
                    'id': 'terminal_codex_main',
                    'metadata': {'terminal_name': 'codex', 'session_key': 'main'},
                }],
            }
            response.raise_for_status.return_value = None
            client = AsyncMock()
            client.get.return_value = response
            client.delete.return_value = response
            client.post.return_value = response
            await manual_switch._close_native_terminal(client, 's', 'codex')
            await manual_switch._launch_native_terminal(client, 's', 'codex')
            self.assertTrue(client.delete.await_args.args[0].endswith('/terminal_codex_main'))
            self.assertEqual(client.post.await_args.kwargs['json']['ensure_native_terminal'], True)
        asyncio.run(scenario())

    def test_manual_cross_provider_handoff_continues_existing_session(self) -> None:
        async def scenario() -> None:
            response = unittest.mock.Mock()
            response.raise_for_status.return_value = None
            client = AsyncMock()
            client.post.return_value = response
            await manual_switch._continue_provider_handoff(client, 's')
            request = client.post.await_args.kwargs['json']
            self.assertIn('existing conversation and workspace state', request['data']['content'][0]['text'])
        asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
