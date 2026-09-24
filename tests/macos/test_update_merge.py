# tests/macos/test_update_merge.py
"""
Merge-based git updates (macOS Clawdaddy installs carry local commits on top
of upstream/main, so `git pull --ff-only` can never succeed there).

Every repo here is a throwaway temp dir -- this suite never touches
~/lobster's own git state, and never sets LOBSTER_UPDATE_REMOTE=upstream
against a real clone.

Fixture shape: a bare "upstream" repo (the far end of an `origin` remote) plus
a "local" clone that gets one local-only commit, mirroring a Mac install that
is upstream/main plus Mac-specific commits. A "seed" working clone is used
only to author and push new commits into the bare repo, standing in for
upstream advancing while the Mac install wasn't looking.
"""
import subprocess
from pathlib import Path

import pytest

from src.mcp.update_manager import UpdateManager


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
def _unset_remote(monkeypatch):
    # Default: exercise the code path with LOBSTER_UPDATE_REMOTE unset, same
    # as an operator who never set it. Individual tests override as needed.
    monkeypatch.delenv("LOBSTER_UPDATE_REMOTE", raising=False)
    monkeypatch.delenv("LOBSTER_SELFCHECK_CMD", raising=False)


def test_clean_merge_keeps_both_commits(repos, monkeypatch):
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
