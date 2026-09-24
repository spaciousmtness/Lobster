"""One-shot, time-limited approval grants for the reach guard hook.

Grants live in a small JSON file mapping a canonical request key to the
epoch second it expires at. A grant is consumed at most once, and only
while it has not expired -- this is what lets an operator approve a single
action outside the workspace without opening the door permanently.

The agent itself can never call ``grant``: nothing under its control writes
this file. Only a human-triggered approval path (outside this module) does.

Every read-modify-write against the file (both `grant` and `consume`) holds
an exclusive `flock` on a sidecar lock file for its whole critical section.
Each PreToolUse call runs as its own OS process, so without a real lock two
concurrent calls can both read the file before either writes it back --
either double-consuming one grant, or a `grant` write racing a `consume`
write and one silently clobbering the other.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

TTL = 600  # seconds a grant stays valid before it expires unused

# The one real approvals file every piece of Clawdaddy agrees on: the reach
# guard consumes grants from it, and ask_melissa (macos/approval_server.py)
# writes them here on an "approved" button press. Kept as a single public
# constant so no caller has to duplicate the path.
DEFAULT_PATH = Path.home() / ".local" / "state" / "clawdaddy" / "approvals.json"


def key(tool_name: str, tool_input: dict) -> str:
    """Canonical key for one tool call: sha256(tool_name + "\\n" + sorted json).

    Bash is keyed on its command alone: Claude Code fills in `description`
    and `timeout` itself and they vary between the ask and the retry, while
    neither changes what runs."""
    if tool_name == "Bash" and isinstance(tool_input, dict):
        tool_input = {"command": tool_input.get("command", "")}
    canonical = tool_name + "\n" + json.dumps(tool_input, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load(path: Path) -> dict:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".approvals-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def _locked(path: Path):
    """Exclusive lock on `path`'s sidecar `.lock` file, held for one
    read-modify-write cycle. `flock` is advisory but held on the open file
    description, so it serializes both threads within one process and
    separate OS processes hitting the same approvals file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.parent / (path.name + ".lock")
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def grant(key: str, path: Path) -> None:
    """Record a one-shot approval for `key`, valid for TTL seconds from now."""
    with _locked(path):
        data = _load(path)
        data[key] = time.time() + TTL
        _atomic_write(path, data)


def consume(key: str, path: Path, now: float) -> bool:
    """Consume the grant for `key` if present and unexpired.

    Returns True at most once per grant: a matched, unexpired grant is
    removed from the file as part of being consumed, whether or not it was
    expired, so a stale entry never lingers to be reused or raced.
    """
    with _locked(path):
        data = _load(path)
        expiry = data.pop(key, None)
        if expiry is None:
            return False
        if now > expiry:
            _atomic_write(path, data)
            return False
        _atomic_write(path, data)
        return True
