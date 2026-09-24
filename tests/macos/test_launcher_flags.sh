#!/usr/bin/env bash
# tests/macos/test_launcher_flags.sh
#
# claude-persistent.sh must launch claude with $LOBSTER_CLAUDE_FLAGS when set
# (the Mac path) and fall back to --dangerously-skip-permissions when unset
# (the Linux path). Orphan cleanup must use $LOBSTER_CLAUDE_MATCH.
#
# Usage: bash tests/macos/test_launcher_flags.sh
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO/scripts/claude-persistent.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/bin" "$TMP/ws" "$TMP/msgs"
cat > "$TMP/bin/claude" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do printf '%s\n' "$a"; done
printf 'ENABLE_TOOL_SEARCH=%s\n' "${ENABLE_TOOL_SEARCH-<unset>}"
EOF
cat > "$TMP/bin/pgrep" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$PGREP_LOG"
exit 1
EOF
chmod +x "$TMP/bin/claude" "$TMP/bin/pgrep"

PASS=0; FAIL=0
ok()   { PASS=$((PASS + 1)); echo "ok   - $1"; }
bad()  { FAIL=$((FAIL + 1)); echo "FAIL - $1"; }
has()  { grep -qxF -- "$2" <<<"$1"; }

# run_launch <flags-or-__UNSET__>: prints the argv the fake claude received
run_launch() {
    (
        if [[ "$1" == "__UNSET__" ]]; then unset LOBSTER_CLAUDE_FLAGS
        else export LOBSTER_CLAUDE_FLAGS="$1"; fi
        export LOBSTER_WORKSPACE="$TMP/ws" LOBSTER_MESSAGES="$TMP/msgs"
        # shellcheck disable=SC1090
        source "$SCRIPT"
        export PATH="$TMP/bin:$PATH"   # the script prepends ~/.local/bin; ours must win
        exec_claude_dispatcher "the prompt"
    ) 2>&1
}

MAC='--setting-sources= --settings $HOME/lobster/macos/claude-settings.json --mcp-config $HOME/lobster/macos/mcp.json --strict-mcp-config --permission-mode dontAsk --append-system-prompt-file $HOME/lobster/macos/CLAUDE.mac.md'

out="$(run_launch "$MAC")"
has "$out" "--settings" && ok "mac run passes --settings" || bad "mac run passes --settings: $out"
has "$out" "--dangerously-skip-permissions" && bad "mac run must not skip permissions" || ok "mac run does not skip permissions"
has "$out" "$HOME/lobster/macos/claude-settings.json" && ok "literal \$HOME in flags expands" || bad "literal \$HOME in flags expands: $out"
has "$out" "dontAsk" && ok "mac run passes dontAsk" || bad "mac run passes dontAsk"
has "$out" "ENABLE_TOOL_SEARCH=false" && ok "mac run exports ENABLE_TOOL_SEARCH=false" || bad "mac run tool search: $out"
has "$out" "sonnet" && has "$out" "150" && has "$out" "the prompt" && ok "mac run keeps model, max-turns, prompt" || bad "mac run keeps model, max-turns, prompt"

out="$(run_launch "--settings /abs/path/claude-settings.json --permission-mode dontAsk")"
has "$out" "/abs/path/claude-settings.json" && ok "pre-expanded absolute path passes through" || bad "pre-expanded path: $out"

out="$(run_launch '--mcp-config ${HOME}/lobster/macos/mcp.json')"
has "$out" "$HOME/lobster/macos/mcp.json" && ok "literal \${HOME} in flags expands" || bad "\${HOME} form: $out"

out="$(run_launch "   ")"
[[ "$(sed -n 1p <<<"$out")" == "--dangerously-skip-permissions" ]] && ok "whitespace-only flags fall back to default" || bad "whitespace-only fallback: $out"

# bash 5.2 patsub_replacement: an & in $HOME must not be replaced by the match
out="$(HOME="$TMP/a&b" run_launch '--settings $HOME/s.json')"
has "$out" "$TMP/a&b/s.json" && ok "\$HOME with & replaced literally" || bad "& in HOME: $out"

# The setting-sources= form (no file-based sources) must survive word splitting
out="$(run_launch '--setting-sources= --settings /x.json')"
has "$out" "--setting-sources=" && ok "--setting-sources= passes as one word" || bad "setting-sources=: $out"

out="$(run_launch "__UNSET__")"
has "$out" "--dangerously-skip-permissions" && ok "unset falls back to --dangerously-skip-permissions" || bad "unset fallback: $out"
has "$out" "--settings" && bad "unset run must not pass --settings" || ok "unset run passes no --settings"
has "$out" "ENABLE_TOOL_SEARCH=<unset>" && ok "linux run leaves ENABLE_TOOL_SEARCH alone" || bad "linux tool search: $out"
[[ "$(sed -n 1p <<<"$out")" == "--dangerously-skip-permissions" ]] && ok "unset run: flag is first arg" || bad "unset run first arg: $out"

# Orphan cleanup uses LOBSTER_CLAUDE_MATCH
match_run() {
    (
        export LOBSTER_WORKSPACE="$TMP/ws" LOBSTER_MESSAGES="$TMP/msgs" PGREP_LOG="$TMP/pgrep.log"
        if [[ "$1" == "__UNSET__" ]]; then unset LOBSTER_CLAUDE_MATCH
        else export LOBSTER_CLAUDE_MATCH="$1"; fi
        source "$SCRIPT"
        export PATH="$TMP/bin:$PATH"
        tmux() { return 1; }
        kill_orphaned_claude_processes >/dev/null
    ) 2>&1
    cat "$TMP/pgrep.log"
}
out="$(match_run 'claude.*lobster/macos/claude-settings.json')"
has "$out" 'claude.*lobster/macos/claude-settings.json' && ok "orphan cleanup uses LOBSTER_CLAUDE_MATCH" || bad "orphan cleanup match: $out"
out="$(match_run "__UNSET__")"
has "$out" 'claude.*--dangerously-skip-permissions' && ok "orphan cleanup default pattern" || bad "orphan cleanup default: $out"

echo "passed=$PASS failed=$FAIL"
[[ $FAIL -eq 0 ]]
