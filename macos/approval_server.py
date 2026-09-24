#!/usr/bin/env python3
"""ask_melissa: the MCP server the agent calls when the reach guard refuses.

When `reach_guard.py` denies a tool call for lacking a one-shot approval, the
agent is told to call `ask_melissa` with what it wants to do and why. This
server is that tool: it records the request as pending, texts Melissa a
Yes/No button through Lobster's outbox, and returns at once -- it never
waits for her, because a blocked tool call freezes the whole dispatcher
(Task 7 live finding, 2026-09-24). A sweeper thread in this process watches
the inbox for her press; on "Yes" it records a one-shot grant in the same
`~/.local/state/clawdaddy/approvals.json` file the reach guard consumes from,
and on any outcome (yes, no, or 30 minutes of silence) it drops a source
"system" inbox message telling the dispatcher, which then retries the exact
same call (or tells Melissa it won't).

Self-contained on purpose: it follows the same `mcp.server` + stdio pattern
as `src/mcp/inbox_server.py`, but it does not import from it and it never
touches Lobster's own outbox/inbox code. It reads and writes the same
`$LOBSTER_MESSAGES` directories Lobster's bot process already watches, using
the same message shapes, so Melissa's Telegram client and Lobster's outbox
watcher need no changes to understand an approval request or her reply to it.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from macos import approvals  # imported as part of the `macos` package (tests)
    from macos import reach_guard
except ImportError:  # run directly, e.g. `python macos/approval_server.py`
    import approvals
    import reach_guard

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# How long a request stays pending before it expires unanswered.
PENDING_TTL_SECONDS = 1800

# The non-path remainder of a tool call's rendering is truncated to this
# many chars. The path (or Bash command) itself is never truncated: Melissa
# always sees exactly where a call reaches.
RENDER_TRUNCATE_CHARS = 300

# How much of a Write's content / an Edit's new text the approval shows.
PREVIEW_LINES = 5
PREVIEW_LINE_CHARS = 120

# Pending requests (id -> {tool_name, tool_input, action, expected_text,
# chat_id, created, deadline}), shared by every approval_server process and
# surviving their restarts. Lives beside approvals.json, which the agent can
# neither read nor write (reach guard hard-deny + sandbox).
PENDING_PATH = approvals.DEFAULT_PATH.parent / "pending-approvals.json"

# How often the sweeper looks for presses, after one sweep at startup. It is
# the only thing between Melissa's tap and the dispatcher hearing about it.
SWEEP_INTERVAL_SECONDS = 1.0

_PRESS_RE = re.compile(r"^\[Button pressed: (approve|deny):([^\]]*)\]$")

MESSAGES_DIR = Path(os.environ.get("LOBSTER_MESSAGES", str(Path.home() / "messages"))).expanduser()
INBOX_DIR = MESSAGES_DIR / "inbox"
OUTBOX_DIR = MESSAGES_DIR / "outbox"
PROCESSED_DIR = MESSAGES_DIR / "processed"
SENT_DIR = MESSAGES_DIR / "sent"


# ---------------------------------------------------------------------------
# Pure functions (tested directly, no filesystem side effects beyond what
# their own arguments name)
# ---------------------------------------------------------------------------


_PRIMARY_KEYS = ("command", "file_path", "notebook_path", "path", "url", "query")
_PREVIEW_KEYS = ("content", "new_string", "new_source")


def _preview(text: str) -> list[str]:
    """Size plus the first few lines of a text payload. Backticks become
    quotes so a preview can never close the surrounding code block."""
    lines = [f"{len(text)} chars"]
    for line in text.splitlines()[:PREVIEW_LINES]:
        line = line.replace("`", "'")
        if len(line) > PREVIEW_LINE_CHARS:
            line = line[: PREVIEW_LINE_CHARS - 3] + "..."
        lines.append(f"  {line}")
    return lines


def _render_call(tool_name: str, tool_input: dict) -> str:
    """Rendering of a tool call for the outbox message, path first.

    The first line is `Tool: <command or full path>`, never truncated, so
    Melissa always sees exactly what runs or where it writes. For a write,
    the content's size and first lines follow; any other arguments come
    after, truncated to ~300 chars.
    """
    primary_key = next((k for k in _PRIMARY_KEYS if isinstance(tool_input.get(k), str)), None)
    lines = [f"{tool_name}: {tool_input[primary_key]}" if primary_key else f"{tool_name}:"]
    if tool_name == "Bash":
        return lines[0]
    rest = {k: v for k, v in tool_input.items() if k != primary_key and k not in _PREVIEW_KEYS}
    for k in _PREVIEW_KEYS:
        v = tool_input.get(k)
        if isinstance(v, str):
            label = "content" if k == "content" else "new text"
            size, *first = _preview(v)
            lines.append(f"{label}: {size}")
            lines.extend(first)
    if rest:
        try:
            rendered = json.dumps(rest, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            rendered = str(rest)
        rendered = rendered.replace("`", "'")
        if len(rendered) > RENDER_TRUNCATE_CHARS:
            rendered = rendered[: RENDER_TRUNCATE_CHARS - 3] + "..."
        lines.append(f"other: {rendered}")
    return "\n".join(lines)


def expected_press_text(tool_name: str | None, tool_input: dict | None, action: str) -> str:
    """What a genuine press's `original_message_text` must contain: the
    request's Call rendering (or, with no tool, its action line)."""
    if tool_name:
        return _render_call(tool_name, tool_input or {})
    return f"Clawdaddy wants to: {action}"


