# tests/macos/test_update_merge.py
"""
Merge-based git updates (macOS Clawdaddy installs carry local commits on top
of upstream/main, so `git pull --ff-only` can never succeed there).

Every repo here is a throwaway temp dir -- this suite never touches
~/lobster's own git state, and never sets LOBSTER_UPDATE_REMOTE=upstream
against a real clone. It also never touches a real launchd service: every
test runs with a fake `launchctl` shadowing the real one on PATH, and with
LOBSTER_WORKSPACE/LOBSTER_MESSAGES pointed at tmp_path, so a merge's outcome
JSON and inbox note never land in ~/lobster-workspace or ~/messages either.

Fixture shape: a bare "upstream" repo (the far end of an `origin` remote) plus
a "local" clone that gets one local-only commit, mirroring a Mac install that
is upstream/main plus Mac-specific commits. A "seed" working clone is used
only to author and push new commits into the bare repo, standing in for
upstream advancing while the Mac install wasn't looking.
"""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from src.mcp.update_manager import UpdateManager

SELFCHECK_SCRIPT = Path(__file__).resolve().parents[2] / "macos" / "selfcheck.sh"

# A fake `launchctl`: logs every invocation (one line of argv per call) to
# FAKE_LAUNCHCTL_LOG, and answers `print`/`kickstart` for com.lobster.router
# and com.lobster.claude entirely from env vars a test sets, so no test ever
# depends on -- or can disturb -- this machine's real LaunchAgent state.
#
#   FAKE_ROUTER_LOADED=1   -- `print .../com.lobster.router` succeeds
#   FAKE_ROUTER_RUNNING=1  -- and reports "state = running"
#   FAKE_CLAUDE_LOADED=1   -- `print .../com.lobster.claude` succeeds
#
# When a kickstart/print call names com.lobster.claude, the logged line is
# tagged [JSON_PRESENT] or [JSON_MISSING] depending on whether
# $LOBSTER_WORKSPACE/logs/last-update.json exists at that exact moment --
# the ordering check the fix round asked for, recorded by the fake tool
# itself rather than inferred from code reading.
_FAKE_LAUNCHCTL = """#!/usr/bin/env python3
import os, sys
from pathlib import Path

args = sys.argv[1:]
line = " ".join(args)

if "com.lobster.claude" in line:
    marker = "JSON_MISSING"
    workspace = os.environ.get("LOBSTER_WORKSPACE")
    log_dir = os.environ.get("LOBSTER_LOG_DIR") or (workspace and str(Path(workspace) / "logs"))
    if log_dir and (Path(log_dir) / "last-update.json").exists():
        marker = "JSON_PRESENT"
    line = f"{line} [{marker}]"

log_path = os.environ.get("FAKE_LAUNCHCTL_LOG")
if log_path:
    with open(log_path, "a") as f:
        f.write(line + "\\n")

if args and args[0] == "print" and len(args) > 1:
    label = args[1].rsplit("/", 1)[-1]
    if label == "com.lobster.router":
        if os.environ.get("FAKE_ROUTER_LOADED") != "1":
            sys.exit(1)
        if os.environ.get("FAKE_ROUTER_RUNNING") == "1":
            print("state = running")
        else:
            print("state = starting")
        sys.exit(0)
    if label == "com.lobster.claude":
        sys.exit(0 if os.environ.get("FAKE_CLAUDE_LOADED") == "1" else 1)
    sys.exit(1)
sys.exit(0)
"""


def _run(*args, cwd):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


def _commit(repo: Path, filename: str, content: str, message: str):
    target = repo / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _run("add", filename, cwd=repo)
    _run("commit", "--quiet", "-m", message, cwd=repo)


def _push_new_commit(seed: Path, upstream: Path, filename: str, content: str, message: str):
    """Author a commit in the scratch `seed` clone and push it into the bare
    upstream repo, simulating upstream/main advancing independently."""
    _commit(seed, filename, content, message)
    _run("push", str(upstream), "main", cwd=seed)


