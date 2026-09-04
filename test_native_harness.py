#!/usr/bin/env python3
"""Tests for the native-harness handoff and scheduled-task modules."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import handoff
import native_scheduler as ns


def _git_workspace(root: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "T"], check=True)
    (root / "file.txt").write_text("hi", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
    return root


def test_handoff_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws = _git_workspace(Path(tmp) / "ws")
        original = handoff.Handoff(
            goal="Ship rotation",
            progress="Gates 1-5 done",
            decisions="Desktop only; reseed via handoff",
            files="native_scheduler.py",
            status="tests pass",
            blockers="none",
            next_action="Run gate 6",
            from_account="claude-1",
            to_account="claude-3",
        )
        path = handoff.write(ws, original)
        assert path.exists()
        assert handoff.is_pending(ws), "writing must arm the pending marker"

        restored = handoff.read_latest(ws)
        assert restored is not None
        for name in ("goal", "progress", "decisions", "files", "status", "next_action"):
            assert getattr(restored, name) == getattr(original, name), name
        # git context is filled in automatically
        assert restored.branch, "branch must be recorded"
        assert restored.commit, "commit must be recorded"
        assert Path(restored.worktree_path).resolve() == ws.resolve()
        assert restored.to_account == "claude-3"

        consumed = handoff.consume(ws)
        assert consumed is not None and consumed.goal == "Ship rotation"
        assert not handoff.is_pending(ws), "consume must clear the marker"
    print("  ok test_handoff_roundtrip")


def test_handoff_empty_fields_do_not_leak_placeholders() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws = _git_workspace(Path(tmp) / "ws")
        handoff.write(ws, handoff.Handoff(goal="only goal"))
        restored = handoff.read_latest(ws)
        assert restored is not None
        assert restored.goal == "only goal"
        assert restored.blockers == "", "placeholder text must not round-trip as content"
    print("  ok test_handoff_empty_fields_do_not_leak_placeholders")


def _fake_user_data_dir(root: Path, account_uuid: str = "acct-uuid") -> Path:
    store = root / ns.SESSIONS_DIRNAME / account_uuid / "session-uuid" / ns.STORE_NAME
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"scheduledTasks": [], "recordedSkips": {}}), encoding="utf-8")
    return root


def test_install_and_remove() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        udd = _fake_user_data_dir(base / "udd")
        cfg = base / "cfg"
        ws = _git_workspace(base / "ws")

        result = ns.install(udd, ws, claude_config_dir=cfg)
        assert result["action"] == "created"
        assert Path(result["backup"]).exists(), "a backup must be kept"

        record = result["record"]
        assert record["useWorktree"] is False, "worktree must be off for continuations"
        assert record["disableJitter"] is True, "jitter is latency on every rotation"
        assert record["cronExpression"] == ns.ROTATION_CRON
        assert record["cwd"] == str(ws.resolve())

        skill = Path(result["skill"])
        assert skill.exists()
        text = skill.read_text(encoding="utf-8")
        assert "handoff-pending" in text
        assert "no pending handoff" in text, "prompt must self-gate"

        # Re-installing updates in place and preserves createdAt.
        again = ns.install(udd, ws, claude_config_dir=cfg)
        assert again["action"] == "updated"
        assert again["record"]["createdAt"] == record["createdAt"]
        data = json.loads(Path(result["store"]).read_text(encoding="utf-8"))
        ids = [t["id"] for t in data["scheduledTasks"]]
        assert ids.count(ns.TASK_ID) == 1, "must not duplicate the task"

        assert ns.remove(udd) is True
        data = json.loads(Path(result["store"]).read_text(encoding="utf-8"))
        assert all(t["id"] != ns.TASK_ID for t in data["scheduledTasks"])
        assert ns.remove(udd) is False, "removing twice is a no-op"
    print("  ok test_install_and_remove")


def test_store_validation_refuses_bad_shapes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        udd = _fake_user_data_dir(base / "udd")
        store = ns.find_store(udd)

        for bad in ("[]", '{"scheduledTasks": {}}', '{"scheduledTasks": [1]}', "not json"):
            store.path.write_text(bad, encoding="utf-8")
            try:
                ns.load_store(store)
            except ns.SchedulerError:
                continue
            raise AssertionError(f"expected refusal for {bad!r}")

        # Missing store must raise, not invent a directory.
        try:
            ns.find_store(base / "nonexistent")
        except ns.SchedulerError:
            pass
        else:
            raise AssertionError("expected SchedulerError for a missing store")
    print("  ok test_store_validation_refuses_bad_shapes")


def test_dry_run_touches_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        udd = _fake_user_data_dir(base / "udd")
        cfg = base / "cfg"
        ws = _git_workspace(base / "ws")
        before = ns.find_store(udd).path.read_text(encoding="utf-8")
        result = ns.install(udd, ws, claude_config_dir=cfg, dry_run=True)
        assert "would be" in result["action"]
        assert ns.find_store(udd).path.read_text(encoding="utf-8") == before
        assert not ns.skill_path(claude_config_dir=cfg).exists()
    print("  ok test_dry_run_touches_nothing")


def test_parses_the_real_store_if_present() -> None:
    real = Path.home() / "Library" / "Application Support" / "Claude"
    try:
        store = ns.find_store(real)
    except ns.SchedulerError as exc:
        print(f"  skip test_parses_the_real_store_if_present ({exc})")
        return
    data = ns.load_store(store)  # read-only
    ids = [t.get("id") for t in data["scheduledTasks"]]
    print(f"  ok test_parses_the_real_store_if_present (tasks: {ids})")


def main() -> int:
    tests = [
        test_handoff_roundtrip,
        test_handoff_empty_fields_do_not_leak_placeholders,
        test_install_and_remove,
        test_store_validation_refuses_bad_shapes,
        test_dry_run_touches_nothing,
        test_parses_the_real_store_if_present,
        test_stop_hook_is_silent_without_a_request,
        test_stop_hook_fires_once_per_generation,
        test_stop_hook_prepare_phase_differs,
        test_stop_hook_fails_open_on_garbage,
    ]
    print(f"running {len(tests)} tests")
    for test in tests:
        test()
    print("all passed")
    return 0



def _run_hook(workspace: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "rotation_stop_hook.py")],
        input="{}", capture_output=True, text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(workspace)},
        timeout=30,
    )
    return proc.returncode, proc.stderr


def test_stop_hook_is_silent_without_a_request() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws = _git_workspace(Path(tmp) / "ws")
        code, err = _run_hook(ws)
        assert code == 0, "must not block a session that is not rotating"
        assert err.strip() == ""
    print("  ok test_stop_hook_is_silent_without_a_request")


def test_stop_hook_fires_once_per_generation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws = _git_workspace(Path(tmp) / "ws")
        req = ws / ".omni-route" / "rotation-request.json"
        req.parent.mkdir(parents=True, exist_ok=True)
        req.write_text(json.dumps({"phase": "switch", "generation": 7}), encoding="utf-8")

        code, err = _run_hook(ws)
        assert code == 2, "armed rotation must block the stop"
        assert "handoff" in err and "Commit your work" in err

        code, err = _run_hook(ws)
        assert code == 0, "second fire would loop forever"
        assert err.strip() == ""

        # A new generation may fire again.
        req.write_text(json.dumps({"phase": "switch", "generation": 8}), encoding="utf-8")
        code, _ = _run_hook(ws)
        assert code == 2
    print("  ok test_stop_hook_fires_once_per_generation")


def test_stop_hook_prepare_phase_differs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws = _git_workspace(Path(tmp) / "ws")
        req = ws / ".omni-route" / "rotation-request.json"
        req.parent.mkdir(parents=True, exist_ok=True)
        req.write_text(json.dumps({"phase": "prepare", "generation": 1}), encoding="utf-8")
        code, err = _run_hook(ws)
        assert code == 2
        assert "wind down" in err and "Commit your work" not in err
    print("  ok test_stop_hook_prepare_phase_differs")


def test_stop_hook_fails_open_on_garbage() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws = _git_workspace(Path(tmp) / "ws")
        req = ws / ".omni-route" / "rotation-request.json"
        req.parent.mkdir(parents=True, exist_ok=True)
        for bad in ("not json", "[]", '{"phase": "nonsense"}'):
            req.write_text(bad, encoding="utf-8")
            code, _ = _run_hook(ws)
            assert code == 0, f"must fail open for {bad!r}"
    print("  ok test_stop_hook_fails_open_on_garbage")

if __name__ == "__main__":
    raise SystemExit(main())