def _norm_ws(text: str) -> str:
    return " ".join(text.split())


def build_request(
    owner_id: int,
    request_id: str,
    action: str,
    why: str,
    tool_name: str | None = None,
    tool_input: dict | None = None,
    ts_ms: int | None = None,
) -> dict:
    """The outbox message asking Melissa to approve or deny one action.

    Matches Lobster's outbox JSON shape (`id`, `source`, `chat_id`, `text`,
    `timestamp`, `buttons`) exactly, so the existing Telegram outbox watcher
    sends it with no changes on its end.
    """
    ts_ms = ts_ms if ts_ms is not None else int(time.time() * 1000)
    lines = ["⏳ Needs your tap:", f"Clawdaddy wants to: {action}", f"Why: {why}"]
    if tool_name:
        # A code block: the bot renders Markdown, and underscores in a path
        # would otherwise turn into italics and disappear.
        lines.append("Call:")
        lines.append(f"```\n{_render_call(tool_name, tool_input or {})}\n```")
    return {
        "id": f"{ts_ms}_approval",
        "source": "telegram",
        "chat_id": owner_id,
        "text": "\n".join(lines),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        # [label, callback_data] pairs: the shape the router's
        # build_inline_keyboard reads from outbox files.
        "buttons": [
            [
                ["Yes", f"approve:{request_id}"],
                ["No", f"deny:{request_id}"],
            ]
        ],
    }


def _press_is_genuine(msg: dict, owner_id: int | None, expected_text: str | None) -> bool:
    """A press counts only if it came from `owner_id` on Telegram and from
    the message this request sent: the bot records the pressed message's
    text as `original_message_text`, and it must contain `expected_text`
    (compared whitespace-normalized). A press on a button the agent sent
    itself with a guessed or leaked id fails this."""
    if owner_id is None or msg.get("source") != "telegram" or msg.get("user_id") != owner_id:
        return False
    if not expected_text:
        return False
    return _norm_ws(expected_text) in _norm_ws(str(msg.get("original_message_text") or ""))


def _claim(matched: Path, processed: Path) -> None:
    """Move a matched button-press file into `processed`, best-effort."""
    try:
        processed.mkdir(parents=True, exist_ok=True, mode=0o700)
        matched.rename(processed / matched.name)
    except OSError:
        pass  # already moved/consumed by someone else -- the answer still stands


# ---------------------------------------------------------------------------
# Pending requests and the sweeper. The sweeper answers presses for pending
# requests, expires the ones nobody answered, and clears every other
# approve:/deny: press: the dispatcher is told never to mark a button press
# processed, so a press nobody is waiting for (late, duplicate, forged, or
# for an expired request) would otherwise sit in the inbox forever and make
# wait_for_messages return at once, every time.
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _pending_locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path.parent / (path.name + ".lock"), "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _load_pending(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _deadline(entry: Any) -> float | None:
    """An entry's deadline. Bare numbers are the pre-non-blocking format
    (id -> deadline) and still read, so an old file is swept, not crashed on."""
    if isinstance(entry, (int, float)):
        return float(entry)
    if isinstance(entry, dict) and isinstance(entry.get("deadline"), (int, float)):
        return float(entry["deadline"])
    return None


def add_pending(request_id: str, deadline: float, path: Path, record: dict | None = None) -> None:
    with _pending_locked(path):
        data = _load_pending(path)
        data[request_id] = {**(record or {}), "deadline": deadline}
        _atomic_write(path, data)


def _take_pending(request_id: str, path: Path, now: float) -> dict | None:
    """Remove and return a live pending request, atomically, so two sweepers
    (one per approval_server process) can never both answer one press."""
    with _pending_locked(path):
        data = _load_pending(path)
        entry = data.get(request_id)
        d = _deadline(entry)
        if not isinstance(entry, dict) or d is None or d < now:
            return None
        del data[request_id]
        _atomic_write(path, data)
    return entry


