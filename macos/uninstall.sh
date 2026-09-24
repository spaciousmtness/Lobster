#!/bin/bash
# macos/uninstall.sh -- boots out and removes Clawdaddy's three launchd
# LaunchAgents. Idempotent: safe to run whether or not they are loaded, or
# whether or not the plists still exist.
#
# Usage:
#   bash macos/uninstall.sh
set -euo pipefail

TARGET_HOME="${HOME:?HOME must be set}"
LAUNCH_AGENTS_DIR="$TARGET_HOME/Library/LaunchAgents"
uid="$(id -u)"

PLISTS=(com.lobster.router com.lobster.claude com.lobster.update-check)
for name in "${PLISTS[@]}"; do
    dest="$LAUNCH_AGENTS_DIR/${name}.plist"
    launchctl bootout "gui/$uid/$name" >/dev/null 2>&1 || true
    if [[ -f "$dest" ]]; then
        rm -f "$dest"
        echo "removed $dest"
    else
        echo "$dest not present, nothing to remove"
    fi
done
