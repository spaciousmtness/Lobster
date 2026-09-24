# tests/macos/test_approval_server.py
import json, time, threading
from pathlib import Path
from macos import approval_server as s, approvals

def press(inbox, uid, data):
    (inbox / f"{time.time_ns()}.json").write_text(json.dumps(
        {"source": "telegram", "user_id": uid, "chat_id": uid, "text": f"[Button pressed: {data}]"}))




def test_outbox_message_has_buttons(tmp_path):
    msg = s.build_request(285289661, "abc", "Read ~/Documents/x.md", "to summarize it")
    assert msg["chat_id"] == 285289661 and msg["source"] == "telegram"
    # The router's build_inline_keyboard reads [label, callback_data] pairs.
    assert msg["buttons"] == [[["Yes", "approve:abc"], ["No", "deny:abc"]]]


def test_buttons_render_through_the_real_router_builder():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "bot"))
    import os
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
    os.environ.setdefault("TELEGRAM_ALLOWED_USERS", "1")
    from lobster_bot import build_inline_keyboard
    msg = s.build_request(285289661, "abc", "x", "y")
    kb = build_inline_keyboard(msg["buttons"])
    row = kb.inline_keyboard[0]
    assert [(b.text, b.callback_data) for b in row] == [("Yes", "approve:abc"), ("No", "deny:abc")]


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
    out = asyncio.run(s.handle_ask_melissa({"action": "a", "why": "b", "tool_name": tool, "tool_input": inp}))
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
                                            "tool_input": {"file_path": "~/Documents/x.md"}}))
    assert out[0].text.startswith("Asked Melissa (request ")
    assert len(list((msgs / "outbox").iterdir())) == 1
    assert len(s.pending_ids(tmp_path / "pending.json", time.time())) == 1


def test_no_dangling_docs_reference():
    assert "approval-server-step5" not in (Path(s.__file__)).read_text()


# ---------------------------------------------------------------------------
# Non-blocking ask_melissa (Task 7 live finding: a 30-minute block froze the
# dispatcher). ask_melissa records the request and returns; the sweeper
# honors the press, grants, and tells the dispatcher through a system inbox
# message.
# ---------------------------------------------------------------------------
import subprocess
import sys as _sys


def _setup(tmp_path, monkeypatch):
    home = tmp_path / "home"; (home / "lobster-workspace").mkdir(parents=True)
    msgs = tmp_path / "clawmsgs"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", str(OWNER))
    monkeypatch.setattr(s, "OUTBOX_DIR", msgs / "outbox")
    monkeypatch.setattr(s, "SENT_DIR", msgs / "sent")
    monkeypatch.setattr(s, "INBOX_DIR", msgs / "inbox")
    monkeypatch.setattr(s, "PROCESSED_DIR", msgs / "processed")
    monkeypatch.setattr(s, "PENDING_PATH", tmp_path / "state" / "pending.json")
    monkeypatch.setattr(s.approvals, "DEFAULT_PATH", tmp_path / "state" / "approvals.json")
    (msgs / "inbox").mkdir(parents=True)
    return msgs


BASH = ("Bash", {"command": "ls /Users/m/Documents/notes"})


def _ask(tool=BASH):
    out = asyncio.run(asyncio.wait_for(
        s.handle_ask_melissa({"action": "list notes", "why": "you asked", "tool_name": tool[0],
                              "tool_input": tool[1]}), timeout=10))
    return out[0].text


def _request_id(text):
    m = re.match(r"^Asked Melissa \(request ([0-9a-f]+)\)\. ", text)
    assert m, text
    return m.group(1)


def _sent(msgs):
    [f] = list((msgs / "outbox").iterdir())
    return json.loads(f.read_text())


def _notes(msgs):
    out = []
    for f in sorted((msgs / "inbox").glob("*.json")):
        m = json.loads(f.read_text())
        if m.get("source") == "system":
            out.append(m)
    return out


def _sweep(msgs, tmp_path, now=None):
    return s.sweep_once(msgs / "inbox", msgs / "processed", tmp_path / "state" / "pending.json",
                        tmp_path / "state" / "approvals.json", OWNER, now if now is not None else time.time())


import re


def test_ask_melissa_returns_at_once_and_records_pending(tmp_path, monkeypatch):
    msgs = _setup(tmp_path, monkeypatch)
    t0 = time.time()
    text = _ask()
    assert time.time() - t0 < 2
    rid = _request_id(text)
    assert text == (f"Asked Melissa (request {rid}). Tell her briefly you're waiting for her tap, then "
                    "carry on with other messages. A system message will arrive when she answers; "
                    "retry the exact same call only after it says approved.")
    sent = _sent(msgs)
    assert sent["text"].splitlines()[0] == "⏳ Needs your tap:"
    assert sent["buttons"] == [[["Yes", f"approve:{rid}"], ["No", f"deny:{rid}"]]]
    pending = json.loads((tmp_path / "state" / "pending.json").read_text())
    rec = pending[rid]
    assert rec["tool_name"] == "Bash" and rec["tool_input"] == BASH[1]
    assert abs(rec["created"] - t0) < 5
    assert rid in s.pending_ids(tmp_path / "state" / "pending.json", time.time())


