#!/bin/bash
# macos/install.sh -- installs Clawdaddy's three launchd LaunchAgents.
#
# Repo location is resolved from THIS SCRIPT'S OWN PATH, never from
# "$HOME/lobster": in a real install the two are the same path, but a test
# can set HOME to a throwaway directory and still run this script from the
# real checkout. That split decides where each generated file goes:
#
#   - macos/mcp.json is rendered from mcp.json.in next to this script, i.e.
#     into THIS REPO's macos/ dir, always -- it is gitignored, regenerated
#     on every run, and lives beside the settings/flags that reference it.
#   - The three plists are rendered for $HOME (real installs: $HOME/lobster
#     IS this repo; a test with HOME=$tmp gets plists written under
#     $tmp/Library/LaunchAgents referencing $tmp/lobster/... paths, without
#     touching the real user's LaunchAgents at all).
#
# Usage:
#   bash macos/install.sh            write files only, load nothing
#   bash macos/install.sh --start    write files, then bootstrap each agent
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_HOME="${HOME:?HOME must be set}"

START=false
for arg in "$@"; do
    case "$arg" in
        --start) START=true ;;
        -h|--help)
            echo "Usage: $0 [--start]"
            exit 0
            ;;
        *)
            echo "install.sh: unknown argument: $arg" >&2
            exit 1
            ;;
    esac
done

LAUNCH_AGENTS_DIR="$TARGET_HOME/Library/LaunchAgents"
LOG_DIR="$TARGET_HOME/lobster-workspace/logs"
STATE_DIR="$TARGET_HOME/.local/state/clawdaddy"

mkdir -p "$LAUNCH_AGENTS_DIR" "$LOG_DIR"
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

# sed treats "&" and "\" specially in a replacement string, and a path can
# legitimately contain either, so escape both before using $TARGET_HOME as
# the substitution for @HOME@.
esc_home=$(printf '%s' "$TARGET_HOME" | sed -e 's/[\&]/\\&/g')

render() {
    # render <src> <dest>
    sed "s|@HOME@|$esc_home|g" "$1" > "$2"
}

# macos/mcp.json always renders into THIS repo's macos/ dir (see header).
render "$SCRIPT_DIR/mcp.json.in" "$SCRIPT_DIR/mcp.json"
echo "wrote $SCRIPT_DIR/mcp.json"

chmod +x "$SCRIPT_DIR/run-with-env.sh"

PLISTS=(com.lobster.router com.lobster.claude com.lobster.update-check)
for name in "${PLISTS[@]}"; do
    template="$SCRIPT_DIR/${name}.plist.in"
    dest="$LAUNCH_AGENTS_DIR/${name}.plist"
    if [[ ! -f "$template" ]]; then
        echo "install.sh: missing template $template" >&2
        exit 1
    fi
    render "$template" "$dest"
    plutil -lint "$dest" >/dev/null
    echo "wrote $dest"
done

if ! $START; then
    echo "install.sh: wrote plists without loading them (pass --start to bootstrap)"
    exit 0
fi

uid="$(id -u)"
for name in "${PLISTS[@]}"; do
    dest="$LAUNCH_AGENTS_DIR/${name}.plist"
    # Idempotent: bootout an already-loaded copy first (ignore failure if it
    # was never loaded), then bootstrap fresh.
    launchctl bootout "gui/$uid" "$dest" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$uid" "$dest"
    echo "bootstrapped $name"
done
