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
WORKSPACE="$TARGET_HOME/lobster-workspace"
STATE_DIR="$TARGET_HOME/.local/state/clawdaddy"
# Logs live outside the workspace (LOBSTER_LOG_DIR in mac.env, and the
# plists' Standard{Out,Error}Path): the agent can write the workspace.
LOG_DIR="$STATE_DIR/logs"
USER_CONFIG="$WORKSPACE/user-config"
OLD_USER_CONFIG="$TARGET_HOME/lobster-user-config"

mkdir -p "$LAUNCH_AGENTS_DIR" "$STATE_DIR" "$LOG_DIR" "$USER_CONFIG"
chmod 700 "$STATE_DIR" "$LOG_DIR"

# Lobster's user config (LOBSTER_USER_CONFIG in mac.env) lives inside the
# workspace on this Mac. Move anything from the old ~/lobster-user-config
# over, entry by entry, never overwriting; the old folder itself is left
# for Melissa to delete.
if [[ -d "$OLD_USER_CONFIG" ]]; then
    for entry in "$OLD_USER_CONFIG"/* "$OLD_USER_CONFIG"/.[!.]*; do
        [[ -e "$entry" ]] || continue
        name="$(basename "$entry")"
        if [[ -e "$USER_CONFIG/$name" ]]; then
            echo "install.sh: kept $USER_CONFIG/$name; $entry left in place (both exist)"
        else
            mv "$entry" "$USER_CONFIG/$name"
            echo "moved $entry -> $USER_CONFIG/$name"
        fi
    done
fi

# The workspace CLAUDE.md: Claude Code loads no CLAUDE.md here
# (--setting-sources=), so the launcher's "Read CLAUDE.md" prompt lands on
# this file, which points at Lobster's real instructions. Rewritten on every
# run while it still carries the marker line; a hand-written one is left alone.
WS_CLAUDE_MD="$WORKSPACE/CLAUDE.md"
MARKER="<!-- written by lobster/macos/install.sh; edits are overwritten on the next install -->"
if [[ ! -e "$WS_CLAUDE_MD" ]] || [[ "$(head -n 1 "$WS_CLAUDE_MD")" == "$MARKER" ]]; then
    admin_chat_id=""
    config_env="$TARGET_HOME/lobster/config/config.env"
    if [[ -f "$config_env" ]]; then
        admin_chat_id="$(grep -m1 '^LOBSTER_ADMIN_CHAT_ID=' "$config_env" | cut -d= -f2- | tr -d '"' || true)"
        if [[ -z "$admin_chat_id" ]]; then
            admin_chat_id="$(grep -m1 '^TELEGRAM_ALLOWED_USERS=' "$config_env" | cut -d= -f2- | tr -d '"' | cut -d, -f1 || true)"
        fi
    fi
    [[ "$admin_chat_id" =~ ^-?[0-9]+$ ]] || admin_chat_id=""
    {
        echo "$MARKER"
        echo "# Lobster workspace"
        echo
        echo "This folder is your workspace. Lobster's instructions live in the repo:"
        echo
        echo "1. Read \`~/lobster/CLAUDE.md\`."
        echo "2. Read \`~/lobster/.claude/sys.dispatcher.bootup.md\` and follow its Startup Behavior,"
        echo "   with the changes under \"Starting up\" in your system prompt (CLAUDE.mac.md)."
        echo
        if [[ -n "$admin_chat_id" ]]; then
            echo "ADMIN_CHAT_ID=$admin_chat_id"
        else
            echo "ADMIN_CHAT_ID is not set: config/config.env had no TELEGRAM_ALLOWED_USERS."
        fi
    } > "$WS_CLAUDE_MD"
    echo "wrote $WS_CLAUDE_MD"
else
    echo "install.sh: left hand-written $WS_CLAUDE_MD alone"
fi

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