def _take_expired(path: Path, now: float) -> list[dict]:
    """Remove and return every pending request past its deadline."""
    with _pending_locked(path):
        data = _load_pending(path)
        expired = {k: v for k, v in data.items() if (_deadline(v) or 0) < now}
        if not expired:
            return []
        for k in expired:
            del data[k]
        _atomic_write(path, data)
    return [v for v in expired.values() if isinstance(v, dict)]


def remove_pending(request_id: str, path: Path) -> None:
    with _pending_locked(path):
        data = _load_pending(path)
        if data.pop(request_id, None) is not None:
            _atomic_write(path, data)


def pending_ids(path: Path, now: float) -> set[str]:
    with _pending_locked(path):
        data = _load_pending(path)
    return {k for k, v in data.items() if (d := _deadline(v)) is not None and d >= now}


def sweep_stray_presses(inbox: Path, processed: Path, pending_path: Path, now: float) -> int:
    """Move every approve:/deny: button press in `inbox` whose id is not
    pending (unknown, finished, or past its deadline) into `processed`.
    Returns how many were moved."""
    if not inbox.is_dir():
        return 0
    live = pending_ids(pending_path, now)
    moved = 0
    for f in sorted(inbox.glob("*.json")):
        try:
            msg = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(msg, dict):
            continue
        m = _PRESS_RE.match(str(msg.get("text") or ""))
        if not m or m.group(2) in live:
            continue
        before = f.exists()
        _claim(f, processed)
        if before and not f.exists():
            moved += 1
    return moved


def _one_line_call(entry: dict) -> str:
    tool_name = entry.get("tool_name")
    if tool_name:
        tool_input = entry.get("tool_input")
        return _render_call(str(tool_name), tool_input if isinstance(tool_input, dict) else {}).splitlines()[0]
    return str(entry.get("action") or "an action")


def _write_inbox_note(inbox: Path, request_id: str, text: str) -> None:
    """A source "system" inbox message for the dispatcher: the same shape
    scripts/daily-update-check.sh and update_manager's inbox note use
    ("system" is the only source the inbox server's quarantine guard
    accepts for a system-generated message)."""
    msg_id = f"{int(time.time() * 1000)}_approval_{request_id}"
    _atomic_write(inbox / f"{msg_id}.json", {
        "id": msg_id,
        "source": "system",
        "chat_id": 0,
        "type": "approval_notification",
        "text": text,
        "timestamp": datetime.now().astimezone().isoformat(),
    })


def answer_presses(inbox: Path, processed: Path, pending_path: Path, approvals_path: Path,
                   owner_id: int | None, now: float) -> int:
    """Act on every approve:/deny: press for a live pending request, in
    `inbox` and in `processing/` (the dispatcher is told to leave presses
    alone, but may still have claimed one). A genuine press (the owner's,
    from the message this request sent) takes the request off the pending
    list, grants on approve (the grant's TTL starts now, at the tap), and
    leaves a system inbox note. Any other press for a pending id is moved
    out of the way and ignored; the request stays pending. Returns how many
    requests were answered."""
    live = pending_ids(pending_path, now)
    if not live:
        return 0
    answered = 0
    for base in (inbox, inbox.parent / "processing"):
        if not base.is_dir():
            continue
        for f in sorted(base.glob("*.json")):
            try:
                msg = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(msg, dict):
                continue
            m = _PRESS_RE.match(str(msg.get("text") or ""))
            if not m or m.group(2) not in live:
                continue
            verb, request_id = m.groups()
            with _pending_locked(pending_path):
                entry = _load_pending(pending_path).get(request_id)
            expected = entry.get("expected_text") if isinstance(entry, dict) else None
            if not _press_is_genuine(msg, owner_id, expected):
                _claim(f, processed)
                continue
            entry = _take_pending(request_id, pending_path, now)
            _claim(f, processed)
            if entry is None:
                continue  # another sweeper answered it first
            call = _one_line_call(entry)
            if verb == "approve":
                approvals.grant(approvals.key(str(entry.get("tool_name") or ""), entry.get("tool_input") or {}),
                                approvals_path)
                _write_inbox_note(inbox, request_id, f"Melissa approved: {call}. Retry that exact call now.")
            else:
                _write_inbox_note(inbox, request_id, f"Melissa declined: {call}. Tell her you won't do it.")
            live.discard(request_id)
            answered += 1
    return answered


def expire_pending(inbox: Path, pending_path: Path, now: float) -> int:
    expired = _take_expired(pending_path, now)
    for entry in expired:
        _write_inbox_note(inbox, str(entry.get("id") or "expired"),
                          f"Approval for {_one_line_call(entry)} expired without an answer; tell Melissa.")
    return len(expired)


