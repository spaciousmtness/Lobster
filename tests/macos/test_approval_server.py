# tests/macos/test_approval_server.py
import json, time, threading
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
