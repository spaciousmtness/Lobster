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
