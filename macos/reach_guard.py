#!/usr/bin/env python3
"""Reach guard: the PreToolUse hook that confines Clawdaddy to its workspace.

Claude Code calls this before every tool use. In order:
  0. an unlisted tool is refused outright ("tool not permitted")
  1. a hard-denied location is denied no matter what, grant or no grant
     ("never allowed")
  6. for a recursive tool (Grep/Glob/LS), a target that merely CONTAINS a
     hard-denied location is denied too -- a grant on a parent can't
     authorize walking into the secret underneath it
  2. tools with no path of their own (WebFetch, WebSearch, the inbox
     page-fetcher) always need a fresh grant
  5. Lobster's own MCP tools are always allowed
  3. anything inside ~/lobster-workspace is allowed outright, and (for the
     read-only tools Read/Glob/Grep) anything strictly inside a ~/messages
     subfolder other than inbox/config
  4. otherwise, only a matching one-shot approval lets it through

Bash is judged by the whole command string rather than by picking out
tokens (tokens are too easy to hide a path inside): it's auto-allowed only
when the working directory is inside the workspace and the command has
no way to reach outside it (see `_bash_looks_safe`); anything else falls
back to rule 4, and a hard-denied location anywhere in the command text
denies outright per rule 1.

`~` is expanded against the `home` argument this call was made with, never
against the real process `$HOME` -- the two are the same value in the real
CLI, but a test's fake `home` must not leak the real machine's paths in.

Fails closed: any exception during the CLI's decision-making prints a deny.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    from macos import approvals  # imported as part of the `macos` package (tests)
except ImportError:  # run directly, e.g. `python macos/reach_guard.py`
    import approvals

# ---------------------------------------------------------------------------
# Hard-denied locations, relative to `home`. A path under (or equal to) any
# of these is denied no matter what -- including with an unexpired grant.
# Matched case-insensitively: APFS is case-preserving but case-insensitive
# by default, so ".SSH" is the same directory as ".ssh" on disk.
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
# relative to `home`.
HARD_DENIED_GLOB_RELATIVE = (
    "Library/Group Containers/*1password*",
)

# Used by the rule-6 ancestor check for recursive tools (Grep/Glob/LS): a
# target that contains one of these is denied even with a grant, because
# recursing into it would read the secret underneath. ~/messages/inbox and
# ~/messages/config are excluded here on purpose -- reading ~/messages
# itself only needs a grant (rule 4), it isn't hard-denied; only the
# inbox/config subfolders themselves are (rule 1, checked separately).
ANCESTOR_CHECK_RELATIVE = tuple(
    e for e in HARD_DENIED_RELATIVE if e not in ("messages/inbox", "messages/config")
)

# Outside the workspace, a file whose name merely looks like a secret is
# treated as one -- catches stray credential files hard-denial didn't
# anticipate by exact location. Checked against every path component, not
# just the last one, so a directory like "my-secret-vault/notes.md" is
# still caught even though "notes.md" itself looks innocent.
SENSITIVE_NAME_SUBSTRINGS = ("credential", "secret", ".env")

PATH_KEYS = ("file_path", "path", "notebook_path")

# Glob's "pattern" and Grep's "glob" fields can smuggle a path in through a
# wildcard; the fixed portion before the first wildcard is judged like any
# other path argument.
PATTERN_KEYS = {"Glob": "pattern", "Grep": "glob"}

# Tools with no path of their own: they reach outside the machine by nature,
# so they always need a fresh grant -- there is no "inside the workspace"
# for a URL.
NO_PATH_TOOLS = ("WebFetch", "WebSearch", "mcp__lobster-inbox__fetch_page")

# Read-only tools may also read (not write) anywhere under ~/messages other
# than its hard-denied inbox/config subfolders.
MESSAGES_EXEMPT_TOOLS = ("Read", "Glob", "Grep")

# Tools that can recurse into a directory tree: a hard-denied location lying
# anywhere under their target is denied outright (rule 6).
RECURSIVE_TOOLS = ("Grep", "Glob", "LS")

# The only tools Clawdaddy is permitted to call at all. An unlisted
# tool_name -- built-in or MCP -- is refused before anything else runs.
KNOWN_TOOLS = frozenset(
    {
        "Read",
        "Write",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
        "Glob",
        "Grep",
        "LS",
        "Bash",
        "TodoWrite",
        "Task",
        "Agent",
        "WebFetch",
        "WebSearch",
    }
)

DENY_TOOL_NOT_PERMITTED = "tool not permitted"
DENY_NEVER_ALLOWED = "never allowed"
DENY_NEEDS_APPROVAL = (
    "Outside your folder. Call ask_melissa with what you want to do and why, "
    "then retry the exact same call."
)

# ---------------------------------------------------------------------------
# Bash: judged as a whole command string, not by picking out tokens.
# ---------------------------------------------------------------------------

# A "/" or "~" immediately preceded by nothing (start of string) or by
# whitespace/an operator/quote character is a path lead-in a plain word
# like "notes" or "my/relative/thing" can't produce.
_BASH_PATH_LEAD_RE = re.compile(r"""(^|[\s=@<>:'"(,;|&])(/|~)""")

_BASH_DENYLIST_WORDS = frozenset(
    {
        "python",
        "python3",
        "node",
        "ruby",
        "perl",
        "osascript",
        "sh",
        "bash",
        "zsh",
        "eval",
        "source",
        "exec",
        "env",
        "xargs",
        "find",
        "open",
        "sudo",
    }
)

# Signatures checked as plain case-insensitive substrings of the whole Bash
# command string -- this alone catches a hard-denied location written as an
# absolute path, a "~/" form, a "$HOME/" form, or its bare home-relative
# name, since all four forms contain the same distinctive substring.
_BASH_HARD_DENIED_SIGNATURES = tuple(
    sorted({e.casefold() for e in HARD_DENIED_RELATIVE} | {"lobster/config", "1password"})
)


def _bash_hard_denied(command: str) -> bool:
    lowered = command.casefold()
    return any(sig in lowered for sig in _BASH_HARD_DENIED_SIGNATURES)


def _bash_looks_safe(command: str) -> bool:
    """True only when the command can't be read as reaching outside the cwd."""
    if _BASH_PATH_LEAD_RE.search(command):
        return False
    if "$" in command or "`" in command or ".." in command:
        return False
    words = re.split(r"[\s;|&()]+", command)
    return not any(w in _BASH_DENYLIST_WORDS for w in words if w)


def _decide_bash(
    tool_input: dict, workspace: Path, resolved_cwd: Path, approvals_path: Path, now: float
) -> tuple[str, str]:
    command = tool_input.get("command", "") or ""
    if _bash_hard_denied(command):
        return "deny", DENY_NEVER_ALLOWED
    if _inside_casefold(resolved_cwd, workspace) and _bash_looks_safe(command):
        return "allow", "safe bash inside your folder"
    k = approvals.key("Bash", tool_input)
    if approvals.consume(k, approvals_path, now):
        return "allow", "one-shot approval consumed"
    return "deny", DENY_NEEDS_APPROVAL


# ---------------------------------------------------------------------------
# Tool gating and path resolution
# ---------------------------------------------------------------------------


def _tool_allowed(tool_name: str) -> bool:
    if tool_name in KNOWN_TOOLS:
        return True
    if tool_name.startswith("mcp__lobster-inbox__"):
        return True
    if tool_name.startswith("mcp__clawdaddy-approval__"):
        return True
    return False


def _own_mcp_tool(tool_name: str) -> bool:
    if tool_name == "mcp__lobster-inbox__fetch_page":
        return False
    return tool_name.startswith("mcp__lobster-inbox__") or tool_name.startswith(
        "mcp__clawdaddy-approval__"
    )


def _expand_home(raw: str, home: Path) -> Path:
    """Expand a leading "~" against `home`, never against the real $HOME."""
    if raw == "~":
        return home
    if raw.startswith("~/"):
        return home / raw[2:]
    return Path(raw)


def _resolve(raw: str, base: Path, home: Path) -> Path:
    p = _expand_home(raw, home)
    if not p.is_absolute():
        p = base / p
    try:
        return p.resolve()  # resolves symlinks, so a link out is judged by its target
    except (OSError, RuntimeError):
        return p


def _pattern_fixed_prefix(pattern: str) -> str:
    """The portion of a glob pattern before its first wildcard character."""
    cut = len(pattern)
    for ch in ("*", "?", "["):
        pos = pattern.find(ch)
        if pos != -1 and pos < cut:
            cut = pos
    return pattern[:cut]


def _gather_raw_paths(tool_input: dict) -> list[str]:
    raw = []
    for k in PATH_KEYS:
        v = tool_input.get(k)
        if isinstance(v, str) and v:
            raw.append(v)
    return raw


def _gather_pattern_candidates(
    tool_name: str, tool_input: dict, workspace: Path, home: Path
) -> list[Path]:
    field = PATTERN_KEYS.get(tool_name)
    if not field:
        return []
    raw_pattern = tool_input.get(field)
    if not isinstance(raw_pattern, str) or not raw_pattern:
        return []
    base_raw = tool_input.get("path")
    base = (
        _resolve(base_raw, workspace, home)
        if isinstance(base_raw, str) and base_raw
        else workspace
    )
    prefix = _pattern_fixed_prefix(raw_pattern)
    if not prefix:
        return []
    return [_resolve(prefix, base, home)]


# ---------------------------------------------------------------------------
# Case-insensitive containment/matching
# ---------------------------------------------------------------------------


def _relative_parts_casefold(path: Path, base: Path):
    """`path`'s parts past `base`, or None if `path` isn't (case-insensitively)
    inside `base`. An empty tuple means `path` == `base`."""
    p_parts = path.parts
    b_parts = base.parts
    if len(p_parts) < len(b_parts):
        return None
    if tuple(x.casefold() for x in p_parts[: len(b_parts)]) != tuple(
        x.casefold() for x in b_parts
    ):
        return None
    return p_parts[len(b_parts) :]


def _inside_casefold(path: Path, base: Path) -> bool:
    return _relative_parts_casefold(path, base) is not None


def _matches_relative_entry(rel_parts: tuple, entry: str) -> bool:
    entry_parts = tuple(entry.split("/"))
    n = len(entry_parts)
    if len(rel_parts) < n:
        return False
    return tuple(p.casefold() for p in rel_parts[:n]) == tuple(p.casefold() for p in entry_parts)


def _matches_glob_entry(rel_parts: tuple, pattern_parts: tuple) -> bool:
    if len(rel_parts) < len(pattern_parts):
        return False
    return all(
        fnmatch.fnmatch(part.casefold(), pat.casefold())
        for part, pat in zip(rel_parts, pattern_parts)
    )


def _inside_messages_readable(resolved: Path, messages: Path) -> bool:
    """True only for a path strictly inside a ~/messages subfolder other
    than inbox/config -- ~/messages itself is excluded (rule 4: needs a
    grant), and so is anything landing directly in inbox/config (which is
    also separately hard-denied by rule 1)."""
    rel_parts = _relative_parts_casefold(resolved, messages)
    if not rel_parts:  # None (not inside) or () (== messages itself)
        return False
    return rel_parts[0].casefold() not in ("inbox", "config")


def _is_hard_denied(resolved: Path, home: Path, workspace: Path) -> bool:
    rel_parts = _relative_parts_casefold(resolved, home)
    if rel_parts is not None:
        if any(_matches_relative_entry(rel_parts, entry) for entry in HARD_DENIED_RELATIVE):
            return True
        for pattern in HARD_DENIED_GLOB_RELATIVE:
            if _matches_glob_entry(rel_parts, tuple(pattern.split("/"))):
                return True

    if not _inside_casefold(resolved, workspace):
        if any(
            substr in part.casefold()
            for part in resolved.parts
            for substr in SENSITIVE_NAME_SUBSTRINGS
        ):
            return True

    return False


def _is_ancestor_of_denied(target: Path, home: Path) -> bool:
    """True if a hard-denied location lies at or under `target` -- i.e.
    `target` is an ancestor of (or equal to) it. Used to stop a recursive
    tool from being granted a broad target that quietly contains a secret."""
    rel_parts = _relative_parts_casefold(target, home)
    if rel_parts is None:
        # target isn't under home; the only way it's still an ancestor of
        # every denial is if home itself sits under target.
        return _inside_casefold(home, target)

    target_cf = tuple(p.casefold() for p in rel_parts)
    depth = len(target_cf)

    for entry in ANCESTOR_CHECK_RELATIVE:
        entry_cf = tuple(p.casefold() for p in entry.split("/"))
        if depth <= len(entry_cf) and entry_cf[:depth] == target_cf:
            return True

    for pattern in HARD_DENIED_GLOB_RELATIVE:
        pattern_parts = pattern.split("/")
        if depth <= len(pattern_parts) and all(
            fnmatch.fnmatch(target_cf[i], pattern_parts[i].casefold()) for i in range(depth)
        ):
            return True

    return False


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def decide(
    tool_name: str,
    tool_input: dict,
    home: Path,
    workspace: Path,
    approvals_path: Path,
    now: float,
    cwd: "str | Path | None" = None,
    messages: "str | Path | None" = None,
) -> tuple[str, str]:
    home = Path(home)
    workspace = Path(workspace)
    messages = Path(messages) if messages is not None else home / "messages"

    # Rule 0: an unlisted tool is refused outright.
    if not _tool_allowed(tool_name):
        return "deny", DENY_TOOL_NOT_PERMITTED

    if tool_name == "Bash":
        resolved_cwd = _resolve(str(cwd), workspace, home) if cwd is not None else workspace
        return _decide_bash(tool_input, workspace, resolved_cwd, approvals_path, now)

    raw_paths = _gather_raw_paths(tool_input)
    resolved_paths = [_resolve(p, workspace, home) for p in raw_paths]
    resolved_paths += _gather_pattern_candidates(tool_name, tool_input, workspace, home)

    # Rule 1: hard-denied locations always lose, grant or no grant.
    if any(_is_hard_denied(r, home, workspace) for r in resolved_paths):
        return "deny", DENY_NEVER_ALLOWED

    # Rule 6: a recursive tool whose target contains a hard-denied location
    # is denied too -- a grant on the parent can't unlock what's under it.
    if tool_name in RECURSIVE_TOOLS and any(
        _is_ancestor_of_denied(r, home) for r in resolved_paths
    ):
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
    # workspace (or, for the read-only tools, inside a ~/messages subfolder
    # other than inbox/config).
    if not resolved_paths:
        return "allow", "no path arguments"

    messages_exempt = tool_name in MESSAGES_EXEMPT_TOOLS
    outside = [
        r
        for r in resolved_paths
        if not _inside_casefold(r, workspace)
        and not (messages_exempt and _inside_messages_readable(r, messages))
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
        stdin_cwd = payload.get("cwd")

        home = Path.home()
        workspace = Path(
            os.environ.get("LOBSTER_WORKSPACE", str(home / "lobster-workspace"))
        ).expanduser()
        messages = Path(
            os.environ.get("LOBSTER_MESSAGES", str(home / "messages"))
        ).expanduser()
        approvals_path = home / ".local" / "state" / "clawdaddy" / "approvals.json"

        decision, reason = decide(
            tool_name,
            tool_input,
            home,
            workspace,
            approvals_path,
            time.time(),
            cwd=stdin_cwd,
            messages=messages,
        )
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
