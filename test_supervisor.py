#!/usr/bin/env python3
"""Tests for supervisor decision-making and the rotation sequence."""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

import handoff
import quota
import supervisor
from account_pool import AccountProfile, PoolConfig, clamp_threshold, preparation_percent


def _usage(session=None, weekly=None, state="ready") -> quota.Usage:
    return quota.Usage(
        state=state,
        session=quota.Window(session, 300),
        weekly=quota.Window(weekly, 10080),
    )


def test_threshold_clamping_and_preparation() -> None:
    assert clamp_threshold(99) == 95.0, "must clamp, not reject, a legacy value"
    assert clamp_threshold(200) == 95.0
    assert clamp_threshold(50) == 50.0
    assert preparation_percent(95) == 92.0
    assert preparation_percent(50) == 47.0
    print("  ok test_threshold_clamping_and_preparation")


def test_phase_evaluation() -> None:
    ev = lambda u: supervisor.evaluate(u, rotate_at_percent=95.0)
    assert ev(_usage(session=10, weekly=10)) is supervisor.Phase.NORMAL
    assert ev(_usage(session=92, weekly=10)) is supervisor.Phase.PREPARE
    assert ev(_usage(session=95, weekly=10)) is supervisor.Phase.SWITCH
    assert ev(_usage(session=99, weekly=10)) is supervisor.Phase.SWITCH
    # The weekly window is a backstop: it trips the threshold on its own.
    assert ev(_usage(session=1, weekly=96)) is supervisor.Phase.SWITCH
    assert ev(_usage(session=1, weekly=93)) is supervisor.Phase.PREPARE
    print("  ok test_phase_evaluation")


def test_unknown_usage_never_rotates() -> None:
    ev = lambda u: supervisor.evaluate(u, rotate_at_percent=95.0)
    assert ev(quota.Usage(state="unknown")) is supervisor.Phase.NORMAL
    assert ev(_usage(state="unknown", session=99)) is supervisor.Phase.NORMAL
    assert ev(_usage()) is supervisor.Phase.NORMAL, "no numbers means no decision"
    print("  ok test_unknown_usage_never_rotates")


def test_rotation_lock_is_exclusive() -> None:
    with supervisor.rotation_lock(timeout=1.0):
        code = (
            "import supervisor,sys\n"
            "try:\n"
            "    with supervisor.rotation_lock(timeout=0.5): sys.exit(9)\n"
            "except supervisor.SupervisorError: sys.exit(3)\n"
        )
        result = subprocess.run(
            ["python3", "-c", code], cwd=str(Path(__file__).parent),
            capture_output=True, timeout=60,
        )
        assert result.returncode == 3, (
            f"a second rotation must be refused, got {result.returncode}"
        )
    print("  ok test_rotation_lock_is_exclusive")


def _workspace(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@e.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "T"], check=True)
    (root / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "i"], check=True)
    return root


def test_request_lifecycle() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws = _workspace(Path(tmp) / "ws")
        assert supervisor.request_generation(ws) == 0
        supervisor.write_request(ws, supervisor.Phase.PREPARE, generation=4)
        assert supervisor.request_generation(ws) == 4
        data = json.loads((ws / ".omni-route" / "rotation-request.json").read_text())
        assert data["phase"] == "prepare"
        supervisor.clear_request(ws)
        assert supervisor.request_generation(ws) == 0
    print("  ok test_request_lifecycle")


class _FakePool:
    """Stands in for the account pool so rotation can be driven deterministically."""

    def __init__(self, nxt):
        self.config = PoolConfig(accounts=(), rotate_at_percent=95.0)
        self.enabled = True
        self._next = nxt
        self.rotated_with: dict | None = None

    def rotate_session(self, session_id, **kwargs):
        self.rotated_with = kwargs
        return self._next

    def snapshot(self):
        return {}


def _supervisor_for(ws: Path, nxt, **overrides):
    sup = supervisor.Supervisor(ws, pool=_FakePool(nxt), sleep=lambda _s: None)
    for name, value in overrides.items():
        setattr(sup, name, value)
    return sup


def test_rotation_stops_when_no_account_is_available() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws = _workspace(Path(tmp) / "ws")
        sup = _supervisor_for(ws, None, wait_for_handoff=lambda **_k: True,
                              stop_provider=lambda *_a: None)
        result = sup.rotate()
        assert result.outcome is supervisor.Outcome.NEEDS_USER_ACTION
        assert "no account is available" in result.detail
        assert supervisor.request_generation(ws) == 0, "must not leave a request armed"
    print("  ok test_rotation_stops_when_no_account_is_available")


def test_rotation_stops_on_identity_mismatch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws = _workspace(Path(tmp) / "ws")
        profile = AccountProfile("claude-9", provider="claude", config_dir=Path(tmp) / "cfg")
        started: list[str] = []
        sup = _supervisor_for(
            ws, profile,
            wait_for_handoff=lambda **_k: True,
            stop_provider=lambda *_a: None,
            prepare_account=lambda _p: None,
            verify_account=lambda _p: (False, "wrong account: expected A, found B"),
            start_provider=lambda p: started.append(p.name),
        )
        result = sup.rotate()
        assert result.outcome is supervisor.Outcome.NEEDS_USER_ACTION
        assert "identity check failed" in result.detail
        assert started == [], "must never launch after a failed identity check"
    print("  ok test_rotation_stops_on_identity_mismatch")


