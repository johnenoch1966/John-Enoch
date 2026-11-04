#!/usr/bin/env bash
# Publish the latest report to x20 (Google-internal static hosting).
# Visible to Googlers only (corp network + SSO). Safe to run from cron.
#
#   X20_DIR=/google/data/rw/teams/<team>/   (recommended; https://<team>.teams.x20web.corp.google.com/)
#   X20_DIR=/google/data/rw/users/jo/johnenoch/agentic-adoption   (personal quick share)
#   DATA_WT=~/projects/research-data-wt     (worktree of the research-data branch)
set -euo pipefail
X20_DIR="${X20_DIR:?set X20_DIR}"
DATA_WT="${DATA_WT:-$HOME/projects/research-data-wt}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/john_enoch_deploy}"

export GIT_SSH_COMMAND="ssh -i $SSH_KEY -o IdentitiesOnly=yes -o BatchMode=yes"
git -C "$DATA_WT" pull -q --ff-only origin research-data
mkdir -p "$X20_DIR"
rsync -a --delete --exclude draft.html "$DATA_WT/reports/" "$X20_DIR/"
# x20 serves what POSIX "other" can read: make the report readable by all Googlers.
chmod o+rx "$X20_DIR" && find "$X20_DIR" -type d -exec chmod o+rx {} + && find "$X20_DIR" -type f -exec chmod o+r {} +
echo "$(date -u +%FT%TZ) published $(git -C "$DATA_WT" log -1 --format=%h) to $X20_DIR"
