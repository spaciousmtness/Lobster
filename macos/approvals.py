"""One-shot, time-limited approval grants for the reach guard hook.

Grants live in a small JSON file mapping a canonical request key to the
epoch second it expires at. A grant is consumed at most once, and only
while it has not expired -- this is what lets an operator approve a single
action outside the workspace without opening the door permanently.

The agent itself can never call ``grant``: nothing under its control writes
this file. Only a human-triggered approval path (outside this module) does.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

TTL = 600  # seconds a grant stays valid before it expires unused


def key(tool_name: str, tool_input: dict) -> str:
    """Canonical key for one tool call: sha256(tool_name + "\\n" + sorted json)."""
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


def grant(key: str, path: Path) -> None:
    """Record a one-shot approval for `key`, valid for TTL seconds from now."""
    data = _load(path)
    data[key] = time.time() + TTL
    _atomic_write(path, data)


def consume(key: str, path: Path, now: float) -> bool:
    """Consume the grant for `key` if present and unexpired.

    Returns True at most once per grant: a matched, unexpired grant is
    removed from the file as part of being consumed, whether or not it was
    expired, so a stale entry never lingers to be reused or raced.
    """
    data = _load(path)
    expiry = data.pop(key, None)
    if expiry is None:
        return False
    if now > expiry:
        _atomic_write(path, data)
        return False
    _atomic_write(path, data)
    return True
