#!/bin/bash
# macos/selfcheck.sh -- post-merge health check for the merge-based updater
# (src/mcp/update_manager.py:_execute_merge_update). Its exit code is what
# tells the updater whether to keep the merge or `git reset --hard` back to
# the pre-merge sha, so this script only ever REPORTS health -- it never
# touches git, and it never bootstraps or loads a LaunchAgent.
#
# IMPORTANT: this script must NEVER kickstart com.lobster.claude.
# com.lobster.claude is the dispatcher that called this update in the first
# place; restarting it before update_manager.py has recorded an outcome and
# decided rollback-or-keep would kill the very process about to read that
# decision, mid-decision, possibly onto code that's about to be reverted out
# from under it. Kickstarting claude (only on a confirmed, recorded success)
# is update_manager.py's job, done last, after it has already written
# last-update.json and posted the inbox note.
#
# Exit codes, which update_manager.py's caller relies on:
#   0 -- healthy (pytest passed; router confirmed running, or services
#        aren't loaded at all so there's nothing further to check).
#   1 -- pytest failed, or any other generic failure. No service was
#        touched.
#   2 -- pytest passed and the router WAS kickstarted onto the merged code,
#        but never reported `state = running` within the timeout. The
#        updater rolls back on this code specifically, then re-kickstarts
#        the router itself onto the reverted (last-known-good) code.
#
# Two phases:
#   1. Run the macOS test suite.
#   2. If (and only if) com.lobster.router is already loaded, kickstart it
#      on the merged code and wait for it to come back up. If it isn't
#      loaded (e.g. plists installed but not yet bootstrapped), phase 1's
#      result is the whole check.
#
# Test-only overrides (never set in mac.env; production always uses the
# defaults below):
#   LOBSTER_SELFCHECK_CMD          -- replaces this ENTIRE script (read by
#                                      update_manager.py, not by this file).
#   LOBSTER_SELFCHECK_PYTEST_CMD   -- replaces just phase 1, so phase 2 can
#                                      be tested against a real fake
#                                      launchctl without a full pytest run.
#   LOBSTER_SELFCHECK_ROUTER_TIMEOUT / LOBSTER_SELFCHECK_POLL_INTERVAL --
#                                      shrink phase 2's wait loop for tests.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

if [ -n "${LOBSTER_SELFCHECK_PYTEST_CMD:-}" ]; then
    # shellcheck disable=SC2086
    eval "$LOBSTER_SELFCHECK_PYTEST_CMD"
else
    .venv/bin/pytest tests/macos -q
fi
PYTEST_STATUS=$?

if [ "$PYTEST_STATUS" -ne 0 ]; then
    exit 1
fi

UID_NUM="$(id -u)"

if ! launchctl print "gui/$UID_NUM/com.lobster.router" >/dev/null 2>&1; then
    # Not loaded -- nothing running to restart or verify. Phase 1 is the
    # whole check, and com.lobster.claude is never touched by this script.
    exit 0
fi

launchctl kickstart -k "gui/$UID_NUM/com.lobster.router"

TIMEOUT="${LOBSTER_SELFCHECK_ROUTER_TIMEOUT:-60}"
POLL_INTERVAL="${LOBSTER_SELFCHECK_POLL_INTERVAL:-1}"
DEADLINE=$(( $(date +%s) + TIMEOUT ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    if launchctl print "gui/$UID_NUM/com.lobster.router" 2>/dev/null | grep -q "state = running"; then
        exit 0
    fi
    sleep "$POLL_INTERVAL"
done

echo "selfcheck: com.lobster.router did not reach state = running within ${TIMEOUT}s" >&2
exit 2
