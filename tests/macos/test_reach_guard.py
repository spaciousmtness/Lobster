# tests/macos/test_reach_guard.py
import json, time
from pathlib import Path
import pytest
from macos import reach_guard, approvals

@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"; ws = home / "lobster-workspace"; ws.mkdir(parents=True)
    (home / "messages").mkdir()
    return home, ws, home / "messages" / "config-approvals.json"

def d(env, tool, inp, now=None):
    home, ws, ap = env
    return reach_guard.decide(tool, inp, home, ws, ap, now or time.time())[0]

def test_inside_workspace_allowed(env):
    home, ws, _ = env
    assert d(env, "Write", {"file_path": str(ws / "note.md")}) == "allow"
    assert d(env, "Read", {"file_path": "note.md"}) == "allow"

def test_outside_denied_without_grant(env):
    home, _, _ = env
    assert d(env, "Read", {"file_path": str(home / "Documents/x.md")}) == "deny"

def test_outside_allowed_once_with_grant(env):
    home, _, ap = env
    inp = {"file_path": str(home / "Documents/x.md")}
    approvals.grant(approvals.key("Read", inp), ap)
    assert d(env, "Read", inp) == "allow"
    assert d(env, "Read", inp) == "deny"

def test_grant_expires(env):
    home, _, ap = env
    inp = {"file_path": str(home / "Documents/x.md")}
    approvals.grant(approvals.key("Read", inp), ap)
    assert d(env, "Read", inp, now=time.time() + 601) == "deny"

@pytest.mark.parametrize("p", [".ssh/id_ed25519", "Library/Keychains/login.keychain-db",
    ".config/clawdaddy/env", ".freedom/x", ".claude/.credentials.json", "lobster/config/config.env"])
def test_hard_denied_even_with_grant(env, p):
    home, _, ap = env
    inp = {"file_path": str(home / p)}
    approvals.grant(approvals.key("Read", inp), ap)
    assert d(env, "Read", inp) == "deny"

def test_bash_outside_path_needs_grant(env):
    home, _, _ = env
    assert d(env, "Bash", {"command": "ls ~/Documents"}) == "deny"
    assert d(env, "Bash", {"command": "ls notes"}) == "allow"
    assert d(env, "Bash", {"command": f"cat {home}/.ssh/id_ed25519"}) == "deny"

def test_web_needs_grant(env):
    assert d(env, "WebFetch", {"url": "https://example.com"}) == "deny"

@pytest.mark.parametrize("tool,p", [("Write", "messages/inbox/fake.json"), ("Write", "messages/config/x.json"),
    ("Write", ".local/state/clawdaddy/approvals.json"), ("Read", "messages/inbox/a.json"), ("Write", "messages/outbox/x.json")])
def test_agent_cannot_forge_approval_or_messages(env, tool, p):
    home, _, ap = env
    inp = {"file_path": str(home / p)}
    if "outbox" not in p:
        approvals.grant(approvals.key(tool, inp), ap)
    assert d(env, tool, inp) == "deny"

def test_messages_readable(env):
    home, _, _ = env
    (home / "messages" / "sent").mkdir()
    assert d(env, "Read", {"file_path": str(home / "messages/sent/a.json")}) == "allow"

def test_own_mcp_tools_allowed(env):
    assert d(env, "mcp__lobster-inbox__send_reply", {"chat_id": 1, "text": "hi"}) == "allow"
    assert d(env, "mcp__clawdaddy-approval__ask_melissa", {"action": "x", "why": "y"}) == "allow"