def test_owner_approve_press_grants_and_notes(tmp_path, monkeypatch):
    msgs = _setup(tmp_path, monkeypatch)
    rid = _request_id(_ask())
    f = press_with(msgs / "inbox", f"approve:{rid}", _sent(msgs)["text"].replace("```", ""))
    _sweep(msgs, tmp_path)
    assert not f.exists() and (msgs / "processed" / f.name).exists()
    assert s.pending_ids(tmp_path / "state" / "pending.json", time.time()) == set()
    [note] = _notes(msgs)
    assert note["text"] == "Melissa approved: Bash: ls /Users/m/Documents/notes. Retry that exact call now."
    assert note["chat_id"] == 0 and note["id"] and note["timestamp"] and note["type"]
    assert (msgs / "inbox" / f"{note['id']}.json").exists()
    ap = tmp_path / "state" / "approvals.json"
    assert approvals.consume(approvals.key(*BASH), ap, time.time()) is True


def test_grant_ttl_counts_from_the_tap(tmp_path, monkeypatch):
    msgs = _setup(tmp_path, monkeypatch)
    rid = _request_id(_ask())
    press_with(msgs / "inbox", f"approve:{rid}", _sent(msgs)["text"])
    t_tap = time.time()
    _sweep(msgs, tmp_path)
    ap = tmp_path / "state" / "approvals.json"
    [expiry] = json.loads(ap.read_text()).values()
    assert expiry >= t_tap + approvals.TTL - 1


def test_owner_deny_press_notes_without_grant(tmp_path, monkeypatch):
    msgs = _setup(tmp_path, monkeypatch)
    rid = _request_id(_ask())
    f = press_with(msgs / "inbox", f"deny:{rid}", _sent(msgs)["text"])
    _sweep(msgs, tmp_path)
    assert (msgs / "processed" / f.name).exists()
    [note] = _notes(msgs)
    assert note["text"] == "Melissa declined: Bash: ls /Users/m/Documents/notes. Tell her you won't do it."
    assert not approvals.consume(approvals.key(*BASH), tmp_path / "state" / "approvals.json", time.time())
    assert s.pending_ids(tmp_path / "state" / "pending.json", time.time()) == set()


def test_pending_expires_with_a_note(tmp_path, monkeypatch):
    msgs = _setup(tmp_path, monkeypatch)
    rid = _request_id(_ask())
    _sweep(msgs, tmp_path, now=time.time() + 29 * 60)
    assert _notes(msgs) == []
    _sweep(msgs, tmp_path, now=time.time() + 31 * 60)
    [note] = _notes(msgs)
    assert note["text"] == ("Approval for Bash: ls /Users/m/Documents/notes expired without an answer; "
                            "tell Melissa.")
    assert json.loads((tmp_path / "state" / "pending.json").read_text()) == {}
    # A late press after expiry is only swept away.
    f = press_with(msgs / "inbox", f"approve:{rid}", _sent(msgs)["text"])
    _sweep(msgs, tmp_path, now=time.time() + 32 * 60)
    assert not f.exists() and len(_notes(msgs)) == 1
    assert not approvals.consume(approvals.key(*BASH), tmp_path / "state" / "approvals.json", time.time())


@pytest.mark.parametrize("uid,original", [
    (999, None),                                          # not the owner
    (OWNER, "Clawdaddy wants to: x\nBash: rm -rf /Users/m/Documents"),  # forged message
])
def test_foreign_or_forged_press_is_ignored(tmp_path, monkeypatch, uid, original):
    msgs = _setup(tmp_path, monkeypatch)
    rid = _request_id(_ask())
    f = press_with(msgs / "inbox", f"approve:{rid}", original or _sent(msgs)["text"], uid=uid)
    _sweep(msgs, tmp_path)
    assert not f.exists()  # moved out of the dispatcher's way
    assert _notes(msgs) == []
    assert not approvals.consume(approvals.key(*BASH), tmp_path / "state" / "approvals.json", time.time())
    assert rid in s.pending_ids(tmp_path / "state" / "pending.json", time.time())


def test_press_is_honored_by_a_fresh_server_process(tmp_path, monkeypatch):
    msgs = _setup(tmp_path, monkeypatch)
    rid = _request_id(_ask())
    press_with(msgs / "inbox", f"approve:{rid}", _sent(msgs)["text"])
    repo = Path(__file__).resolve().parents[2]
    code = ("import sys, time; from pathlib import Path; from macos import approval_server as s; "
            "a = [Path(x) for x in sys.argv[1:5]]; s.sweep_once(*a, int(sys.argv[5]), time.time())")
    subprocess.run([_sys.executable, "-c", code, str(msgs / "inbox"), str(msgs / "processed"),
                    str(tmp_path / "state" / "pending.json"), str(tmp_path / "state" / "approvals.json"),
                    str(OWNER)], cwd=repo, check=True, env={**__import__("os").environ, "HOME": str(tmp_path / "home")})
    [note] = _notes(msgs)
    assert note["text"].startswith("Melissa approved: ")
    assert approvals.consume(approvals.key(*BASH), tmp_path / "state" / "approvals.json", time.time())


def test_a_press_is_honored_only_once(tmp_path, monkeypatch):
    msgs = _setup(tmp_path, monkeypatch)
    rid = _request_id(_ask())
    press_with(msgs / "inbox", f"approve:{rid}", _sent(msgs)["text"])
    press_with(msgs / "inbox", f"approve:{rid}", _sent(msgs)["text"])
    _sweep(msgs, tmp_path)
    assert len(_notes(msgs)) == 1
    ap = tmp_path / "state" / "approvals.json"
    assert approvals.consume(approvals.key(*BASH), ap, time.time())
    assert not approvals.consume(approvals.key(*BASH), ap, time.time())


def test_sweeper_interval_is_at_most_two_seconds():
    assert s.SWEEP_INTERVAL_SECONDS <= 2