@pytest.fixture
def repos(tmp_path):
    upstream = tmp_path / "upstream.git"
    upstream.mkdir()
    _run("init", "--quiet", "--bare", "-b", "main", str(upstream), cwd=tmp_path)

    seed = tmp_path / "seed"
    seed.mkdir()
    _run("init", "--quiet", "-b", "main", str(seed), cwd=tmp_path)
    _run("config", "user.email", "test@example.com", cwd=seed)
    _run("config", "user.name", "Test", cwd=seed)
    # shared.txt exists from the base commit so later commits on both sides
    # can edit the SAME line of it and actually conflict. docs/SLACK-SETUP.md
    # is also seeded here and never touched again by either side, mirroring
    # the real repo: a long-standing file this particular update doesn't
    # change on either end -- the case the phantom-tolerance test exercises.
    (seed / "shared.txt").write_text("original\n")
    (seed / "docs").mkdir()
    (seed / "docs" / "SLACK-SETUP.md").write_text("as committed\n")
    _run("add", "shared.txt", "docs/SLACK-SETUP.md", cwd=seed)
    _run("commit", "--quiet", "-m", "base commit", cwd=seed)
    _run("push", str(upstream), "main", cwd=seed)

    local = tmp_path / "local"
    _run("clone", "--quiet", str(upstream), str(local), cwd=tmp_path)
    _run("config", "user.email", "test@example.com", cwd=local)
    _run("config", "user.name", "Test", cwd=local)

    return upstream, seed, local


