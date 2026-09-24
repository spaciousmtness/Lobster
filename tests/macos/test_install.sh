#!/usr/bin/env bash
# tests/macos/test_install.sh
#
# install.sh resolves the repo from its own path, not from "$HOME/lobster",
# so it must work correctly even when HOME points at a throwaway directory:
# plists render for that HOME (into $HOME/Library/LaunchAgents) while
# macos/mcp.json renders next to install.sh itself, in this repo's own
# macos/ dir. Without --start, install.sh must write files only and never
# call launchctl bootstrap/load.
#
# Usage: bash tests/macos/test_install.sh
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO/macos/install.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); echo "ok   - $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL - $1"; }

out="$(HOME="$TMP" bash "$SCRIPT" 2>&1)"
rc=$?
[[ $rc -eq 0 ]] && ok "install.sh exits 0 with no --start" || bad "install.sh exits 0 with no --start (rc=$rc): $out"
[[ "$out" != *"bootstrapped"* ]] && ok "no --start never bootstraps anything" || bad "no --start never bootstraps anything: $out"

LA="$TMP/Library/LaunchAgents"
for name in com.lobster.router com.lobster.claude com.lobster.update-check; do
    f="$LA/${name}.plist"
    if [[ -f "$f" ]]; then
        ok "$name.plist was written"
    else
        bad "$name.plist was written"
        continue
    fi

    if plutil -lint "$f" >/dev/null 2>&1; then
        ok "$name.plist passes plutil -lint"
    else
        bad "$name.plist passes plutil -lint: $(plutil -lint "$f" 2>&1)"
    fi

    if grep -q '\$HOME' "$f" || grep -q '@HOME@' "$f"; then
        bad "$name.plist has no literal \$HOME or @HOME@ left"
    else
        ok "$name.plist has no literal \$HOME or @HOME@ left"
    fi

    if grep -qF "$TMP/lobster" "$f"; then
        ok "$name.plist points into the target HOME ($TMP)"
    else
        bad "$name.plist points into the target HOME ($TMP): $(cat "$f")"
    fi
done

if grep -qF "/opt/homebrew/bin/bash" "$LA/com.lobster.claude.plist"; then
    ok "claude agent runs claude-persistent.sh via /opt/homebrew/bin/bash"
else
    bad "claude agent runs claude-persistent.sh via /opt/homebrew/bin/bash"
fi
if grep -qF "/opt/homebrew/bin/bash" "$LA/com.lobster.router.plist"; then
    bad "router agent should not need /opt/homebrew/bin/bash"
else
    ok "router agent does not reference /opt/homebrew/bin/bash"
fi

# macos/mcp.json is a repo-relative artifact: it renders beside install.sh
# itself (this repo's macos/), not under $TMP, because install.sh finds the
# repo from its own path rather than from "$HOME/lobster".
mcp="$REPO/macos/mcp.json"
if [[ -f "$mcp" ]]; then
    ok "macos/mcp.json was rendered into this repo's macos/ dir"
else
    bad "macos/mcp.json was rendered into this repo's macos/ dir"
fi
if [[ -f "$mcp" ]] && ! grep -q '@HOME@' "$mcp"; then
    ok "macos/mcp.json has no @HOME@ left"
else
    bad "macos/mcp.json has no @HOME@ left"
fi
if [[ -f "$mcp" ]] && grep -qF "$TMP/lobster" "$mcp"; then
    ok "macos/mcp.json was rendered using this run's HOME ($TMP)"
else
    bad "macos/mcp.json was rendered using this run's HOME ($TMP)"
fi

# Idempotent: a second run with no --start must succeed again.
out2="$(HOME="$TMP" bash "$SCRIPT" 2>&1)"
rc2=$?
[[ $rc2 -eq 0 ]] && ok "install.sh is idempotent (second run exits 0)" || bad "install.sh is idempotent (second run rc=$rc2): $out2"

echo "passed=$PASS failed=$FAIL"
[[ $FAIL -eq 0 ]]
