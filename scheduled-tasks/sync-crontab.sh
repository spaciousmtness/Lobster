#!/bin/bash
# Lobster Crontab Synchronizer

set -e

WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
REPO_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
JOBS_FILE="$WORKSPACE/scheduled-jobs/jobs.json"
RUNNER="$REPO_DIR/scheduled-tasks/run-job.sh"

if ! command -v crontab &> /dev/null; then
    echo "Warning: crontab not found. Install cron to enable scheduled tasks."
    exit 0
fi

if [ ! -f "$JOBS_FILE" ]; then
    echo "Error: Jobs file not found: $JOBS_FILE"
    exit 1
fi

MARKER="# LOBSTER-SCHEDULED"
EXISTING=$(crontab -l 2>/dev/null | grep -v "$MARKER" | grep -v "$RUNNER" || true)

if command -v jq &> /dev/null; then
    CRON_ENTRIES=$(jq -r --arg runner "$RUNNER" --arg marker "$MARKER" '
        .jobs | to_entries[] |
        select(.value.enabled == true) |
        "\(.value.schedule) \($runner) \(.key) \($marker)"
    ' "$JOBS_FILE" 2>/dev/null || echo "")
else
    CRON_ENTRIES=""
fi

{
    if [ -n "$EXISTING" ]; then
        echo "$EXISTING"
    fi
    if [ -n "$CRON_ENTRIES" ]; then
        echo "$CRON_ENTRIES"
    fi
} | crontab -

echo "Crontab synchronized:"
crontab -l 2>/dev/null | grep "$MARKER" || echo "(no lobster jobs)"