@pytest.fixture(autouse=True)
def _env_sandbox(tmp_path, monkeypatch):
    # Default: the merge path is only enabled by LOBSTER_UPDATE_REMOTE (M1),
    # so point it at the fixture's `origin`, the Mac shape. Tests of the
    # unset (Linux) behavior delenv it themselves.
    monkeypatch.setenv("LOBSTER_UPDATE_REMOTE", "origin")
    monkeypatch.delenv("LOBSTER_SELFCHECK_CMD", raising=False)
    monkeypatch.delenv("LOBSTER_LOG_DIR", raising=False)
    # A successful merge writes an outcome JSON and drops an inbox note --
    # point both at tmp_path so no test can ever touch the operator's real
    # ~/lobster-workspace or ~/messages.
    monkeypatch.setenv("LOBSTER_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("LOBSTER_MESSAGES", str(tmp_path / "messages"))


@pytest.fixture(autouse=True)
def fake_launchctl(tmp_path, monkeypatch):
    """Shadow the real `launchctl` on PATH for every test in this file, so a
    merge's post-success (claude kickstart) or post-rollback (router
    recovery kickstart) behavior never reaches this machine's real
    LaunchAgents. Returns the log file path; tests that need to inspect
    calls request this fixture by name."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    script = bin_dir / "launchctl"
    script.write_text(_FAKE_LAUNCHCTL)
    script.chmod(0o755)

    log_path = tmp_path / "launchctl.log"
    monkeypatch.setenv("FAKE_LAUNCHCTL_LOG", str(log_path))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("FAKE_ROUTER_LOADED", raising=False)
    monkeypatch.delenv("FAKE_ROUTER_RUNNING", raising=False)
    monkeypatch.delenv("FAKE_CLAUDE_LOADED", raising=False)
    return log_path


def _launchctl_log(log_path: Path) -> str:
    return log_path.read_text() if log_path.exists() else ""


# ---------------------------------------------------------------------------
# The four required cases
# ---------------------------------------------------------------------------


def test_clean_merge_keeps_both_commits(repos, monkeypatch, fake_launchctl):
    upstream, seed, local = repos
    monkeypatch.setenv("LOBSTER_SELFCHECK_CMD", "true")

    _commit(local, "local.txt", "local change\n", "local commit")
    _push_new_commit(seed, upstream, "remote.txt", "remote change\n", "remote commit")

    mgr = UpdateManager(repo_path=local)
    result = mgr._execute_git_update()

    assert result["success"] is True, result
    assert (local / "local.txt").read_text() == "local change\n"
    assert (local / "remote.txt").read_text() == "remote change\n"
    log = _run("log", "--oneline", cwd=local)
    assert "local commit" in log
    assert "remote commit" in log
    # com.lobster.claude was never loaded (fixture default) -> checked, but
    # never kickstarted.
    claude_log = _launchctl_log(fake_launchctl)
    assert "print" in claude_log and "com.lobster.claude" in claude_log
    assert "kickstart" not in claude_log


def test_conflict_leaves_head_and_tree_unchanged(repos, monkeypatch):
    upstream, seed, local = repos
    monkeypatch.setenv("LOBSTER_SELFCHECK_CMD", "true")

    _commit(local, "shared.txt", "local change\n", "local commit")
    head_before = _run("rev-parse", "HEAD", cwd=local).strip()

    _push_new_commit(seed, upstream, "shared.txt", "upstream change\n", "remote commit")

    mgr = UpdateManager(repo_path=local)
    result = mgr._execute_git_update()

    assert result == {"success": False, "error": "conflict", "files": ["shared.txt"]}
    assert _run("rev-parse", "HEAD", cwd=local).strip() == head_before
    assert not (local / ".git" / "MERGE_HEAD").exists()
    assert _run("status", "--porcelain", cwd=local).strip() == ""


def test_failed_selfcheck_rolls_back(repos, monkeypatch):
    upstream, seed, local = repos
    monkeypatch.setenv("LOBSTER_SELFCHECK_CMD", "false")

    _commit(local, "local.txt", "local change\n", "local commit")
    head_before = _run("rev-parse", "HEAD", cwd=local).strip()

    _push_new_commit(seed, upstream, "remote.txt", "remote change\n", "remote commit")

    mgr = UpdateManager(repo_path=local)
    result = mgr._execute_git_update()

    assert result == {"success": False, "error": "selfcheck failed, rolled back"}
    assert _run("rev-parse", "HEAD", cwd=local).strip() == head_before
    assert not (local / "remote.txt").exists()
    assert _run("status", "--porcelain", cwd=local).strip() == ""


def test_unset_remote_fast_forwardable_uses_original_path(repos, monkeypatch):
    upstream, seed, local = repos
    monkeypatch.delenv("LOBSTER_UPDATE_REMOTE", raising=False)

    _push_new_commit(seed, upstream, "remote.txt", "remote change\n", "remote commit")
    head_before = _run("rev-parse", "HEAD", cwd=local).strip()

    mgr = UpdateManager(repo_path=local)
    result = mgr._execute_git_update()

    assert result["success"] is True, result
    assert result["previous_sha"] == head_before
    assert (local / "remote.txt").read_text() == "remote change\n"
    # The original path: a plain fast-forward, carrying its own rollback command.
    assert "rollback_command" in result
    assert result["current_sha"] == _run("rev-parse", "HEAD", cwd=local).strip()


# ---------------------------------------------------------------------------
# Dirty tree / case-collision tolerance
# ---------------------------------------------------------------------------


def test_dirty_tree_refuses_before_merging(repos, monkeypatch):
    upstream, seed, local = repos
    monkeypatch.setenv("LOBSTER_SELFCHECK_CMD", "true")

    _commit(local, "local.txt", "local change\n", "local commit")
    _push_new_commit(seed, upstream, "remote.txt", "remote change\n", "remote commit")

    # An ordinary uncommitted change to a tracked file -- not the macOS
    # case-collision phantom -- must block the merge outright.
    (local / "shared.txt").write_text("uncommitted edit\n")

    mgr = UpdateManager(repo_path=local)
    result = mgr._execute_git_update()

    assert result == {"success": False, "error": "dirty tree"}
    assert not (local / "remote.txt").exists()


def test_slack_setup_case_collision_phantom_is_tolerated(repos, monkeypatch):
    """macOS's case-insensitive filesystem makes docs/SLACK-SETUP.md and
    docs/slack-setup.md collide on disk, so `git status` can report the
    tracked path as modified with no real edit behind it. That specific,
    narrowly-named path must not block a merge the way any other dirty
    tracked file would."""
    upstream, seed, local = repos
    monkeypatch.setenv("LOBSTER_SELFCHECK_CMD", "true")

    _commit(local, "local.txt", "local change\n", "local commit")
    _push_new_commit(seed, upstream, "remote.txt", "remote change\n", "remote commit")

    # Simulate the phantom: the working tree disagrees with the index at
    # exactly that path, with no `git add` ever run.
    (local / "docs" / "SLACK-SETUP.md").write_text("phantom content\n")
    status_line = _run("status", "--porcelain", "--untracked-files=no", cwd=local)
    assert "docs/SLACK-SETUP.md" in status_line

    mgr = UpdateManager(repo_path=local)
    result = mgr._execute_git_update()

    assert result["success"] is True, result
    assert (local / "remote.txt").read_text() == "remote change\n"


# ---------------------------------------------------------------------------
# CRITICAL fix: LOBSTER_UPDATE_REMOTE validation
# ---------------------------------------------------------------------------


def test_execute_update_refuses_remote_with_leading_dash(repos, monkeypatch):
    upstream, seed, local = repos
    head_before = _run("rev-parse", "HEAD", cwd=local).strip()
    monkeypatch.setenv("LOBSTER_UPDATE_REMOTE", "--upload-pack=id")

    mgr = UpdateManager(repo_path=local)
    result = mgr._execute_git_update()

    assert result == {"success": False, "error": "invalid remote"}
    # Nothing was ever fetched or merged.
    assert _run("rev-parse", "HEAD", cwd=local).strip() == head_before


def test_execute_update_refuses_remote_with_spaces(repos, monkeypatch):
    upstream, seed, local = repos
    monkeypatch.setenv("LOBSTER_UPDATE_REMOTE", "origin main")

    mgr = UpdateManager(repo_path=local)
    result = mgr._execute_git_update()

    assert result == {"success": False, "error": "invalid remote"}


def test_execute_update_refuses_unknown_remote_name(repos, monkeypatch):
    upstream, seed, local = repos
    monkeypatch.setenv("LOBSTER_UPDATE_REMOTE", "nonexistent")

    mgr = UpdateManager(repo_path=local)
    result = mgr._execute_git_update()

    assert result == {"success": False, "error": "invalid remote"}


def test_check_for_updates_also_refuses_invalid_remote(repos, monkeypatch):
    upstream, seed, local = repos
    monkeypatch.setenv("LOBSTER_UPDATE_REMOTE", "--evil")

    mgr = UpdateManager(repo_path=local)
    result = mgr._check_git_updates()

    assert result == {"updates_available": False, "error": "invalid remote"}


# ---------------------------------------------------------------------------
# MINOR fix: an untracked-file-overwrite merge refusal is not a "conflict"
# ---------------------------------------------------------------------------


def test_untracked_file_overwrite_is_merge_refused_not_conflict(repos, monkeypatch):
    upstream, seed, local = repos
    monkeypatch.setenv("LOBSTER_SELFCHECK_CMD", "true")

    _commit(local, "local.txt", "local change\n", "local commit")
    # Never `git add`ed -- an ordinary untracked file that happens to share a
    # path with something the incoming merge would create.
    (local / "collide.txt").write_text("untracked local content\n")

    _push_new_commit(seed, upstream, "collide.txt", "upstream content\n", "remote commit")

    mgr = UpdateManager(repo_path=local)
    result = mgr._execute_git_update()

    assert result["success"] is False
    assert result["error"] == "merge refused"
    assert "collide.txt" in result["message"]
    # Nothing about the conflict-only shape (no "files" list of ambiguous
    # empty conflicts), and the untracked file itself was left alone.
    assert "files" not in result
    assert (local / "collide.txt").read_text() == "untracked local content\n"


# ---------------------------------------------------------------------------
# IMPORTANT fix: ordering around the router/claude restart
# ---------------------------------------------------------------------------


def test_router_timeout_rolls_back_and_records_and_restarts_router(repos, monkeypatch, fake_launchctl):
    upstream, seed, local = repos
    # Simulates selfcheck.sh's own contract: exit 2 means it kickstarted the
    # router onto the merged code and the router never came up, and
    # selfcheck never touched com.lobster.claude.
    monkeypatch.setenv("LOBSTER_SELFCHECK_CMD", "exit 2")

    _commit(local, "local.txt", "local change\n", "local commit")
    head_before = _run("rev-parse", "HEAD", cwd=local).strip()
    _push_new_commit(seed, upstream, "remote.txt", "remote change\n", "remote commit")

    mgr = UpdateManager(repo_path=local)
    result = mgr._execute_git_update()

    assert result == {
        "success": False,
        "error": "selfcheck failed, rolled back",
        "reason": "router failed to start",
    }
    assert _run("rev-parse", "HEAD", cwd=local).strip() == head_before
    assert not (local / "remote.txt").exists()
    assert _run("status", "--porcelain", cwd=local).strip() == ""

    log = _launchctl_log(fake_launchctl)
    assert "kickstart -k gui/" in log and "com.lobster.router" in log
    assert "com.lobster.claude" not in log  # never touched

    workspace = Path(os.environ["LOBSTER_WORKSPACE"])
    outcome = json.loads((workspace / "logs" / "last-update.json").read_text())
    assert outcome["status"] == "rolled back: router failed to start"
    assert outcome["before"] == head_before

    inbox_dir = Path(os.environ["LOBSTER_MESSAGES"]) / "inbox"
    notes = list(inbox_dir.glob("*.json"))
    assert len(notes) == 1
    note = json.loads(notes[0].read_text())
    assert note["source"] == "system"  # "internal" is not a valid inbox source
    assert "rolled back" in note["text"].lower()


def test_success_writes_outcome_and_inbox_before_claude_kickstart(repos, monkeypatch, fake_launchctl):
    upstream, seed, local = repos
    monkeypatch.setenv("LOBSTER_SELFCHECK_CMD", "true")
    monkeypatch.setenv("FAKE_CLAUDE_LOADED", "1")

    _commit(local, "local.txt", "local change\n", "local commit")
    previous_sha = _run("rev-parse", "HEAD", cwd=local).strip()
    _push_new_commit(seed, upstream, "remote.txt", "remote change\n", "remote commit")

    mgr = UpdateManager(repo_path=local)
    result = mgr._execute_git_update()

    assert result["success"] is True, result
    assert result["rollback_command"] == f"git reset --hard {previous_sha}"

    log_lines = _launchctl_log(fake_launchctl).strip().splitlines()
    claude_lines = [l for l in log_lines if "com.lobster.claude" in l and "kickstart" in l]
    assert claude_lines, log_lines
    # The fake launchctl itself checked, at the moment it was invoked,
    # whether last-update.json already existed -- proving the write
    # happened first without trusting code-reading alone.
    assert "[JSON_PRESENT]" in claude_lines[0], claude_lines

    workspace = Path(os.environ["LOBSTER_WORKSPACE"])
    outcome = json.loads((workspace / "logs" / "last-update.json").read_text())
    assert outcome["status"] == "merged"
    assert outcome["before"] == previous_sha
    assert outcome["merged_commits"]

    inbox_dir = Path(os.environ["LOBSTER_MESSAGES"]) / "inbox"
    notes = list(inbox_dir.glob("*.json"))
    assert len(notes) == 1
    note = json.loads(notes[0].read_text())
    assert note["source"] == "system"
    assert "merged" in note["text"].lower()


def test_success_skips_claude_kickstart_when_not_loaded(repos, monkeypatch, fake_launchctl):
    upstream, seed, local = repos
    monkeypatch.setenv("LOBSTER_SELFCHECK_CMD", "true")
    # FAKE_CLAUDE_LOADED left unset by the fixture -- "not loaded".

    _commit(local, "local.txt", "local change\n", "local commit")
    _push_new_commit(seed, upstream, "remote.txt", "remote change\n", "remote commit")

    mgr = UpdateManager(repo_path=local)
    result = mgr._execute_git_update()

    assert result["success"] is True, result
    log = _launchctl_log(fake_launchctl)
    assert "com.lobster.claude" in log  # loaded-check happened
    assert "kickstart" not in log       # but nothing was ever kickstarted

    workspace = Path(os.environ["LOBSTER_WORKSPACE"])
    assert (workspace / "logs" / "last-update.json").exists()


# ---------------------------------------------------------------------------
# selfcheck.sh's own contract, run directly (never touches git; launchctl is
# faked and the pytest phase is overridden, so this is safe to run against
# the real script even though it `cd`s into the real repo root).
# ---------------------------------------------------------------------------


def _run_selfcheck(extra_env: dict, timeout: int = 30) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(extra_env)
    return subprocess.run(["bash", str(SELFCHECK_SCRIPT)], capture_output=True, text=True, env=env, timeout=timeout)


def test_selfcheck_pytest_failure_exits_1_and_never_touches_launchctl(fake_launchctl):
    result = _run_selfcheck({"LOBSTER_SELFCHECK_PYTEST_CMD": "false"})
    assert result.returncode == 1
    assert _launchctl_log(fake_launchctl) == ""


def test_selfcheck_router_not_loaded_exits_0(fake_launchctl):
    result = _run_selfcheck({"LOBSTER_SELFCHECK_PYTEST_CMD": "true"})
    assert result.returncode == 0


def test_selfcheck_router_recovers_exits_0_and_never_touches_claude(fake_launchctl):
    result = _run_selfcheck({
        "LOBSTER_SELFCHECK_PYTEST_CMD": "true",
        "FAKE_ROUTER_LOADED": "1",
        "FAKE_ROUTER_RUNNING": "1",
        "LOBSTER_SELFCHECK_ROUTER_TIMEOUT": "3",
        "LOBSTER_SELFCHECK_POLL_INTERVAL": "1",
    })
    assert result.returncode == 0
    assert "com.lobster.claude" not in _launchctl_log(fake_launchctl)


def test_selfcheck_router_timeout_exits_2_and_never_touches_claude(fake_launchctl):
    result = _run_selfcheck({
        "LOBSTER_SELFCHECK_PYTEST_CMD": "true",
        "FAKE_ROUTER_LOADED": "1",
        # FAKE_ROUTER_RUNNING left unset -- it never reports "state = running".
        "LOBSTER_SELFCHECK_ROUTER_TIMEOUT": "2",
        "LOBSTER_SELFCHECK_POLL_INTERVAL": "1",
    })
    assert result.returncode == 2
    assert "com.lobster.claude" not in _launchctl_log(fake_launchctl)


# ---------------------------------------------------------------------------
# Final fix wave
# ---------------------------------------------------------------------------


def test_unset_remote_with_local_commits_never_merges(repos, monkeypatch):
    """M1: a Linux install (LOBSTER_UPDATE_REMOTE unset) that carries local
    commits keeps the original ff-only behavior -- it refuses, it never
    merges."""
    upstream, seed, local = repos
    monkeypatch.delenv("LOBSTER_UPDATE_REMOTE", raising=False)
    monkeypatch.setenv("LOBSTER_SELFCHECK_CMD", "true")

    _commit(local, "local.txt", "local change\n", "local commit")
    head_before = _run("rev-parse", "HEAD", cwd=local).strip()
    _push_new_commit(seed, upstream, "remote.txt", "remote change\n", "remote commit")

    result = UpdateManager(repo_path=local)._execute_git_update()

    assert result["success"] is False, result
    assert _run("rev-parse", "HEAD", cwd=local).strip() == head_before
    assert not (local / "remote.txt").exists()


def test_outcome_goes_to_log_dir_not_workspace(repos, monkeypatch, tmp_path):
    """I4: with LOBSTER_LOG_DIR set, last-update.json lands there and
    nothing is written into the workspace."""
    upstream, seed, local = repos
    monkeypatch.setenv("LOBSTER_SELFCHECK_CMD", "true")
    log_dir = tmp_path / "state" / "logs"
    monkeypatch.setenv("LOBSTER_LOG_DIR", str(log_dir))

    _commit(local, "local.txt", "local change\n", "local commit")
    _push_new_commit(seed, upstream, "remote.txt", "remote change\n", "remote commit")

    result = UpdateManager(repo_path=local)._execute_git_update()

    assert result["success"] is True, result
    assert json.loads((log_dir / "last-update.json").read_text())["status"] == "merged"
    assert not (Path(os.environ["LOBSTER_WORKSPACE"]) / "logs" / "last-update.json").exists()


DAILY_CHECK = Path(__file__).resolve().parents[2] / "scripts" / "daily-update-check.sh"


def test_daily_update_check_ms_timestamp_is_numeric(repos, tmp_path):
    """M2: `date +%s%3N` prints a literal "3N" on macOS; the id and file name
    must be a plain 13-digit millisecond timestamp."""
    upstream, seed, local = repos
    lib = local / "scripts" / "lib"
    lib.mkdir(parents=True)
    (lib / "json_message.sh").write_text(
        (DAILY_CHECK.parent / "lib" / "json_message.sh").read_text())
    _push_new_commit(seed, upstream, "remote.txt", "remote change\n", "remote commit")
    messages = tmp_path / "dmsgs"
    (messages / "inbox").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if k != "LOBSTER_UPDATE_REMOTE"}
    env.update(LOBSTER_INSTALL_DIR=str(local), LOBSTER_MESSAGES=str(messages),
               LOBSTER_CONFIG_DIR=str(tmp_path / "noconfig"), HOME=str(tmp_path))
    r = subprocess.run(["bash", str(DAILY_CHECK)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    (note,) = list((messages / "inbox").glob("*.json"))
    assert re.fullmatch(r"\d{13}_update_available\.json", note.name), note.name
    assert re.fullmatch(r"\d{13}_update_available", json.loads(note.read_text())["id"])
