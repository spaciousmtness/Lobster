#!/usr/bin/env python3
"""ask_melissa: the MCP server the agent calls when the reach guard refuses.

When `reach_guard.py` denies a tool call for lacking a one-shot approval, the
agent is told to call `ask_melissa` with what it wants to do and why, then
retry the exact same call. This server is that tool: it texts Melissa a
Yes/No button through Lobster's outbox, blocks until she presses one (or the
timeout expires), and on "approved" records a one-shot grant in the same
`~/.local/state/clawdaddy/approvals.json` file the reach guard consumes from
-- so the agent's retry of the exact same call goes through.

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

# How long a single ask_melissa call is allowed to block, at most, and how
# often wait_for_answer re-scans the inbox while waiting.
MAX_TIMEOUT_SECONDS = 1800
DEFAULT_TIMEOUT_SECONDS = 1800
POLL_INTERVAL_SECONDS = 1.0

# The non-path remainder of a tool call's rendering is truncated to this
# many chars. The path (or Bash command) itself is never truncated: Melissa
# always sees exactly where a call reaches.
RENDER_TRUNCATE_CHARS = 300

# How much of a Write's content / an Edit's new text the approval shows.
PREVIEW_LINES = 5
PREVIEW_LINE_CHARS = 120

# Pending request ids (id -> deadline epoch), shared by every
# approval_server process. Lives beside approvals.json, which the agent can
# neither read nor write (reach guard hard-deny + sandbox).
PENDING_PATH = approvals.DEFAULT_PATH.parent / "pending-approvals.json"

# How often the stray-press sweeper runs, after one sweep at startup.
SWEEP_INTERVAL_SECONDS = 15.0

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
    lines = [f"Clawdaddy wants to: {action}", f"Why: {why}"]
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
        "buttons": [
            [
                {"text": "Yes", "callback_data": f"approve:{request_id}"},
                {"text": "No", "callback_data": f"deny:{request_id}"},
            ]
        ],
    }


def wait_for_answer(
    inbox: Path,
    processed: Path,
    request_id: str,
    owner_id: int,
    deadline: float,
    poll: float,
    expected_text: str | None = None,
) -> str:
    """Block until Melissa presses Yes/No for `request_id`, or `deadline` passes.

    Scans `inbox` sorted by name, and the sibling `processing/` directory
    too: the dispatcher is told to leave button presses alone, but it may
    still have claimed one with `mark_processing` before this function's
    next poll. Only a button press from `owner_id`, for exactly this
    `request_id`, is consumed -- everything else (a stranger's press, a
    press for a different request, an ordinary text message) is left
    untouched for the dispatcher to handle normally. The matched file is
    moved into `processed` so it is never seen twice.

    With `expected_text`, a press also has to come from the message this
    request sent: the bot records the pressed message's text as
    `original_message_text`, and it must contain `expected_text` (compared
    whitespace-normalized). A press whose message doesn't match -- a button
    the agent sent itself with a guessed or leaked id -- is moved to
    `processed` and ignored.
    """
    approve_text = f"[Button pressed: approve:{request_id}]"
    deny_text = f"[Button pressed: deny:{request_id}]"
    processing = inbox.parent / "processing"

    while True:
        for base in (inbox, processing):
            if not base.is_dir():
                continue
            for f in sorted(base.glob("*.json")):
                try:
                    msg = json.loads(f.read_text())
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("source") != "telegram" or msg.get("user_id") != owner_id:
                    continue
                text = msg.get("text")
                if text not in (approve_text, deny_text):
                    continue
                if expected_text is not None and _norm_ws(expected_text) not in _norm_ws(
                    str(msg.get("original_message_text") or "")
                ):
                    _claim(f, processed)
                    continue
                _claim(f, processed)
                return "approved" if text == approve_text else "denied"

        if time.time() >= deadline:
            return "timed out"
        time.sleep(poll)


def _claim(matched: Path, processed: Path) -> None:
    """Move a matched button-press file into `processed`, best-effort."""
    try:
        processed.mkdir(parents=True, exist_ok=True, mode=0o700)
        matched.rename(processed / matched.name)
    except OSError:
        pass  # already moved/consumed by someone else -- the answer still stands


# ---------------------------------------------------------------------------
# Pending ids and the stray-press sweeper (I1). The dispatcher is told never
# to mark a button press processed, so a press nobody is waiting for (late,
# duplicate, or for a request that already timed out) would sit in the inbox
# forever and make wait_for_messages return at once, every time.
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


def add_pending(request_id: str, deadline: float, path: Path) -> None:
    with _pending_locked(path):
        data = _load_pending(path)
        data[request_id] = deadline
        _atomic_write(path, data)


def remove_pending(request_id: str, path: Path) -> None:
    with _pending_locked(path):
        data = _load_pending(path)
        if data.pop(request_id, None) is not None:
            _atomic_write(path, data)


def pending_ids(path: Path, now: float) -> set[str]:
    with _pending_locked(path):
        data = _load_pending(path)
    return {k for k, v in data.items() if isinstance(v, (int, float)) and v >= now}


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


def _sweep_forever(interval: float) -> None:
    while True:
        try:
            sweep_stray_presses(INBOX_DIR, PROCESSED_DIR, PENDING_PATH, time.time())
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

    try:
        timeout_seconds = float(args.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    timeout_seconds = max(0.0, min(MAX_TIMEOUT_SECONDS, timeout_seconds))

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

    deadline = time.time() + timeout_seconds
    add_pending(request_id, deadline, PENDING_PATH)
    try:
        _write_outbox_message(ts_ms, msg)
        loop = asyncio.get_running_loop()
        verdict = await loop.run_in_executor(
            None,
            lambda: wait_for_answer(
                INBOX_DIR,
                PROCESSED_DIR,
                request_id,
                owner_id,
                deadline,
                POLL_INTERVAL_SECONDS,
                expected_text=expected_press_text(tool_name, tool_input, action),
            ),
        )
    finally:
        remove_pending(request_id, PENDING_PATH)

    if verdict == "approved":
        approvals.grant(approvals.key(tool_name, tool_input), approvals.DEFAULT_PATH)

    return [TextContent(type="text", text=verdict)]


server = Server("clawdaddy-approval")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="ask_melissa",
            description=(
                "Ask Melissa to approve one specific tool call the reach guard just "
                "refused. Texts her Yes/No buttons on Telegram and blocks (up to "
                "timeout_seconds, default and max 1800) until she answers. On "
                "'approved', records a one-shot grant for the exact tool_name + "
                "tool_input given here, so retrying that same call goes through. "
                "Returns 'approved', 'denied', or 'timed out'."
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
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Max seconds to wait for Melissa's answer. Default and max 1800.",
                        "default": DEFAULT_TIMEOUT_SECONDS,
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
