from __future__ import annotations

# omni-route: skip when the legacy runtime is absent. These cover the
# Omnigent-coupled path; test_routing_native.py covers the same routing
# behaviour without fastapi.
import importlib.util as _importlib_util
import sys as _sys

if _importlib_util.find_spec("fastapi") is None:
    print("skipped: fastapi is not installed; the legacy path is not present")
    _sys.exit(0)


import importlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import apply_patch


class ClaudeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.checkout = Path.home() / ".local/share/omnigent-subscription-rotation/omnigent"
        if not (cls.checkout / ".git").exists():
            raise unittest.SkipTest("Pinned Omnigent checkout is required")
        cls.scratch = tempfile.TemporaryDirectory()
        overlay = Path(cls.scratch.name)
        for rel, patches in (
            ("codex_native_app_server.py", [apply_patch.patch_app_server]),
            ("runner/native/orchestration.py", [apply_patch.patch_orchestration, apply_patch.patch_claude_orchestration]),
            ("claude_native_bridge.py", [apply_patch.patch_claude_bridge]),
            ("claude_native_forwarder.py", [apply_patch.patch_claude_forwarder]),
            ("codex_native_forwarder.py", [apply_patch.patch_forwarder]),
            ("inner/codex_native_executor.py", [apply_patch.patch_executor]),
        ):
            target = overlay / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(subprocess.check_output([
                "git", "-C", str(cls.checkout), "show", f"{apply_patch.PINNED}:omnigent/{rel}",
            ]))
            for patch_file in patches:
                patch_file(target)
            compile(target.read_text(), str(target), "exec")
        for name in ("codex_account_pool.py", "codex_account_rotation.py", "claude_account_integration.py"):
            shutil.copy2(Path(__file__).parent / "payload" / name, overlay / name)
        sys.path.insert(0, str(cls.checkout))
        import omnigent
        omnigent.__path__.insert(0, str(overlay))
        cls.bridge = importlib.import_module("omnigent.claude_native_bridge")
        cls.integration = importlib.import_module("omnigent.claude_account_integration")
        cls.pool = importlib.import_module("omnigent.codex_account_pool")
        import omnigent.runner.native
        omnigent.runner.native.__path__.insert(0, str(overlay / "runner/native"))
        importlib.import_module("omnigent.runner.native.orchestration")
        importlib.import_module("omnigent.claude_native_forwarder")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scratch.cleanup()

    def test_structured_hook_error_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "hooks.jsonl").write_text(json.dumps({"payload": {
                "hook_event_name": "StopFailure", "error": "rate_limit",
            }}) + "\n")
            result = self.bridge.read_hook_events_from_offset(root, 0, start_event_count=0)
            self.assertEqual(result.records[0].account_error, "rate_limit")

    def test_wrapped_claude_draft_is_detected_across_composer_rows(self) -> None:
        pane = """assistant output
─────────────────────────────────────────
❯ How would you continue from this
  point?
─────────────────────────────────────────
  status
"""
        self.assertTrue(self.bridge._draft_in_input_box(pane, "How would you continue f"))
        self.assertFalse(self.bridge._draft_in_input_box(pane, "different draft"))

    def test_environment_and_history_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, "home", return_value=Path(directory)):
            root = Path(directory)
            shared_skill = root / ".agents/skills/implement"
            shared_skill.mkdir(parents=True)
            (shared_skill / "SKILL.md").write_text("---\nname: implement\n---\n")
            profile = self.pool.AccountProfile("claude-1", None, "claude", root / "account")
            env, unset = self.integration.prepare_account_environment(profile, {
                "ANTHROPIC_PROFILE": "wrong", "ANTHROPIC_API_KEY": "wrong",
                "CLAUDE_CODE_OAUTH_TOKEN": "wrong", "ENABLE_TOOL_SEARCH": "true",
            }, workspace=root / "project")
            self.assertEqual(env, {"CLAUDE_CONFIG_DIR": str(root / "account"), "ENABLE_TOOL_SEARCH": "true"})
            self.assertIn("ANTHROPIC_PROFILE", unset)
            self.assertNotIn("CLAUDE_CONFIG_DIR", unset)
            self.assertEqual((root / "account/projects").resolve(), (root / ".claude/projects").resolve())
            self.assertEqual((root / "account/skills/implement").resolve(), shared_skill.resolve())
            account_config = json.loads((root / "account/.claude.json").read_text())
            self.assertTrue(account_config["hasCompletedOnboarding"])
            self.assertTrue(
                account_config["projects"][str((root / "project").resolve())]["hasTrustDialogAccepted"]
            )
            self.assertEqual((root / "account/.claude.json").stat().st_mode & 0o777, 0o600)
            (root / ".claude/projects/session.jsonl").write_text("history")
            self.integration.prepare_account_environment(profile, {})
            self.assertEqual((root / "account/projects/session.jsonl").read_text(), "history")

    def test_existing_account_history_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, "home", return_value=Path(directory)):
            root = Path(directory)
            account_projects = root / "account/projects/workspace"
            account_projects.mkdir(parents=True)
            (account_projects / "session.jsonl").write_text("existing session")
            profile = self.pool.AccountProfile("claude-1", None, "claude", root / "account")
            self.integration.prepare_account_environment(profile, {})
            self.assertEqual((root / ".claude/projects/workspace/session.jsonl").read_text(), "existing session")
            self.assertEqual(len(list((root / "account").glob("projects.before-pool-*"))), 1)


    def test_claude_quota_requests_shared_pool_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("omnigent.codex_native_bridge.bridge_dir_for_bridge_id", return_value=root):
                self.pool.record_runtime_account(root, session_id="session-1", account_name="claude-1", provider="claude")
                self.assertTrue(self.integration.request_claude_rotation("session-1"))
                request = self.pool.read_rotation_request(root)
                self.assertEqual(request["account_name"], "claude-1")
                self.assertTrue(request["replay_required"])
                self.pool.clear_rotation_request(root)
                self.pool.record_runtime_account(root, session_id="session-1", account_name="codex-1", provider="codex")
                self.assertFalse(self.integration.request_claude_rotation("session-1"))
                self.assertIsNone(self.pool.read_rotation_request(root))


    def test_default_history_is_not_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, "home", return_value=Path(directory)):
            root = Path(directory)
            projects = root / ".claude/projects"
            projects.mkdir(parents=True)
            (projects / "session.jsonl").write_text("history")
            profile = self.pool.AccountProfile("legacy", None, "claude", root / ".claude")
            self.integration.prepare_account_environment(profile, {})
            self.assertEqual((projects / "session.jsonl").read_text(), "history")


if __name__ == "__main__":
    unittest.main()
