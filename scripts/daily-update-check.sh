#!/bin/bash
# Daily check for Lobster updates - inject message if updates available
set -euo pipefail

# Developer mode: suppress all system notifications so the developer isn't
# bothered while testing. Real user messages are never affected by this flag.
_LOBSTER_CONFIG="${LOBSTER_CONFIG_DIR:-$HOME/lobster-config}/config.env"
if [ -f "$_LOBSTER_CONFIG" ]; then
    _DEV_MODE=$(grep -m1 '^LOBSTER_DEV_MODE=' "$_LOBSTER_CONFIG" 2>/dev/null | cut -d= -f2)
    if [ "$_DEV_MODE" = "true" ] || [ "$_DEV_MODE" = "1" ]; then
        exit 0
    fi
fi
unset _LOBSTER_CONFIG _DEV_MODE

LOBSTER_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
INBOX="${LOBSTER_MESSAGES:-$HOME/messages}/inbox"

# Shared jq --arg JSON builder (BIS-724, scripts/lib/json_message.sh) — single
# source of truth for jq-arg-safe JSON construction, see PR history for #2004.
source "$LOBSTER_DIR/scripts/lib/json_message.sh"

cd "$LOBSTER_DIR"

# Support both git and tarball installs
if [ -d "$LOBSTER_DIR/.git" ]; then
    REMOTE="${LOBSTER_UPDATE_REMOTE:-origin}"

    # REMOTE is an env var, not network input, but it still reaches
    # `git fetch` argv unquoted, and a value like "--upload-pack=<cmd>"
    # there is arbitrary local command execution. Refuse anything that
    # isn't a plain remote name git itself already knows about, before
    # fetching anything.
    case "$REMOTE" in
        -*)
            echo "daily-update-check: refusing remote '$REMOTE' (looks like an option)" >&2
            exit 1
            ;;
    esac
    if ! [[ "$REMOTE" =~ ^[A-Za-z0-9._-]+$ ]]; then
        echo "daily-update-check: refusing invalid remote name '$REMOTE'" >&2
        exit 1
    fi
    if ! git remote | grep -qx -- "$REMOTE"; then
        echo "daily-update-check: '$REMOTE' is not a configured git remote" >&2
        exit 1
    fi

    git fetch --quiet -- "$REMOTE" main

    LOCAL=$(git rev-parse HEAD)
    REMOTE_SHA=$(git rev-parse "$REMOTE/main")

    if [ "$LOCAL" != "$REMOTE_SHA" ]; then
        # Count only commits not already merged, so a Mac install ahead of
        # $REMOTE/main by its own local commits isn't reported as behind.
        BEHIND=$(git rev-list --count "HEAD..$REMOTE/main")
        if [ "$BEHIND" -gt 0 ]; then
            TIMESTAMP=$(date +%s%3N)
            _json_build_message \
                --arg id "${TIMESTAMP}_update_available" \
                --arg source "system" \
                --argjson chat_id 0 \
                --arg type "update_notification" \
                --arg text "UPDATE AVAILABLE: Lobster is ${BEHIND} commits behind ${REMOTE}/main. Use check_updates for details." \
                --arg timestamp "$(date -Iseconds)" \
                > "$INBOX/${TIMESTAMP}_update_available.json"
        fi
    fi
else
    # Tarball mode: check GitHub Releases API
    CURRENT_VERSION=$(cat "$LOBSTER_DIR/VERSION" 2>/dev/null || echo "0.0.0")
    LATEST_TAG=$(curl -fsSL "https://api.github.com/repos/SiderealPress/lobster/releases/latest" 2>/dev/null | jq -r '.tag_name // empty')
    LATEST_VERSION="${LATEST_TAG#v}"

    if [ -n "$LATEST_VERSION" ] && [ "$LATEST_VERSION" != "$CURRENT_VERSION" ]; then
        TIMESTAMP=$(date +%s%3N)
        _json_build_message \
            --arg id "${TIMESTAMP}_update_available" \
            --arg source "system" \
            --argjson chat_id 0 \
            --arg type "update_notification" \
            --arg text "UPDATE AVAILABLE: Lobster v${CURRENT_VERSION} -> v${LATEST_VERSION}. Use check_updates for details." \
            --arg timestamp "$(date -Iseconds)" \
            > "$INBOX/${TIMESTAMP}_update_available.json"
    fi
fi