def test_rotation_succeeds_and_reports_unclean_wrap_up() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws = _workspace(Path(tmp) / "ws")
        profile = AccountProfile("claude-9", provider="claude", config_dir=Path(tmp) / "cfg")
        sup = _supervisor_for(
            ws, profile,
            wait_for_handoff=lambda **_k: False,      # turn never ended
            stop_provider=lambda *_a: None,
            prepare_account=lambda _p: None,
            verify_account=lambda _p: (True, "uuid"),
            start_provider=lambda _p: None,
            wait_for_resume=lambda **_k: True,
        )
        result = sup.rotate()
        assert result.outcome is supervisor.Outcome.OK
        assert result.unclean is True, "a forced restart must be recorded as unclean"
        assert result.to_account == "claude-9"
    print("  ok test_rotation_succeeds_and_reports_unclean_wrap_up")


def test_rotation_falls_back_to_headless_and_reports_degraded() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws = _workspace(Path(tmp) / "ws")
        profile = AccountProfile("claude-9", provider="claude", config_dir=Path(tmp) / "cfg")
        launches: list[str] = []
        sup = _supervisor_for(
            ws, profile,
            wait_for_handoff=lambda **_k: True,
            stop_provider=lambda *_a: None,
            prepare_account=lambda _p: None,
            verify_account=lambda _p: (True, "uuid"),
            start_provider=lambda p: launches.append(p.name),
            wait_for_resume=lambda **_k: False,       # app never starts a turn
            headless_continuation=lambda _p: True,
        )
        result = sup.rotate()
        assert result.outcome is supervisor.Outcome.DEGRADED, (
            "a headless rescue must never be reported as a clean rotation"
        )
        assert len(launches) == supervisor.RESTART_ATTEMPTS + 1, (
            "restarts are retried before the fallback, then the app is handed back"
        )
    print("  ok test_rotation_falls_back_to_headless_and_reports_degraded")


def test_rotation_needs_user_action_when_everything_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws = _workspace(Path(tmp) / "ws")
        profile = AccountProfile("claude-9", provider="claude", config_dir=Path(tmp) / "cfg")
        sup = _supervisor_for(
            ws, profile,
            wait_for_handoff=lambda **_k: True,
            stop_provider=lambda *_a: None,
            prepare_account=lambda _p: None,
            verify_account=lambda _p: (True, "uuid"),
            start_provider=lambda _p: None,
            wait_for_resume=lambda **_k: False,
            headless_continuation=lambda _p: False,
        )
        result = sup.rotate()
        assert result.outcome is supervisor.Outcome.NEEDS_USER_ACTION
        assert result.unclean is True
    print("  ok test_rotation_needs_user_action_when_everything_fails")


def test_wait_for_resume_observes_the_marker() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws = _workspace(Path(tmp) / "ws")
        sup = supervisor.Supervisor(ws, pool=_FakePool(None), sleep=lambda _s: None)
        handoff.write(ws, handoff.Handoff(goal="g"))
        assert sup.wait_for_resume(timeout=0.3) is False, "armed handoff means not resumed"
        handoff.clear(ws)
        assert sup.wait_for_resume(timeout=0.3) is True
    print("  ok test_wait_for_resume_observes_the_marker")


def main() -> int:
    tests = [
        test_threshold_clamping_and_preparation,
        test_phase_evaluation,
        test_unknown_usage_never_rotates,
        test_rotation_lock_is_exclusive,
        test_request_lifecycle,
        test_rotation_stops_when_no_account_is_available,
        test_rotation_stops_on_identity_mismatch,
        test_rotation_succeeds_and_reports_unclean_wrap_up,
        test_rotation_falls_back_to_headless_and_reports_degraded,
        test_rotation_needs_user_action_when_everything_fails,
        test_wait_for_resume_observes_the_marker,
        test_confirm_requires_activity_after_launch,
        test_preflight_checks_the_credential_not_the_profile_history,
    ]
    print(f"running {len(tests)} supervisor tests")
    for test in tests:
        test()
    print("all passed")
    return 0

def test_confirm_requires_activity_after_launch() -> None:
    """Presence of account state proves nothing; it must be touched after launch.

    A desktop profile keeps a directory for every account it has ever run as, so
    reading that directory as identity would accept a stale account.
    """
    import desktop
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        cfg = base / "cfg"
        cfg.mkdir()
        (cfg / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"accountUuid": "uuid-A"}}), encoding="utf-8"
        )
        udd = base / "udd"
        stale = udd / "claude-code-sessions" / "uuid-A"
        stale.mkdir(parents=True)

        launch = time.time() + 5  # nothing has happened since this moment
        ok, detail = desktop.confirm_claude_account(udd, cfg, since=launch)
        assert ok is False and "not touched after launch" in detail

        # Touching it after the launch is the proof.
        stale.touch()
        ok, detail = desktop.confirm_claude_account(udd, cfg, since=time.time() - 5)
        assert ok is True and detail == "uuid-A"

        # A different account never satisfies the check.
        (cfg / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"accountUuid": "uuid-B"}}), encoding="utf-8"
        )
        ok, detail = desktop.confirm_claude_account(udd, cfg, since=time.time() - 5)
        assert ok is False and "no session state for uuid-B" in detail
    print("  ok test_confirm_requires_activity_after_launch")


def test_preflight_checks_the_credential_not_the_profile_history() -> None:
    import desktop
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        cfg = base / "cfg"; cfg.mkdir()
        udd = base / "udd"; udd.mkdir()
        ok, detail = desktop.verify_claude_account(udd, cfg)
        assert ok is False, "a profile with no credential must not pass pre-flight"

        (cfg / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"accountUuid": "uuid-A"}}), encoding="utf-8"
        )
        ok, detail = desktop.verify_claude_account(udd, cfg)
        assert ok is True and detail == "uuid-A"

        ok, detail = desktop.verify_claude_account(base / "missing", cfg)
        assert ok is False and "signin" in detail
    print("  ok test_preflight_checks_the_credential_not_the_profile_history")


if __name__ == "__main__":
    raise SystemExit(main())