def sweep_once(inbox: Path, processed: Path, pending_path: Path, approvals_path: Path,
               owner_id: int | None, now: float) -> None:
    """One sweeper pass: answer presses, expire the unanswered, clear strays.
    Everything it needs is on disk, so a fresh server process picks up
    requests an earlier one sent."""
    answer_presses(inbox, processed, pending_path, approvals_path, owner_id, now)
    expire_pending(inbox, pending_path, now)
    sweep_stray_presses(inbox, processed, pending_path, now)


def _sweep_forever(interval: float) -> None:
    while True:
        try:
            sweep_once(INBOX_DIR, PROCESSED_DIR, PENDING_PATH, approvals.DEFAULT_PATH, _owner_id(), time.time())
        except Exception:
            pass  # a failed sweep must never take the approval server down
        time.sleep(interval)


def _owner_id() -> int | None:
    """First entry of TELEGRAM_ALLOWED_USERS, or None if unset/non-numeric."""
    raw = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    first = raw.split(",")[0].strip() if raw else ""
    if not first:
        return None
    try:
        return int(first)
    except ValueError:
        return None


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".approval-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _write_outbox_message(ts_ms: int, msg: dict) -> None:
    """Write the approval request to outbox/, atomically, plus a sent/ copy
    -- the same two-write pattern `send_reply` uses in `src/mcp/inbox_server.py`."""
    filename = f"{ts_ms}_approval.json"
    _atomic_write(OUTBOX_DIR / filename, msg)
    try:
        _atomic_write(SENT_DIR / filename, msg)
    except OSError:
        pass  # sent/ is a history copy; losing it never blocks the approval


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


async def handle_ask_melissa(args: dict[str, Any]) -> list[TextContent]:
    action = str(args.get("action", ""))
    why = str(args.get("why", ""))
    tool_name = str(args.get("tool_name", ""))
    tool_input = args.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {"value": tool_input}

    owner_id = _owner_id()
    if owner_id is None:
        return [TextContent(type="text", text="denied")]

    # M5: refuse up front, without texting Melissa, anything the reach guard
    # would refuse even with her "Yes".
    home = Path.home()
    never = reach_guard.never_allowed_reason(
        tool_name,
        tool_input,
        home,
        Path(os.environ.get("LOBSTER_WORKSPACE", str(home / "lobster-workspace"))).expanduser(),
        messages=Path(os.environ.get("LOBSTER_MESSAGES", str(home / "messages"))).expanduser(),
    )
    if never is not None:
        return [TextContent(type="text", text=f"{never} (not sent to Melissa; approval cannot help)")]

    request_id = secrets.token_hex(4)
    ts_ms = int(time.time() * 1000)
    msg = build_request(owner_id, request_id, action, why, tool_name, tool_input, ts_ms=ts_ms)

    created = time.time()
    add_pending(request_id, created + PENDING_TTL_SECONDS, PENDING_PATH, {
        "id": request_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "action": action,
        "expected_text": expected_press_text(tool_name, tool_input, action),
        "chat_id": owner_id,
        "created": created,
    })
    try:
        _write_outbox_message(ts_ms, msg)
    except Exception:
        remove_pending(request_id, PENDING_PATH)
        raise

    return [TextContent(type="text", text=(
        f"Asked Melissa (request {request_id}). Tell her briefly you're waiting for her tap, then "
        "carry on with other messages. A system message will arrive when she answers; "
        "retry the exact same call only after it says approved."))]


server = Server("clawdaddy-approval")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="ask_melissa",
            description=(
                "Ask Melissa to approve one specific tool call the reach guard just "
                "refused. Texts her Yes/No buttons on Telegram and returns at once; "
                "it does not wait for her. When she answers (or after 30 minutes "
                "without an answer) a system message arrives in the inbox saying "
                "approved, declined, or expired. On approved, a one-shot grant "
                "exists for the exact tool_name + tool_input given here, so "
                "retrying that same call goes through."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "One line: what you want to do.",
                    },
                    "why": {
                        "type": "string",
                        "description": "One line: why you need to do it.",
                    },
                    "tool_name": {
                        "type": "string",
                        "description": "The exact tool you will retry, e.g. 'Bash' or 'Read'.",
                    },
                    "tool_input": {
                        "type": "object",
                        "description": "The exact tool_input you will retry with -- must match exactly for the grant to apply.",
                    },
                },
                "required": ["action", "why", "tool_name", "tool_input"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    if name == "ask_melissa":
        return await handle_ask_melissa(arguments)
    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def main() -> None:
    # Sweep once now, then every SWEEP_INTERVAL_SECONDS, for the life of the
    # server (daemon thread: it dies with the process).
    threading.Thread(
        target=_sweep_forever, args=(SWEEP_INTERVAL_SECONDS,), daemon=True, name="press-sweeper"
    ).start()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
