# tests/macos/test_approval_server.py
import json, time, threading
from pathlib import Path
from macos import approval_server as s, approvals

def press(inbox, uid, data):
    (inbox / f"{time.time_ns()}.json").write_text(json.dumps(
        {"source": "telegram", "user_id": uid, "chat_id": uid, "text": f"[Button pressed: {data}]"}))

def test_yes_from_owner_approves(tmp_path):
    inbox, done = tmp_path / "in", tmp_path / "done"; inbox.mkdir(); done.mkdir()
    press(inbox, 285289661, "approve:abc")
    assert s.wait_for_answer(inbox, done, "abc", 285289661, time.time() + 2, 0.05) == "approved"
    assert not list(inbox.iterdir())

def test_no_denies(tmp_path):
    inbox, done = tmp_path / "in", tmp_path / "done"; inbox.mkdir(); done.mkdir()
    press(inbox, 285289661, "deny:abc")
    assert s.wait_for_answer(inbox, done, "abc", 285289661, time.time() + 2, 0.05) == "denied"

def test_stranger_and_other_request_ignored(tmp_path):
    inbox, done = tmp_path / "in", tmp_path / "done"; inbox.mkdir(); done.mkdir()
    press(inbox, 999, "approve:abc"); press(inbox, 285289661, "approve:zzz")
    assert s.wait_for_answer(inbox, done, "abc", 285289661, time.time() + 0.3, 0.05) == "timed out"
    assert len(list(inbox.iterdir())) == 2

def test_outbox_message_has_buttons(tmp_path):
    msg = s.build_request(285289661, "abc", "Read ~/Documents/x.md", "to summarize it")
    assert msg["chat_id"] == 285289661 and msg["source"] == "telegram"
    assert msg["buttons"] == [[{"text": "Yes", "callback_data": "approve:abc"},
                               {"text": "No", "callback_data": "deny:abc"}]]


# ---------------------------------------------------------------------------
# Final fix wave
# ---------------------------------------------------------------------------
import asyncio
import pytest

OWNER = 285289661


def press_with(inbox, data, original, uid=OWNER):
    f = inbox / f"{time.time_ns()}.json"
    f.write_text(json.dumps({"source": "telegram", "user_id": uid, "chat_id": uid, "type": "callback",
                             "text": f"[Button pressed: {data}]", "callback_data": data,
                             "original_message_text": original}))
    return f


# C2: the approval text leads with the full path and shows the content's
# size and first lines, inside a code block (the bot renders Markdown, so
# underscores in a path would otherwise vanish into italics).
def test_write_render_leads_with_full_path_in_code_block():
    path = "/Users/m/Documents/my_long_folder_name/" + "x" * 200 + "/notes_file.md"
    content = "line one\nline two\n" + "filler\n" * 500
    msg = s.build_request(OWNER, "abc", "save notes", "you asked", "Write", {"file_path": path, "content": content})
    text = msg["text"]
    assert "```" in text
    block = text.split("```", 1)[1]
    first = block.strip("\n").splitlines()[0]
    assert first == f"Write: {path}"
    assert f"{len(content)} chars" in block
    assert "line one" in block and "line two" in block
    assert block.count("filler") <= 5


def test_edit_render_shows_path_first():
    msg = s.build_request(OWNER, "abc", "fix", "why", "Edit",
                          {"file_path": "/Users/m/Desktop/a_b.txt", "old_string": "x", "new_string": "yy"})
    block = msg["text"].split("```", 1)[1]
    assert block.strip("\n").splitlines()[0] == "Edit: /Users/m/Desktop/a_b.txt"


def test_bash_render_full_command_in_code_block():
    cmd = "ls " + "a_b " * 200
    msg = s.build_request(OWNER, "abc", "list", "why", "Bash", {"command": cmd})
    assert f"Bash: {cmd}" in msg["text"].split("```", 1)[1]


def test_content_preview_cannot_close_the_code_block():
    msg = s.build_request(OWNER, "abc", "a", "b", "Write", {"file_path": "/Users/m/Documents/x", "content": "```\nhi"})
    assert msg["text"].count("```") == 2


# I3: a press only counts if it came from the message ask_melissa sent.
def test_press_from_forged_message_ignored(tmp_path):
    inbox, done = tmp_path / "in", tmp_path / "done"; inbox.mkdir(); done.mkdir()
    call = "Bash: ls notes"
    press_with(inbox, "approve:abc", "Clawdaddy wants to: something else\nBash: rm notes")
    assert s.wait_for_answer(inbox, done, "abc", OWNER, time.time() + 0.3, 0.05, expected_text=call) == "timed out"


