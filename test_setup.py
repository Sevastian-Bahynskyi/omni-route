import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import configure_subscriptions as setup


class SetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for key, path in {"CONFIG": self.root / "pool.json", "STATE": self.root / "state.json", "ROOT": self.root / "codex", "CLAUDE_ROOT": self.root / "claude"}.items():
            patcher = patch.object(setup, key, path)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_addition_preserves_policy_state_and_legacy_login(self) -> None:
        setup.CONFIG.write_text(json.dumps({"enabled": False, "rotate_at_percent": 88, "claude_fallback_agent": "claude-native-ui", "accounts": [{"name": "codex-1", "auth_json": "/some/auth.json"}]}))
        state = '{"sessions":{"s":"codex-1"},"cooldowns":{"codex-1":{"retry_at":123}}}'
        setup.STATE.write_text(state)
        with patch.dict(os.environ, {}, clear=True):
            accounts = setup._load()
        self.assertEqual(accounts[-1]["provider"], "claude")
        self.assertTrue(accounts[-1]["use_default_config"])
        setup._save(accounts)
        saved = json.loads(setup.CONFIG.read_text())
        self.assertFalse(saved["enabled"])
        self.assertEqual(saved["rotate_at_percent"], 88)
        self.assertNotIn("claude_fallback_agent", saved)
        self.assertEqual(setup.STATE.read_text(), state)
        self.assertEqual(len(setup._load()), 2)

    def test_multiple_claude_logins_have_distinct_scoped_environments(self) -> None:
        calls = []
        def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append((args, kwargs["env"]))
            return subprocess.CompletedProcess(args, 0, json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
        accounts: list[dict[str, object]] = []
        with patch.object(setup.shutil, "which", return_value="claude"), patch.object(setup.subprocess, "run", side_effect=run), patch.dict(os.environ, {"ANTHROPIC_API_KEY": "do-not-use", "CLAUDE_SECURESTORAGE_CONFIG_DIR": "shared"}), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(setup._add_claude(accounts))
            self.assertTrue(setup._add_claude(accounts))
        self.assertEqual([a["name"] for a in accounts], ["claude-1", "claude-2"])
        self.assertNotEqual(calls[0][1]["CLAUDE_CONFIG_DIR"], calls[2][1]["CLAUDE_CONFIG_DIR"])
        for _, env in calls:
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("CLAUDE_SECURESTORAGE_CONFIG_DIR", env)

    def test_claude_only_setup_can_finish(self) -> None:
        setup.CONFIG.write_text(json.dumps({"accounts": [{"name": "claude-1", "provider": "claude", "config_dir": "/private/profile"}]}))
        with patch("builtins.input", return_value="done"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.main(), 0)
        self.assertTrue(json.loads(setup.CONFIG.read_text())["enabled"])

    def test_broken_config_is_not_overwritten(self) -> None:
        setup.CONFIG.write_text("broken")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.main(), 1)
        self.assertEqual(setup.CONFIG.read_text(), "broken")

    def test_api_auth_does_not_register_subscription(self) -> None:
        result = subprocess.CompletedProcess([], 0, json.dumps({"loggedIn": True, "authMethod": "api_key"}))
        accounts: list[dict[str, object]] = []
        with patch.object(setup.shutil, "which", return_value="claude"), patch.object(setup.subprocess, "run", return_value=result), contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(setup._add_claude(accounts))
        self.assertEqual(accounts, [])


if __name__ == "__main__":
    unittest.main()
