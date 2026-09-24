#!/bin/bash
# macos/selfcheck.sh -- post-merge health check for the merge-based updater
# (src/mcp/update_manager.py:_execute_merge_update). A non-zero exit here is
# what tells the updater to `git reset --hard` back to the pre-merge sha, so
# this script only ever REPORTS health -- it never bootstraps or loads a
# LaunchAgent itself, and it never touches git.
#
# Two phases:
#   1. Run the macOS test suite. This alone is the whole check when the
#      LaunchAgents aren't loaded (e.g. a Mac install between plist install
#      and first `launchctl bootstrap`).
#   2. If (and only if) com.lobster.router is already loaded, kickstart both
#      services on the merged code and wait for the router to come back up,
#      so a merge that passes tests but crashes on boot still rolls back.
#
# LOBSTER_SELFCHECK_CMD (tests only) replaces this whole script with a
# single command, so tests never spawn pytest-inside-pytest or touch
# launchctl.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

.venv/bin/pytest tests/macos -q
PYTEST_STATUS=$?

if [ "$PYTEST_STATUS" -ne 0 ]; then
    exit "$PYTEST_STATUS"
fi

UID_NUM="$(id -u)"

if ! launchctl print "gui/$UID_NUM/com.lobster.router" >/dev/null 2>&1; then
    # Not loaded -- nothing running to restart or verify. The pytest result
    # above is the whole check.
    exit 0
fi

launchctl kickstart -k "gui/$UID_NUM/com.lobster.router"
launchctl kickstart -k "gui/$UID_NUM/com.lobster.claude"

DEADLINE=$(( $(date +%s) + 60 ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    if launchctl print "gui/$UID_NUM/com.lobster.router" 2>/dev/null | grep -q "state = running"; then
        exit 0
    fi
    sleep 1
done

echo "selfcheck: com.lobster.router did not reach state = running within 60s" >&2
exit 1
