# tests/macos/test_mac_config.py
import json
from pathlib import Path

MACOS = Path(__file__).resolve().parents[2] / "macos"


def _settings():
    return json.loads((MACOS / "claude-settings.json").read_text())


def test_permissions_deny_secrets_and_approval_state():
    deny = set(_settings()["permissions"]["deny"])
    for p in ("~/.ssh/**", "~/.gnupg/**", "~/Library/Keychains/**", "~/.config/clawdaddy/**",
              "~/.config/openclaw/**", "~/.freedom/**", "~/.claude/.credentials.json",
              "~/lobster/config/config.env", "~/.local/state/clawdaddy/**", "~/messages/inbox/**",
              "~/messages/config/**", "~/lobster-workspace/.claude/**",
              "~/Library/Application Support/1Password/**", "~/Library/Group Containers/*1password*/**"):
        assert f"Read({p})" in deny and f"Edit({p})" in deny, p
    # Write(path) rules are accepted but never consulted by Claude Code; Read/Edit cover Write.
    assert not any(r.startswith("Write(") for r in deny)


def test_sandbox_floor():
    sb = _settings()["sandbox"]
    assert sb["enabled"] is True and sb["allowUnsandboxedCommands"] is False and sb["failIfUnavailable"] is True
    fs = sb["filesystem"]
    assert fs["allowWrite"] == ["~/lobster-workspace"]
    assert {"~/.ssh", "~/Library", "~/.freedom", "~/lobster", "~/messages", "~/.local/state/clawdaddy",
            "~/lobster-workspace/.claude"} <= set(fs["denyWrite"])
    assert {"~/.ssh", "~/.gnupg", "~/Library/Keychains", "~/.config/clawdaddy", "~/.freedom",
            "~/lobster/config", "~/.local/state/clawdaddy", "~/.claude/.credentials.json",
            "~/.config/openclaw", "~/Library/Application Support/1Password",
            "~/Library/Group Containers/*1password*", "~/messages/inbox", "~/messages/config",
            "~/lobster-workspace/.claude"} <= set(fs["denyRead"])
    assert sb["network"]["allowedDomains"] == [] and sb["network"]["strictAllowlist"] is True


def test_hook_gates_every_tool():
    (entry,) = _settings()["hooks"]["PreToolUse"]
    assert entry["matcher"] == "*"
    cmd = entry["hooks"][0]["command"]
    assert cmd.startswith("$HOME/lobster/.venv/bin/python $HOME/lobster/macos/reach_guard.py")
    # A hook that cannot start exits non-2, which Claude Code treats as non-blocking.
    assert cmd.endswith(" || exit 2")


def test_mcp_template_renders():
    rendered = (MACOS / "mcp.json.in").read_text().replace("@HOME@", "/Users/x")
    servers = json.loads(rendered)["mcpServers"]
    assert set(servers) == {"lobster-inbox", "clawdaddy-approval"}
    assert servers["clawdaddy-approval"]["args"] == ["/Users/x/lobster/macos/approval_server.py"]
    assert "@HOME@" not in rendered


def test_dispatcher_instructions():
    text = (MACOS / "CLAUDE.mac.md").read_text()
    for needle in ("~/lobster-workspace", "ask_melissa", "[Button pressed: approve:",
                   "[Button pressed: deny:", "mark_processed", "literal",
                   "never allowed: command must be literal"):
        assert needle in text, needle


# ---------------------------------------------------------------------------
# Final fix wave
# ---------------------------------------------------------------------------
import re
import plistlib

REPO = MACOS.parent


def _mac_env():
    env = {}
    for line in (MACOS / "mac.env").read_text().splitlines():
        m = re.match(r'^([A-Z_]+)="?(.*?)"?$', line)
        if m:
            env[m.group(1)] = m.group(2)
    return env


# C2: Claude Code's own permission layer mirrors the guard's write floor.
def test_edit_denies_mirror_write_floor():
    deny = set(_settings()["permissions"]["deny"])
    for p in ("~/lobster/**", "~/Library/**", "~/.claude/**", "~/.config/**", "~/.local/**",
              "~/.zshrc", "~/.zprofile", "~/.zshenv", "~/.zlogin", "~/.bashrc", "~/.bash_profile",
              "~/.profile", "~/.gitconfig",
              # Claude Code permission rules: "//" is an absolute path, "/" is settings-relative.
              "//opt/homebrew/**", "//usr/local/**"):
        assert f"Edit({p})" in deny, p


# I3: outbox/ and sent/ are unreadable at every layer.
def test_outbox_and_sent_denied_everywhere():
    s = _settings()
    deny = set(s["permissions"]["deny"])
    fs = s["sandbox"]["filesystem"]
    for sub in ("outbox", "sent"):
        assert f"Read(~/messages/{sub}/**)" in deny
        assert f"~/messages/{sub}" in fs["denyRead"]


# C3 / M3 / I4: mac.env points Lobster at the right dirs.
def test_mac_env_dirs():
    env = _mac_env()
    assert env["LOBSTER_USER_CONFIG"] == "$HOME/lobster-workspace/user-config"
    assert env["LOBSTER_CONFIG_DIR"] == "$HOME/lobster/config"
    assert env["LOBSTER_LOG_DIR"] == "$HOME/.local/state/clawdaddy/logs"


# C3: the dispatcher is told where its instructions are and what to skip.
def test_dispatcher_instructions_bootup():
    text = (MACOS / "CLAUDE.mac.md").read_text()
    bootup = (REPO / ".claude" / "sys.dispatcher.bootup.md").read_text()
    for needle in ("~/lobster/CLAUDE.md", "~/lobster/.claude/sys.dispatcher.bootup.md",
                   "~/lobster-workspace/user-config", "~/lobster-user-config"):
        assert needle in text, needle
    # Steps 2c and 2e are the non-literal Bash ones; name them exactly as the bootup file does.
    assert re.search(r"^2c\. Check `~/lobster-workspace/data/context-handoff.json`", bootup, re.M)
    assert re.search(r"^2e\. \*\*Check for a debug-mode reflection prompt sidecar\*\*", bootup, re.M)
    assert re.search(r"[Ss]kip.*\b2c\b.*context-handoff", text)
    assert re.search(r"[Ss]kip.*\b2e\b.*bootup-prompt", text)


# C1 / I4: launchd runs the launcher from ~/lobster and logs outside the workspace.
def _plist(name):
    return plistlib.loads((MACOS / f"{name}.plist.in").read_bytes())


def test_claude_plist_working_directory_is_repo():
    assert _plist("com.lobster.claude")["WorkingDirectory"] == "@HOME@/lobster"


def test_plist_logs_outside_workspace():
    for name in ("com.lobster.claude", "com.lobster.router", "com.lobster.update-check"):
        pl = _plist(name)
        for key in ("StandardOutPath", "StandardErrorPath"):
            assert pl[key].startswith("@HOME@/.local/state/clawdaddy/logs/"), (name, key, pl[key])
