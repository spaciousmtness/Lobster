#!/usr/bin/env python3
"""Reach guard: the PreToolUse hook that confines Clawdaddy to its workspace.

Claude Code calls this before every tool use. It:
  - never allows a secret location, grant or no grant ("never allowed")
  - always requires a fresh one-shot grant for tools with no path of their
    own (WebFetch, WebSearch, the inbox page-fetcher)
  - allows anything inside ~/lobster-workspace outright, and (for the
    read-only tools Read/Glob/Grep) anything inside ~/messages except its
    inbox and config subfolders
  - otherwise allows only by consuming a matching one-shot approval
  - always allows Lobster's own MCP tools

Fails closed: any exception during the CLI's decision-making prints a deny.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

try:
    from macos import approvals  # imported as part of the `macos` package (tests)
except ImportError:  # run directly, e.g. `python macos/reach_guard.py`
    import approvals

# ---------------------------------------------------------------------------
# Hard-denied locations, relative to $HOME. A path under (or equal to) any of
# these is denied no matter what -- including with an unexpired grant.
# ---------------------------------------------------------------------------
HARD_DENIED_RELATIVE = (
    ".ssh",
    ".gnupg",
    "Library/Keychains",
    "Library/Application Support/1Password",
    ".config/clawdaddy",
    ".config/openclaw",
    ".claude/.credentials.json",
    ".freedom",
    "lobster/config/config.env",
    ".local/state/clawdaddy",
    "messages/inbox",
    "messages/config",
)

# Case-insensitive glob, matched component by component against the path
# relative to $HOME.
HARD_DENIED_GLOB_RELATIVE = (
    "Library/Group Containers/*1password*",
)

# Outside the workspace, a file whose name merely looks like a secret is
# treated as one -- catches stray credential files hard-denial didn't
# anticipate by exact location.
SENSITIVE_NAME_SUBSTRINGS = ("credential", "secret", ".env")

PATH_KEYS = ("file_path", "path", "notebook_path")

# Tools with no path of their own: they reach outside the machine by nature,
# so they always need a fresh grant -- there is no "inside the workspace"
# for a URL.
NO_PATH_TOOLS = ("WebFetch", "WebSearch", "mcp__lobster-inbox__fetch_page")

# Read-only tools may also read (not write) anywhere under ~/messages other
# than its hard-denied inbox/config subfolders.
MESSAGES_EXEMPT_TOOLS = ("Read", "Glob", "Grep")

DENY_NEVER_ALLOWED = "never allowed"
DENY_NEEDS_APPROVAL = (
    "Outside your folder. Call ask_melissa with what you want to do and why, "
    "then retry the exact same call."
)


def _own_mcp_tool(tool_name: str) -> bool:
    if tool_name == "mcp__lobster-inbox__fetch_page":
        return False
    return tool_name.startswith("mcp__lobster-inbox__") or tool_name.startswith(
        "mcp__clawdaddy-approval__"
    )


def _bash_path_tokens(command: str) -> list[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    candidates = []
    for tok in tokens:
        candidate = tok
        # Strip a leading redirection like ">", ">>", "2>", "2>>", "<".
        m = re.match(r"^\d*(>>?|<)(.*)$", candidate)
        if m:
            candidate = m.group(2)
        # Strip a leading `KEY=` env-style assignment.
        m = re.match(r"^[A-Za-z_][A-Za-z0-9_]*=(.*)$", candidate)
        if m:
            candidate = m.group(1)
        # Relative tokens are never treated as outside paths.
        if candidate.startswith("/") or candidate.startswith("~"):
            candidates.append(candidate)
    return candidates


def _gather_raw_paths(tool_name: str, tool_input: dict) -> list[str]:
    if tool_name == "Bash":
        return _bash_path_tokens(tool_input.get("command", "") or "")
    raw = []
    for k in PATH_KEYS:
        v = tool_input.get(k)
        if isinstance(v, str) and v:
            raw.append(v)
    return raw


def _resolve(raw: str, workspace: Path) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = workspace / p
    try:
        return p.resolve()  # resolves symlinks, so a link out is judged by its target
    except (OSError, RuntimeError):
        return p


def _matches_relative_entry(rel_posix: str, entry: str) -> bool:
    return rel_posix == entry or rel_posix.startswith(entry + "/")


def _matches_glob_entry(rel_parts: tuple, pattern_parts: tuple) -> bool:
    if len(rel_parts) < len(pattern_parts):
        return False
    return all(
        fnmatch.fnmatch(part.lower(), pat.lower())
        for part, pat in zip(rel_parts, pattern_parts)
    )


def _inside(resolved: Path, base: Path) -> bool:
    try:
        resolved.relative_to(base)
        return True
    except ValueError:
        return False


def _is_hard_denied(resolved: Path, home: Path, workspace: Path) -> bool:
    try:
        rel = resolved.relative_to(home)
    except ValueError:
        rel = None
    if rel is not None:
        rel_posix = rel.as_posix()
        if any(_matches_relative_entry(rel_posix, entry) for entry in HARD_DENIED_RELATIVE):
            return True
        for pattern in HARD_DENIED_GLOB_RELATIVE:
            if _matches_glob_entry(rel.parts, tuple(pattern.split("/"))):
                return True

    if not _inside(resolved, workspace):
        name_lower = resolved.name.lower()
        if any(s in name_lower for s in SENSITIVE_NAME_SUBSTRINGS):
            return True

    return False


def decide(
    tool_name: str,
    tool_input: dict,
    home: Path,
    workspace: Path,
    approvals_path: Path,
    now: float,
) -> tuple[str, str]:
    home = Path(home)
    workspace = Path(workspace)
    messages = home / "messages"

    raw_paths = _gather_raw_paths(tool_name, tool_input)
    resolved_paths = [_resolve(p, workspace) for p in raw_paths]

    # Rule 1: hard-denied locations always lose, grant or no grant.
    if any(_is_hard_denied(r, home, workspace) for r in resolved_paths):
        return "deny", DENY_NEVER_ALLOWED

    # Rule 2: tools with no path of their own always need a fresh grant.
    if tool_name in NO_PATH_TOOLS:
        k = approvals.key(tool_name, tool_input)
        if approvals.consume(k, approvals_path, now):
            return "allow", "one-shot approval consumed"
        return "deny", DENY_NEEDS_APPROVAL

    # Rule 5: Lobster's own MCP tools are always allowed.
    if _own_mcp_tool(tool_name):
        return "allow", "lobster's own tool"

    # Rule 3: nothing to restrict, or everything resolves inside the
    # workspace (or, for the read-only tools, inside ~/messages).
    if not resolved_paths:
        return "allow", "no path arguments"

    messages_exempt = tool_name in MESSAGES_EXEMPT_TOOLS
    outside = [
        r
        for r in resolved_paths
        if not _inside(r, workspace) and not (messages_exempt and _inside(r, messages))
    ]
    if not outside:
        return "allow", "inside your folder"

    # Rule 4: otherwise, only a one-shot approval gets it through.
    k = approvals.key(tool_name, tool_input)
    if approvals.consume(k, approvals_path, now):
        return "allow", "one-shot approval consumed"
    return "deny", DENY_NEEDS_APPROVAL


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        tool_name = payload.get("tool_name", "")
        tool_input = payload.get("tool_input") or {}

        home = Path.home()
        workspace = Path(
            os.environ.get("LOBSTER_WORKSPACE", str(home / "lobster-workspace"))
        ).expanduser()
        # LOBSTER_MESSAGES is read here for parity with the rest of Lobster's
        # tooling, but decide() always derives the messages directory as
        # home/"messages" (it takes no messages parameter), so this value is
        # not otherwise consulted.
        os.environ.get("LOBSTER_MESSAGES", str(home / "messages"))
        approvals_path = home / ".local" / "state" / "clawdaddy" / "approvals.json"

        decision, reason = decide(tool_name, tool_input, home, workspace, approvals_path, time.time())
    except Exception as exc:  # fail closed
        decision, reason = "deny", f"reach guard failed closed: {exc}"

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


if __name__ == "__main__":
    main()
