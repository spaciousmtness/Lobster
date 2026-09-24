#!/usr/bin/env bash
# tests/macos/test_run_with_env.sh
#
# run-with-env.sh must source config.env then mac.env with `set -a`, exec
# the given command with that environment, and complete LOBSTER_WAKE_CMD
# with the real numeric uid (never a literal "$UID"/"$(id -u)" string) --
# lobster_bot.py runs that command without a shell and refuses a leftover
# "$" in it. An already-set LOBSTER_WAKE_CMD must survive untouched.
#
# Usage: bash tests/macos/test_run_with_env.sh
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO/macos/run-with-env.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/lobster/config" "$TMP/lobster/macos"
cat > "$TMP/lobster/config/config.env" <<'EOF'
LOBSTER_WORKSPACE=/fake/workspace
LOBSTER_MESSAGES=/fake/messages
LOBSTER_INSTALL_DIR=/fake/lobster
EOF
cp "$REPO/macos/mac.env" "$TMP/lobster/macos/mac.env"

PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); echo "ok   - $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL - $1"; }

real_uid="$(id -u)"

out="$(HOME="$TMP" "$SCRIPT" env)"

wake_line="$(grep '^LOBSTER_WAKE_CMD=' <<<"$out")"
if [[ "$wake_line" == "LOBSTER_WAKE_CMD=launchctl kickstart -k gui/${real_uid}/com.lobster.claude" ]]; then
    ok "LOBSTER_WAKE_CMD carries the real numeric uid"
else
    bad "LOBSTER_WAKE_CMD carries the real numeric uid: $wake_line"
fi
if [[ "$wake_line" != *'$'* ]]; then
    ok "LOBSTER_WAKE_CMD has no literal \$"
else
    bad "LOBSTER_WAKE_CMD has no literal \$: $wake_line"
fi

flags_line="$(grep '^LOBSTER_CLAUDE_FLAGS=' <<<"$out")"
expected_flags="LOBSTER_CLAUDE_FLAGS=--setting-sources= --settings $TMP/lobster/macos/claude-settings.json --mcp-config $TMP/lobster/macos/mcp.json --strict-mcp-config --permission-mode dontAsk --append-system-prompt-file $TMP/lobster/macos/CLAUDE.mac.md"
if [[ "$flags_line" == "$expected_flags" ]]; then
    ok "LOBSTER_CLAUDE_FLAGS is exported with \$HOME expanded"
else
    bad "LOBSTER_CLAUDE_FLAGS is exported with \$HOME expanded: $flags_line"
fi

grep -qxF "LOBSTER_CLAUDE_MATCH=claude.*lobster/macos/claude-settings.json" <<<"$out" \
    && ok "LOBSTER_CLAUDE_MATCH exported" || bad "LOBSTER_CLAUDE_MATCH exported: $out"
grep -qxF "ENABLE_TOOL_SEARCH=false" <<<"$out" \
    && ok "ENABLE_TOOL_SEARCH exported" || bad "ENABLE_TOOL_SEARCH exported"
grep -qxF "LOBSTER_UPDATE_REMOTE=upstream" <<<"$out" \
    && ok "LOBSTER_UPDATE_REMOTE exported" || bad "LOBSTER_UPDATE_REMOTE exported"
grep -q "^PATH=$TMP/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin" <<<"$out" \
    && ok "PATH exported from mac.env" || bad "PATH exported from mac.env: $out"
grep -qxF "LOBSTER_WORKSPACE=/fake/workspace" <<<"$out" \
    && ok "config.env values are exported too" || bad "config.env values are exported too"

# An already-set LOBSTER_WAKE_CMD must not be overwritten.
out2="$(HOME="$TMP" LOBSTER_WAKE_CMD="true" "$SCRIPT" env)"
grep -qxF "LOBSTER_WAKE_CMD=true" <<<"$out2" \
    && ok "pre-set LOBSTER_WAKE_CMD is left alone" || bad "pre-set LOBSTER_WAKE_CMD is left alone: $out2"

echo "passed=$PASS failed=$FAIL"
[[ $FAIL -eq 0 ]]
