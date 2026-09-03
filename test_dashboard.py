import http.client
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import status_server as base
import status_server_ext as dashboard


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for key, value in {"CONFIG_PATH": self.root / "pool.json", "STATE_PATH": self.root / "state.json", "_AUTH_CACHE": {}}.items():
            patcher = patch.object(base, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for key, value in {"_latest_runtime": {}, "_remote_status": {}}.items():
            patcher = patch.object(dashboard, key, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.auth = self.root / "auth.json"
        self.auth.write_text('{}')
        self.accounts = [
            {"name": "codex-1", "auth_json": str(self.auth)},
            {"name": "claude-1", "provider": "claude", "config_dir": str(self.root / "claude-1")},
            {"name": "claude-2", "provider": "claude", "config_dir": str(self.root / "claude-2")},
        ]
        base.CONFIG_PATH.write_text(json.dumps({"accounts": self.accounts}))

    def test_mixed_status_keeps_claude_accounts_independent(self) -> None:
        base.STATE_PATH.write_text(json.dumps({"session_bindings": {"s1": "claude-2"}, "cooldowns": {"claude-1": {"retry_at": int(time.time()) + 3600}}}))
        with patch.object(base, "_claude_authenticated", return_value=True), patch.object(dashboard, "_latest_runtime", return_value={"mode": "claude", "account_name": "claude-2", "session_id": "s1"}):
            status = dashboard.collect_status()
        self.assertEqual(status["router"]["providerCounts"], {"codex": 1, "claude": 2})
        self.assertEqual(status["router"]["currentProvider"], "claude-2")
        self.assertEqual([account["status"] for account in status["accounts"]], ["ready", "cooldown", "active"])
        self.assertEqual(status["accounts"][2]["sessions"], 1)

    def test_claude_first_reorder_preserves_bindings_and_rejects_partial_order(self) -> None:
        state = json.dumps({"session_bindings": {"s1": "codex-1"}})
        base.STATE_PATH.write_text(state)
        order = ["claude-2", "codex-1", "claude-1"]
        with patch.object(base, "_claude_authenticated", return_value=True):
            status = base.update_route_order(order)
        self.assertEqual([account["name"] for account in status["accounts"]], order)
        self.assertEqual(base.STATE_PATH.read_text(), state)
        saved = base.CONFIG_PATH.read_text()
        for invalid in (["codex-1"], ["claude-2", "codex-1", "unknown"], ["claude-2", "codex-1", "codex-1"]):
            with self.subTest(order=invalid), self.assertRaises(ValueError):
                base.update_route_order(invalid)
            self.assertEqual(base.CONFIG_PATH.read_text(), saved)

    def test_legacy_migration_uses_first_available_suffix_and_default_login(self) -> None:
        base.CONFIG_PATH.write_text(json.dumps({"accounts": [{"name": "claude-legacy", "auth_json": str(self.auth)}], "claude_fallback_agent": "claude-native-ui"}))
        with patch.dict(os.environ, {}, clear=True), patch.object(base, "_claude_authenticated", return_value=True):
            base.update_route_order(["claude-legacy-1", "claude-legacy"])
        saved = json.loads(base.CONFIG_PATH.read_text())
        self.assertNotIn("claude_fallback_agent", saved)
        self.assertEqual(saved["accounts"][0]["provider"], "claude")
        self.assertTrue(saved["accounts"][0]["use_default_config"])
        self.assertEqual(len(base._configured_accounts(saved)), 2)

    def test_claude_auth_environment_isolates_profiles_and_rejects_api_auth(self) -> None:
        inherited = {"CLAUDE_CONFIG_DIR": "/shared", "ANTHROPIC_API_KEY": "test-only", "ANTHROPIC_PROFILE": "shared", "CLAUDE_SECURESTORAGE_CONFIG_DIR": "/shared", "CLAUDE_CODE_OAUTH_TOKEN": "test-only"}
        result = subprocess.CompletedProcess([], 0, json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
        with patch.dict(os.environ, inherited), patch.object(base.subprocess, "run", return_value=result) as run:
            self.assertTrue(base._claude_authenticated((str(self.root / "default"), True)))
            self.assertNotIn("CLAUDE_CONFIG_DIR", run.call_args.kwargs["env"])
            for name in ("one", "two"):
                path = str(self.root / name)
                self.assertTrue(base._claude_authenticated((path, False)))
                env = run.call_args.kwargs["env"]
                self.assertEqual(env["CLAUDE_CONFIG_DIR"], path)
                for key in inherited.keys() - {"CLAUDE_CONFIG_DIR"}:
                    self.assertNotIn(key, env)
            self.assertTrue(base._claude_authenticated((str(self.root / "two"), False)))
            self.assertEqual(run.call_count, 3)
            run.return_value = subprocess.CompletedProcess([], 0, json.dumps({"loggedIn": True, "authMethod": "api_key"}))
            self.assertFalse(base._claude_authenticated((str(self.root / "api"), False)))

    def _server(self) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.ControlHandler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        def close() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.addCleanup(close)
        return server

    def _post(self, server: ThreadingHTTPServer, path: str, body: dict[str, object], origin: str) -> int:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request("POST", path, json.dumps(body), {"Content-Type": "application/json", "Origin": origin})
            response = connection.getresponse()
            response.read()
            return response.status
        finally:
            connection.close()

    def test_cross_origin_posts_cannot_reorder_switch_or_enable_remote_access(self) -> None:
        server = self._server()
        saved = base.CONFIG_PATH.read_text()
        with patch.object(dashboard, "_run_switch") as switch, patch.object(dashboard.remote_access, "enable") as enable:
            for path, body in (("/api/route/order", {"accounts": ["claude-2", "codex-1", "claude-1"]}), ("/api/provider/current", {"provider": "claude-2"}), ("/api/remote-access/enable", {})):
                with self.subTest(path=path):
                    self.assertEqual(self._post(server, path, body, "https://untrusted.example"), 403)
            switch.assert_not_called()
            enable.assert_not_called()
        self.assertEqual(base.CONFIG_PATH.read_text(), saved)

    def test_provider_endpoint_rejects_unknown_names_and_accepts_named_claude(self) -> None:
        server = self._server()
        origin = f"http://127.0.0.1:{server.server_port}"
        with patch.object(dashboard, "_run_switch", return_value={"ok": True}) as switch:
            self.assertEqual(self._post(server, "/api/provider/current", {"provider": "unknown"}, origin), 400)
            switch.assert_not_called()
            self.assertEqual(self._post(server, "/api/provider/current", {"provider": "claude-2"}, origin), 200)
            switch.assert_called_once_with("claude-2", None)


if __name__ == "__main__":
    unittest.main()
