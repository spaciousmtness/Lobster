# tests/macos/test_bot_wake.py
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "bot"))
_ENV = {"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_ALLOWED_USERS": "1"}


def _bot():
    with patch.dict(os.environ, _ENV):
        import lobster_bot
    return lobster_bot


def _hibernating(tmp_path):
    f = tmp_path / "lobster-state.json"
    f.write_text(json.dumps({"mode": "hibernate", "updated_at": "2000-01-01T00:00:00+00:00"}))
    return f


def test_wake_cmd_replaces_systemctl(tmp_path, monkeypatch):
    bot = _bot()
    state = _hibernating(tmp_path)
    monkeypatch.setenv("LOBSTER_WAKE_CMD", "launchctl kickstart -k gui/501/com.lobster.claude")
    run = MagicMock(return_value=MagicMock(returncode=0, stderr=""))
    with patch.object(bot, "LOBSTER_STATE_FILE", state), \
         patch.object(bot, "_is_claude_running", return_value=False), \
         patch.object(bot.subprocess, "run", run):
        bot.wake_claude_if_hibernating()
    argvs = [c.args[0] for c in run.call_args_list]
    assert argvs == [["launchctl", "kickstart", "-k", "gui/501/com.lobster.claude"]]
    assert json.loads(state.read_text())["mode"] == "active"


def test_wake_cmd_true_never_calls_systemctl(tmp_path, monkeypatch):
    bot = _bot()
    state = _hibernating(tmp_path)
    monkeypatch.setenv("LOBSTER_WAKE_CMD", "true")
    run = MagicMock(return_value=MagicMock(returncode=0, stderr=""))
    with patch.object(bot, "LOBSTER_STATE_FILE", state), \
         patch.object(bot, "_is_claude_running", return_value=False), \
         patch.object(bot.subprocess, "run", run):
        bot.wake_claude_if_hibernating()
    argvs = [c.args[0] for c in run.call_args_list]
    assert argvs == [["true"]]
    assert not any("systemctl" in a for argv in argvs for a in argv)


def test_no_wake_cmd_keeps_systemctl(tmp_path, monkeypatch):
    bot = _bot()
    state = _hibernating(tmp_path)
    monkeypatch.delenv("LOBSTER_WAKE_CMD", raising=False)
    run = MagicMock(return_value=MagicMock(returncode=0, stderr=""))
    with patch.object(bot, "LOBSTER_STATE_FILE", state), \
         patch.object(bot, "_is_claude_running", return_value=False), \
         patch.object(bot.subprocess, "run", run):
        bot.wake_claude_if_hibernating()
    assert [c.args[0] for c in run.call_args_list] == [["sudo", "systemctl", "restart", "lobster-claude"]]


def test_process_match_env(monkeypatch):
    bot = _bot()
    monkeypatch.setenv("LOBSTER_CLAUDE_MATCH", "claude.*lobster/macos/claude-settings.json")
    run = MagicMock(return_value=MagicMock(returncode=0, stdout="123\n", stderr=""))
    with patch.object(bot.subprocess, "run", run), patch.object(bot.time, "sleep"):
        assert bot._is_claude_running() is True
        bot._kill_stale_claude()
    assert [c.args[0] for c in run.call_args_list] == [
        ["pgrep", "-f", "claude.*lobster/macos/claude-settings.json"],
        ["pkill", "-f", "claude.*lobster/macos/claude-settings.json"],
    ]


def test_process_match_default(monkeypatch):
    bot = _bot()
    monkeypatch.delenv("LOBSTER_CLAUDE_MATCH", raising=False)
    run = MagicMock(return_value=MagicMock(returncode=1, stdout="", stderr=""))
    with patch.object(bot.subprocess, "run", run):
        assert bot._is_claude_running() is False
    assert run.call_args.args[0] == ["pgrep", "-f", "claude.*--dangerously-skip-permissions"]
