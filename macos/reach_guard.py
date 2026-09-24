#!/usr/bin/env python3
"""Reach guard: the PreToolUse hook that confines Clawdaddy to its workspace.

Claude Code calls this before every tool use. In order (non-Bash tools):
  0. an unlisted tool is refused outright ("tool not permitted")
  C3/I5. a `..` component, or a `{...}` brace group containing "/" or "~",
      anywhere in a Glob pattern or Grep glob denies outright, grant or no
      grant
  1. a hard-denied location is denied no matter what, grant or no grant
     ("never allowed") -- checked against `home` AND against the `messages`
     argument (inbox/config under either)
  6. for a recursive tool (Grep/Glob/LS), a target that merely CONTAINS a
     hard-denied location (relative to home OR relative to messages -- so
     ~/messages itself, or a custom/symlinked messages root, is covered
     too) is denied too -- a grant on a parent can't authorize walking into
     the secret underneath it. A missing "path" is judged as if it were the
     resolved cwd.
  2. tools with no path of their own (WebFetch, WebSearch, the inbox
     page-fetcher) always need a fresh grant
  5. Lobster's own MCP tools are always allowed
  3. anything inside ~/lobster-workspace is allowed outright, and (for the
     read-only tools Read/Glob/Grep) anything strictly inside a ~/messages
     subfolder other than inbox/config
  4. otherwise, only a matching one-shot approval lets it through

Bash (fix round 3, literal-only policy): denied outright, reason "never
allowed: command must be literal", if the command contains any of
`* ? [ { $ \\` ' "`, a ".." path component, the word cd/pushd/popd, or if
the stdin cwd doesn't resolve inside the workspace -- no exceptions, no
grant helps. Otherwise the command is literal: every whitespace/operator-
split token, AND every piece of it split further on `@ : , +` (round 3
fix-up -- catches curl's `@path`, rsync's `host:path`, and a `,`/`+`-joined
argument gluing a path to something else), is resolved as a path (relative
ones against cwd, a leading "~" against home) and judged with the same
hard-deny check a file tool's path argument gets; any hit denies outright,
no hits falls to the ordinary one-shot grant. There is no wildcard/quoting
obfuscation scan any more -- banning the characters that make one possible
is simpler and cheaper than trying to out-guess them.

Every relative path argument (and a Glob/Grep pattern's fixed base) resolves
against the stdin `cwd`, not the workspace -- a Bash-adjacent tool call
genuinely runs relative to wherever the agent's shell currently is. A
Grep/Glob/LS call with no `path` argument at all is judged as though
`path` were that same cwd.

`~` is expanded against the `home` argument this call was made with, never
against the real process `$HOME`. `home`, `workspace` and `messages` are
each `.resolve()`d once at the top of `decide()`, so a symlinked `home`
(or workspace, or messages) is judged by what it actually points at, the
same way every individual path argument already is.

Fails closed: any exception during the CLI's decision-making prints a deny.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import stat
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

# messages/inbox and messages/config, but relative to the `messages`
# argument rather than to `home` -- covers a custom LOBSTER_MESSAGES
# location, and a symlinked one, neither of which the home-relative entries
# above can see.
MESSAGES_HARD_DENIED_SUBFOLDERS = ("inbox", "config")

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
# anywhere under their target is denied outright (rule 6), and a missing
# "path" argument is judged as if it were the resolved cwd.
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
DENY_BASH_MUST_BE_LITERAL = "never allowed: command must be literal"
DENY_NEEDS_APPROVAL = (
    "Outside your folder. Call ask_melissa with what you want to do and why, "
    "then retry the exact same call."
)

# ---------------------------------------------------------------------------
# Bash (fix round 3): literal-only policy. No wildcard/quote scanning -- a
# command containing any character or word that could hide or construct a
# path is refused outright, so the only commands that ever reach path
# resolution are ones where "the tokens are exactly what they look like" is
# actually true.
# ---------------------------------------------------------------------------

_BASH_BANNED_CHARS = frozenset("*?[{$`\\'\"")
# Round 4: ln/link/pax can build a same-command symlink or hard link out of
# the workspace, which no string check can follow.
_BASH_BANNED_WORDS = frozenset({"cd", "pushd", "popd", "ln", "link", "pax"})

# Round 4: a `file:` URL or a `%`-encoded byte lets curl & co. name a path
# the tokenizer never sees literally.
_BASH_BANNED_SUBSTRINGS = ("file:", "%")

# Round 4: `~user`, `~+`, `~-` expand to paths we don't model; only a bare
# `~` or `~/` is judged.
_BASH_TILDE_FORM_RE = re.compile(r"~(?!/|$)")

# Tokenizing splits on whitespace and these operators -- NOT on "/", so a
# path's internal structure survives intact in one token.
_BASH_SPLIT_RE = re.compile(r"[\s;&|<>()=,]+")

# Only used to detect a ".." *component*: splits on "/" too, so
# "../.ssh" or "foo/../bar" actually isolates the ".." between slashes.
_BASH_COMPONENT_SPLIT_RE = re.compile(r"[\s;&|<>()=,/]+")


def _bash_has_banned_chars(command: str) -> bool:
    return any(c in _BASH_BANNED_CHARS for c in command)


def _bash_has_dotdot(command: str) -> bool:
    return any(part == ".." for part in _BASH_COMPONENT_SPLIT_RE.split(command))


def _bash_has_banned_word(command: str) -> bool:
    # Judged by basename too, so `/bin/ln` is `ln`.
    words = _BASH_SPLIT_RE.split(command)
    return any(w.rsplit("/", 1)[-1] in _BASH_BANNED_WORDS for w in words if w)


def _bash_has_banned_substring(command: str) -> bool:
    folded = command.casefold()
    return any(s in folded for s in _BASH_BANNED_SUBSTRINGS)


def _bash_has_tilde_form(command: str) -> bool:
    return any(_BASH_TILDE_FORM_RE.search(t) for t in _bash_tokenize(command))


def _bash_must_be_literal_violation(command: str) -> bool:
    return (
        _bash_has_banned_chars(command)
        or _bash_has_dotdot(command)
        or _bash_has_banned_word(command)
        or _bash_has_banned_substring(command)
        or _bash_has_tilde_form(command)
    )


def _bash_tokenize(command: str) -> list[str]:
    return [t for t in _BASH_SPLIT_RE.split(command) if t]


# A token surviving the first split can still carry a path glued to
# something else via a convention the first split doesn't know about:
# curl's `@path` (read-this-file), rsync's `host:path`, a bare `,`-joined
# tar argument, a `+`-joined one. `--flag=value` is already handled by the
# first split (`=` is one of its operators), so a token like
# `--data-binary=@path` already arrives here as two separate tokens
# (`--data-binary` and `@path`) before this second split ever runs.
_BASH_SUBTOKEN_SPLIT_RE = re.compile(r"[@:,+]+")


def _bash_subtokens(token: str) -> list[str]:
    return [t for t in _BASH_SUBTOKEN_SPLIT_RE.split(token) if t]


def _bash_pieces(token: str) -> list[str]:
    """The token, its @:,+ subtokens, and (round 4) every substring of the
    token that starts at a "/" or "~" -- catches a path glued to a short
    option (`-T/path`, `-o/path`, `-C/path`)."""
    pieces = [token]
    pieces.extend(_bash_subtokens(token))
    pieces.extend(token[i:] for i, c in enumerate(token) if c in "/~" and i > 0)
    return pieces


def _decide_bash(
    tool_input: dict,
    home: Path,
    workspace: Path,
    resolved_cwd: Path,
    messages: Path,
    approvals_path: Path,
    now: float,
) -> tuple[str, str]:
    command = tool_input.get("command", "") or ""

    if not _inside_casefold(resolved_cwd, workspace) or _bash_must_be_literal_violation(command):
        return "deny", DENY_BASH_MUST_BE_LITERAL

    for token in _bash_tokenize(command):
        for piece in _bash_pieces(token):
            resolved = _resolve(piece, resolved_cwd, home)
            if (
                _is_hard_denied(resolved, home, workspace, messages)
                # Round 4: naming a folder that contains a secret (home,
                # ~/.local, ~/messages, "/", ...) lets any recursive command
                # walk into it, so it is refused like a recursive tool's target.
                or _is_ancestor_of_denied(resolved, home)
                or _is_ancestor_of_messages_denied(resolved, messages)
                or _is_hard_link(resolved)
            ):
                return "deny", DENY_NEVER_ALLOWED

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


_FIRMLINK_PARTS = ("/", "system", "volumes", "data")


def _strip_firmlink(p: Path) -> Path:
    """/System/Volumes/Data/<x> is the same file as /<x> on macOS (the data
    volume's firmlink), and resolve() does not undo it -- so strip it,
    string-level, before any check sees the path."""
    parts = p.parts
    if tuple(x.casefold() for x in parts[:4]) == _FIRMLINK_PARTS:
        return Path("/", *parts[4:])
    return p


def _resolve(raw: str, base: Path, home: Path) -> Path:
    p = _expand_home(raw, home)
    if not p.is_absolute():
        p = base / p
    try:
        p = p.resolve()  # resolves symlinks, so a link out is judged by its target
    except (OSError, RuntimeError):
        pass
    return _strip_firmlink(p)


def _is_hard_link(p: Path) -> bool:
    """A regular file with more than one link may be the same inode as a
    secret elsewhere; path checks can't see that, so refuse it."""
    try:
        st = p.stat()
    except (OSError, ValueError):
        return False
    return stat.S_ISREG(st.st_mode) and st.st_nlink > 1


def _pattern_fixed_prefix(pattern: str) -> str:
    """The complete-directory portion of a glob pattern before its first
    wildcard segment. Cut at the last "/" before the first wildcard
    character rather than mid-segment, so a partial name like ".S" cut out
    of ".S*/*" can never be mistaken for a safe, fully-specified path
    component -- the boundary lands on the last directory we can actually
    be sure of. "{" counts as a wildcard here too: a pattern that opens a
    brace group in its very first segment (e.g. "{<home>/.ssh,x}/*") has no
    fixed prefix at all, which is why `_pattern_has_risky_brace` below is a
    separate, independent check rather than relying on this cut alone."""
    cut = len(pattern)
    for ch in ("*", "?", "[", "{"):
        pos = pattern.find(ch)
        if pos != -1 and pos < cut:
            cut = pos
    if cut == len(pattern):
        return pattern  # no wildcard at all
    last_slash = pattern.rfind("/", 0, cut)
    if last_slash == -1:
        return ""  # wildcard in the very first segment -- nothing fixed to judge
    return pattern[: last_slash + 1]


def _pattern_has_dotdot(pattern: str) -> bool:
    return any(part == ".." for part in pattern.split("/"))


_PATTERN_BRACE_RE = re.compile(r"\{([^{}]*)\}")


def _pattern_has_risky_brace(pattern: str) -> bool:
    """True if any `{...}` brace group in the pattern contains "/" or "~" --
    e.g. `{<home>/.ssh,x}/*` -- which can smuggle an absolute or home-
    relative path in where the fixed-prefix cut can't see it (a brace
    group at the very start leaves no fixed prefix at all)."""
    return any(
        "/" in group.group(1) or "~" in group.group(1)
        for group in _PATTERN_BRACE_RE.finditer(pattern)
    )


def _gather_raw_paths(tool_input: dict) -> list[str]:
    raw = []
    for k in PATH_KEYS:
        v = tool_input.get(k)
        if isinstance(v, str) and v:
            raw.append(v)
    return raw


def _gather_pattern_candidates(
    tool_name: str, tool_input: dict, base_dir: Path, home: Path
) -> list[Path]:
    field = PATTERN_KEYS.get(tool_name)
    if not field:
        return []
    raw_pattern = tool_input.get(field)
    if not isinstance(raw_pattern, str) or not raw_pattern:
        return []
    base_raw = tool_input.get("path")
    base = (
        _resolve(base_raw, base_dir, home)
        if isinstance(base_raw, str) and base_raw
        else base_dir
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


def _is_hard_denied(resolved: Path, home: Path, workspace: Path, messages: Path) -> bool:
    rel_parts = _relative_parts_casefold(resolved, home)
    if rel_parts is not None:
        if any(_matches_relative_entry(rel_parts, entry) for entry in HARD_DENIED_RELATIVE):
            return True
        for pattern in HARD_DENIED_GLOB_RELATIVE:
            if _matches_glob_entry(rel_parts, tuple(pattern.split("/"))):
                return True

    # inbox/config are hard-denied relative to the `messages` argument too,
    # not only when messages happens to live at home/"messages".
    messages_rel = _relative_parts_casefold(resolved, messages)
    if messages_rel and messages_rel[0].casefold() in ("inbox", "config"):
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
    """True if a hard-denied location (relative to `home`) lies at or under
    `target` -- i.e. `target` is an ancestor of (or equal to) it. Used to
    stop a recursive tool from being granted a broad target that quietly
    contains a secret -- this includes ~/messages itself, since
    messages/inbox and messages/config sit under it."""
    rel_parts = _relative_parts_casefold(target, home)
    if rel_parts is None:
        # target isn't under home; the only way it's still an ancestor of
        # every denial is if home itself sits under target.
        return _inside_casefold(home, target)

    target_cf = tuple(p.casefold() for p in rel_parts)
    depth = len(target_cf)

    for entry in HARD_DENIED_RELATIVE:
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


def _is_ancestor_of_messages_denied(target: Path, messages: Path) -> bool:
    """True if `target` is at or above `<messages>/inbox` or
    `<messages>/config` -- the `messages`-relative counterpart of
    `_is_ancestor_of_denied`. Needed separately because `messages` can live
    outside `home` entirely (a custom LOBSTER_MESSAGES) or reach its real
    location through a symlink (~/messages -> elsewhere) -- either way
    `messages` itself was already `.resolve()`d at the top of `decide()`,
    so this only has to compare against it directly."""
    rel_parts = _relative_parts_casefold(target, messages)
    if rel_parts is None:
        # target isn't under messages; still an ancestor of both subfolders
        # if messages itself sits under target.
        return _inside_casefold(messages, target)
    depth = len(rel_parts)
    if depth == 0:
        return True  # target IS messages -- both subfolders sit right under it
    if depth == 1 and rel_parts[0].casefold() in MESSAGES_HARD_DENIED_SUBFOLDERS:
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
    home = _strip_firmlink(Path(home).resolve())
    workspace = _strip_firmlink(Path(workspace).resolve())
    messages = _strip_firmlink(
        Path(messages).resolve() if messages is not None else (home / "messages").resolve()
    )

    # Rule 0: an unlisted tool is refused outright.
    if not _tool_allowed(tool_name):
        return "deny", DENY_TOOL_NOT_PERMITTED

    resolved_cwd = _resolve(str(cwd), workspace, home) if cwd is not None else workspace

    if tool_name == "Bash":
        return _decide_bash(tool_input, home, workspace, resolved_cwd, messages, approvals_path, now)

    raw_paths = _gather_raw_paths(tool_input)
    resolved_paths = [_resolve(p, resolved_cwd, home) for p in raw_paths]
    resolved_paths += _gather_pattern_candidates(tool_name, tool_input, resolved_cwd, home)

    # A Grep/Glob/LS call with no explicit "path" is judged as though it
    # targeted the cwd it's actually going to run relative to.
    if tool_name in RECURSIVE_TOOLS and not tool_input.get("path"):
        resolved_paths.append(resolved_cwd)

    # Rule (C3/I5): a ".." component, or a "{...}" brace group smuggling a
    # "/" or "~" inside it, in a Glob pattern or Grep glob is denied
    # outright, grant or no grant -- resolution alone isn't trusted here.
    pattern_field = PATTERN_KEYS.get(tool_name)
    if pattern_field:
        raw_pattern = tool_input.get(pattern_field)
        if isinstance(raw_pattern, str) and (
            _pattern_has_dotdot(raw_pattern) or _pattern_has_risky_brace(raw_pattern)
        ):
            return "deny", DENY_NEVER_ALLOWED

    # Rule 1: hard-denied locations always lose, grant or no grant.
    if any(_is_hard_denied(r, home, workspace, messages) for r in resolved_paths):
        return "deny", DENY_NEVER_ALLOWED

    # Round 4: a hard-linked regular file may be a secret under another name.
    if any(_is_hard_link(r) for r in resolved_paths):
        return "deny", DENY_NEVER_ALLOWED

    # Rule 6: a recursive tool whose target contains a hard-denied location
    # is denied too -- a grant on the parent can't unlock what's under it.
    # Checked relative to home AND relative to messages (a custom or
    # symlinked messages root has no useful home-relative representation).
    if tool_name in RECURSIVE_TOOLS and any(
        _is_ancestor_of_denied(r, home) or _is_ancestor_of_messages_denied(r, messages)
        for r in resolved_paths
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
