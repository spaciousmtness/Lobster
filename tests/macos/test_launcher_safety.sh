#!/usr/bin/env bash
# tests/macos/test_launcher_safety.sh
#
# Final fix wave, C1 + I4, for scripts/claude-persistent.sh:
#   C1  the launcher's own `python3 -c` snippets must never import a module
#       from the current directory (the agent can write ~/lobster-workspace,
#       so a json.py planted there would run unsandboxed as Melissa), and the
#       launcher must cd into the workspace only inside the subshell that
#       execs claude.
#   I4  with LOBSTER_LOG_DIR set, the launcher's logs and the startup flag
#       land there and nothing is written into the workspace; unset (Linux),
#       they stay where they always were.
#
# Usage: bash tests/macos/test_launcher_safety.sh
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO/scripts/claude-persistent.sh"
TMP="$(mktemp -d)"
TMP="$(cd "$TMP" && pwd -P)"
trap 'sleep 0.2; rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); echo "ok   - $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL - $1"; }

mkdir -p "$TMP/bin" "$TMP/evil"
# A module the agent could plant in its own folder: importing it leaves a mark.
for mod in json sys pathlib; do
    printf 'open("%s", "a").write("%s\\n")\n' "$TMP/pwned" "$mod" > "$TMP/evil/$mod.py"
done
cat > "$TMP/bin/claude" <<'EOF'
#!/usr/bin/env bash
pwd -P > "$FAKE_CLAUDE_CWD"
exit 3
EOF
cat > "$TMP/bin/curl" <<'EOF'
#!/usr/bin/env bash
printf '200'
EOF
for t in pgrep tmux systemctl; do
    printf '#!/usr/bin/env bash\nexit 1\n' > "$TMP/bin/$t"
done
chmod +x "$TMP/bin/"*

# ---- C1: python3 -c snippets ignore the current directory -----------------
(
    export LOBSTER_WORKSPACE="$TMP/evil" LOBSTER_MESSAGES="$TMP/msgs1" LOBSTER_LOG_DIR="$TMP/logs1"
    cd "$TMP/evil" || exit 1
    # shellcheck disable=SC1090
    source "$SCRIPT"
    export PATH="$TMP/bin:$PATH"
    echo '{"mode": "active", "session_id": "s"}' > "$STATE_FILE"
    write_boot_stamp >/dev/null
    read_state_mode >/dev/null
    find_session_to_resume >/dev/null
    handle_exit 0 >/dev/null
) 2>/dev/null
if [[ -e "$TMP/pwned" ]]; then
    bad "launcher python3 -c imported a module from cwd: $(tr '\n' ' ' < "$TMP/pwned")"
else
    ok "launcher python3 -c never imports from the current directory"
fi

n_unisolated="$(grep -nE 'python3 +-c' "$SCRIPT" | grep -vcE 'python3 +-I +-c')"
[[ "$n_unisolated" -eq 0 ]] && ok "every python3 -c in the launcher is isolated (-I)" \
    || bad "$n_unisolated python3 -c call(s) without -I: $(grep -nE 'python3 +-c' "$SCRIPT")"

# ---- C1 + I4: launch_claude ------------------------------------------------
# run_launch <log-dir-or-__UNSET__> <tag>: runs launch_claude once from
# $TMP/start with a fake claude; prints the launcher's cwd afterwards.
run_launch() {
    (
        export LOBSTER_WORKSPACE="$TMP/ws-$2" LOBSTER_MESSAGES="$TMP/msgs-$2"
        export FAKE_CLAUDE_CWD="$TMP/claude-cwd-$2" LOBSTER_CLAUDE_FLAGS="--settings /x.json"
        if [[ "$1" == "__UNSET__" ]]; then unset LOBSTER_LOG_DIR; else export LOBSTER_LOG_DIR="$1"; fi
        mkdir -p "$LOBSTER_WORKSPACE" "$TMP/start"
        cd "$TMP/start" || exit 1
        source "$SCRIPT"
        export PATH="$TMP/bin:$PATH"
        launch_claude 1 >/dev/null 2>&1
        pwd -P
    ) 2>/dev/null
}

after="$(run_launch "$TMP/state/logs" mac)"
[[ "$(cat "$TMP/claude-cwd-mac" 2>/dev/null)" == "$TMP/ws-mac" ]] && ok "claude itself still starts in the workspace" \
    || bad "claude cwd: $(cat "$TMP/claude-cwd-mac" 2>/dev/null)"
[[ "$after" == "$TMP/start" ]] && ok "launcher's own cwd is unchanged after launch_claude" \
    || bad "launcher cwd after launch_claude: $after"
[[ -f "$TMP/state/logs/claude-session.log" ]] && ok "session log goes to LOBSTER_LOG_DIR" || bad "session log not in LOBSTER_LOG_DIR"
[[ -f "$TMP/state/logs/claude-persistent.log" ]] && ok "launcher log goes to LOBSTER_LOG_DIR" || bad "launcher log not in LOBSTER_LOG_DIR"
[[ -f "$TMP/state/logs/dispatcher-startup-flag" ]] && ok "startup flag goes to LOBSTER_LOG_DIR" || bad "startup flag not in LOBSTER_LOG_DIR"
left="$(cd "$TMP/ws-mac" && find . -mindepth 1 | tr '\n' ' ')"
[[ -z "$left" ]] && ok "nothing written into the workspace with LOBSTER_LOG_DIR set" || bad "workspace got: $left"

run_launch __UNSET__ linux >/dev/null
[[ -f "$TMP/ws-linux/logs/claude-session.log" ]] && ok "unset LOBSTER_LOG_DIR keeps workspace/logs (Linux)" || bad "linux session log missing"
[[ -f "$TMP/ws-linux/data/dispatcher-startup-flag" ]] && ok "unset LOBSTER_LOG_DIR keeps workspace/data startup flag (Linux)" || bad "linux startup flag missing"

echo "passed=$PASS failed=$FAIL"
[[ $FAIL -eq 0 ]]