def test_press_from_real_message_counts(tmp_path):
    inbox, done = tmp_path / "in", tmp_path / "done"; inbox.mkdir(); done.mkdir()
    msg = s.build_request(OWNER, "abc", "list", "why", "Bash", {"command": "ls notes"})
    # Telegram hands back the rendered plain text: code fences gone.
    press_with(inbox, "approve:abc", msg["text"].replace("```", ""))
    assert s.wait_for_answer(inbox, done, "abc", OWNER, time.time() + 2, 0.05,
                             expected_text=s.expected_press_text("Bash", {"command": "ls notes"}, "list")) == "approved"


# I1: presses for ids nobody is waiting on get swept out of the inbox.
def test_sweeper_moves_stray_presses_only(tmp_path):
    inbox, done = tmp_path / "in", tmp_path / "done"; inbox.mkdir()
    pending = tmp_path / "state" / "pending.json"
    s.add_pending("live", time.time() + 60, pending)
    stray = press_with(inbox, "approve:dead", "x")
    stray_deny = press_with(inbox, "deny:dead2", "x")
    live = press_with(inbox, "approve:live", "x")
    other = inbox / "1_msg.json"; other.write_text(json.dumps({"source": "telegram", "text": "hello"}))
    moved = s.sweep_stray_presses(inbox, done, pending, time.time())
    assert moved == 2
    assert live.exists() and other.exists()
    assert not stray.exists() and not stray_deny.exists()
    assert (done / stray.name).exists()


def test_expired_pending_is_swept(tmp_path):
    inbox, done = tmp_path / "in", tmp_path / "done"; inbox.mkdir()
    pending = tmp_path / "pending.json"
    s.add_pending("old", time.time() - 1, pending)
    f = press_with(inbox, "approve:old", "x")
    assert s.sweep_stray_presses(inbox, done, pending, time.time()) == 1
    assert not f.exists()


def test_remove_pending(tmp_path):
    pending = tmp_path / "pending.json"
    s.add_pending("a", time.time() + 60, pending)
    s.remove_pending("a", pending)
    assert s.pending_ids(pending, time.time()) == set()


# M5: refuse up front, without texting, what the guard refuses outright.
@pytest.mark.parametrize("tool,inp", [
    ("Write", {"file_path": "~/.zshrc", "content": "x"}),
    ("Read", {"file_path": "~/.ssh/id_ed25519"}),
    ("Bash", {"command": "cat *.md"}),
])
def test_ask_melissa_refuses_never_allowed_without_texting(tmp_path, monkeypatch, tool, inp):
    home = tmp_path / "home"; (home / "lobster-workspace").mkdir(parents=True)
    msgs = tmp_path / "clawmsgs"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", str(OWNER))
    monkeypatch.setattr(s, "OUTBOX_DIR", msgs / "outbox")
    monkeypatch.setattr(s, "SENT_DIR", msgs / "sent")
    monkeypatch.setattr(s, "INBOX_DIR", msgs / "inbox")
    out = asyncio.run(s.handle_ask_melissa({"action": "a", "why": "b", "tool_name": tool, "tool_input": inp,
                                            "timeout_seconds": 0}))
    assert out[0].text.startswith("never allowed")
    assert not (msgs / "outbox").exists()


def test_ask_melissa_texts_for_grantable_call(tmp_path, monkeypatch):
    home = tmp_path / "home"; (home / "lobster-workspace").mkdir(parents=True)
    msgs = tmp_path / "clawmsgs"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", str(OWNER))
    monkeypatch.setattr(s, "OUTBOX_DIR", msgs / "outbox")
    monkeypatch.setattr(s, "SENT_DIR", msgs / "sent")
    monkeypatch.setattr(s, "INBOX_DIR", msgs / "inbox")
    monkeypatch.setattr(s, "PENDING_PATH", tmp_path / "pending.json")
    monkeypatch.setattr(s.approvals, "DEFAULT_PATH", tmp_path / "approvals.json")
    out = asyncio.run(s.handle_ask_melissa({"action": "a", "why": "b", "tool_name": "Read",
                                            "tool_input": {"file_path": "~/Documents/x.md"}, "timeout_seconds": 0}))
    assert out[0].text == "timed out"
    assert len(list((msgs / "outbox").iterdir())) == 1
    assert s.pending_ids(tmp_path / "pending.json", time.time()) == set()


def test_no_dangling_docs_reference():
    assert "approval-server-step5" not in (Path(s.__file__)).read_text()
