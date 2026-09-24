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
import json
import os
import secrets
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from macos import approvals  # imported as part of the `macos` package (tests)
except ImportError:  # run directly, e.g. `python macos/approval_server.py`
    import approvals

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# How long a single ask_melissa call is allowed to block, at most, and how
# often wait_for_answer re-scans the inbox while waiting.
MAX_TIMEOUT_SECONDS = 1800
DEFAULT_TIMEOUT_SECONDS = 1800
POLL_INTERVAL_SECONDS = 1.0

# A tool-call rendering (tool_name + tool_input, non-Bash) longer than this
# is truncated -- Bash is exempted below, Melissa always sees the full
# command she is being asked to approve.
RENDER_TRUNCATE_CHARS = 300

MESSAGES_DIR = Path(os.environ.get("LOBSTER_MESSAGES", str(Path.home() / "messages"))).expanduser()
INBOX_DIR = MESSAGES_DIR / "inbox"
OUTBOX_DIR = MESSAGES_DIR / "outbox"
PROCESSED_DIR = MESSAGES_DIR / "processed"
SENT_DIR = MESSAGES_DIR / "sent"


# ---------------------------------------------------------------------------
# Pure functions (tested directly, no filesystem side effects beyond what
# their own arguments name)
# ---------------------------------------------------------------------------


def _render_call(tool_name: str, tool_input: dict) -> str:
    """One-line rendering of a tool call for the outbox message.

    Bash gets the full command, untruncated -- Melissa is being asked to
    approve exactly that command and needs to see all of it. Everything else
    is rendered as `tool_name(json)`, truncated to ~300 chars.
    """
    if tool_name == "Bash":
        return f"Bash: {tool_input.get('command', '')}"
    try:
        rendered = json.dumps(tool_input, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        rendered = str(tool_input)
    one_line = f"{tool_name}({rendered})"
    if len(one_line) > RENDER_TRUNCATE_CHARS:
        one_line = one_line[: RENDER_TRUNCATE_CHARS - 3] + "..."
    return one_line


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
        lines.append(f"Call: {_render_call(tool_name, tool_input or {})}")
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
) -> str:
    """Block until Melissa presses Yes/No for `request_id`, or `deadline` passes.

    Scans `inbox` sorted by name, and (see docs/approval-server-step5.md --
    the reach guard's own `wait_for_messages` never claims a message, but
    Clawdaddy's own dispatcher might have, via `mark_processing`/
    `claim_and_ack`, before this function's next poll) the sibling
    `processing/` directory Lobster's dispatcher can move a claimed message
    into. Only a button press from `owner_id`, for exactly this
    `request_id`, is consumed -- everything else (a stranger's press, a
    press for a different request, an ordinary text message) is left
    untouched for the dispatcher to handle normally. The matched file is
    moved into `processed` so it is never seen twice.
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
                if text == approve_text:
                    _claim(f, processed)
                    return "approved"
                if text == deny_text:
                    _claim(f, processed)
                    return "denied"

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

    request_id = secrets.token_hex(4)
    ts_ms = int(time.time() * 1000)
    msg = build_request(owner_id, request_id, action, why, tool_name, tool_input, ts_ms=ts_ms)
    _write_outbox_message(ts_ms, msg)

    deadline = time.time() + timeout_seconds
    loop = asyncio.get_running_loop()
    verdict = await loop.run_in_executor(
        None,
        wait_for_answer,
        INBOX_DIR,
        PROCESSED_DIR,
        request_id,
        owner_id,
        deadline,
        POLL_INTERVAL_SECONDS,
    )

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
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
