#!/bin/bash
# macos/run-with-env.sh -- sources Lobster's env files, then execs the given
# command with that environment in place.
#
# launchd's ProgramArguments cannot run a shell snippet, so every LaunchAgent
# points its first argument at this script instead: it sources
# config/config.env (secrets, mode 600) and macos/mac.env (committed, no
# secrets), exports everything either file sets, then execs the real command.
#
# This only needs to source and exec, so plain bash (3.2 on macOS) is fine --
# unlike scripts/claude-persistent.sh, which needs BASHPID and is invoked
# with /opt/homebrew/bin/bash explicitly by the plist that runs it.
set -euo pipefail

set -a
# shellcheck disable=SC1091
. "$HOME/lobster/config/config.env"
# shellcheck disable=SC1091
. "$HOME/lobster/macos/mac.env"
set +a

# mac.env is committed and can't hold one operator's literal uid, so the wake
# command is completed here instead, after config.env/mac.env are sourced,
# and only when nothing upstream already set it. lobster_bot.py runs this
# without a shell and refuses it outright if a "$" survives.
: "${LOBSTER_WAKE_CMD:=launchctl kickstart -k gui/$(id -u)/com.lobster.claude}"
export LOBSTER_WAKE_CMD

exec "$@"
