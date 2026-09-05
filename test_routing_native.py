#!/usr/bin/env python3
"""Routing tests against the vendored pool.

These cover the behaviour the original Omnigent-coupled routing tests
protected -- route order, per-provider selection, cooldown and recovery,
independent session bindings -- with no Omnigent dependency.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import account_pool as accounts


class RoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        # Claude credentials are checked by shelling out to `claude auth status`,
        # which cannot succeed for a temporary fixture profile.
        self._auth = patch.object(accounts, "account_has_credential", return_value=True)
        self._auth.start()
        self.addCleanup(self._auth.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for name in ("a.json", "d.json"):
            (self.root / name).write_text(
                json.dumps({"tokens": {"access_token": "x", "account_id": "y"}}),
                encoding="utf-8",
            )
        for name in ("b", "c"):
            directory = self.root / name
            directory.mkdir()
            (directory / ".claude.json").write_text(
                json.dumps({"oauthAccount": {"accountUuid": f"uuid-{name}"}}),
                encoding="utf-8",
            )
        self.a = accounts.AccountProfile("codex-a", self.root / "a.json")
        self.d = accounts.AccountProfile("codex-d", self.root / "d.json")
        self.b = accounts.AccountProfile("claude-b", provider="claude", config_dir=self.root / "b")
        self.c = accounts.AccountProfile("claude-c", provider="claude", config_dir=self.root / "c")
        self.pool = accounts.AccountPool(
            accounts.PoolConfig((self.a, self.b, self.d, self.c)),
            state_path=self.root / "state.json",
            now=lambda: 1000,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_selects_first_account_in_route_order(self) -> None:
        self.assertEqual(self.pool.account_for_session("s1").name, "codex-a")

    def test_selects_first_account_of_a_requested_provider(self) -> None:
        self.assertEqual(
            self.pool.account_for_session("s1", provider="claude").name, "claude-b"
        )

    def test_session_binding_is_stable(self) -> None:
        first = self.pool.account_for_session("s1")
        self.assertEqual(self.pool.account_for_session("s1").name, first.name)

    def test_parallel_sessions_keep_independent_bindings(self) -> None:
        codex = self.pool.account_for_session("s-codex", provider="codex")
        claude = self.pool.account_for_session("s-claude", provider="claude")
        self.assertEqual(codex.name, "codex-a")
        self.assertEqual(claude.name, "claude-b")
        self.assertEqual(self.pool.account_for_session("s-codex").name, "codex-a")

    def test_rotation_cools_the_exhausted_account_and_moves_on(self) -> None:
        self.pool.account_for_session("s1", provider="codex")
        nxt = self.pool.rotate_session(
            "s1", exhausted_account="codex-a", retry_at=2000, reason="quota",
        )
        self.assertEqual(nxt.name, "codex-d", "must stay on the same provider")
        state = self.pool.snapshot()
        self.assertIn("codex-a", state["cooldowns"])

    def test_rotation_can_cross_providers_when_asked(self) -> None:
        self.pool.rotate_session("s1", exhausted_account="codex-a", retry_at=2000, reason="q")
        nxt = self.pool.rotate_session(
            "s1", exhausted_account="codex-d", retry_at=2000, reason="q",
            fallback_to_other_providers=True,
        )
        self.assertEqual(nxt.provider, "claude")

    def test_exhausted_pool_returns_nothing(self) -> None:
        for name in ("codex-a", "codex-d", "claude-b", "claude-c"):
            self.pool.rotate_session("s1", exhausted_account=name, retry_at=2000, reason="q")
        self.assertIsNone(
            self.pool.rotate_session("s1", exhausted_account=None, retry_at=None, reason="q")
        )

    def test_recovered_account_is_selectable_again(self) -> None:
        self.pool.rotate_session("s1", exhausted_account="codex-a", retry_at=2000, reason="q")
        later = accounts.AccountPool(
            self.pool.config, state_path=self.root / "state.json", now=lambda: 3000,
        )
        self.assertEqual(later.account_for_session("s2", provider="codex").name, "codex-a")

    def test_claude_environment_excludes_overrides(self) -> None:
        env = accounts.claude_account_env(self.b)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.b.config_dir))

    def test_threshold_is_clamped_on_load(self) -> None:
        config = self.root / "pool.json"
        config.write_text(json.dumps({
            "enabled": True,
            "rotate_at_percent": 99,
            "accounts": [{"name": "codex-a", "provider": "codex",
                          "auth_json": str(self.root / "a.json")}],
        }), encoding="utf-8")
        loaded = accounts.PoolConfig.load(config)
        self.assertEqual(loaded.rotate_at_percent, 95.0)
        self.assertEqual(loaded.preparation_at_percent, 92.0)

    def test_claude_only_pool_loads(self) -> None:
        config = self.root / "claude-only.json"
        config.write_text(json.dumps({
            "enabled": True,
            "accounts": [{"name": "claude-b", "provider": "claude",
                          "config_dir": str(self.root / "b")}],
        }), encoding="utf-8")
        loaded = accounts.PoolConfig.load(config)
        self.assertEqual(loaded.accounts[0].provider, "claude")


if __name__ == "__main__":
    unittest.main(verbosity=1)
