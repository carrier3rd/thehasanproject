#!/usr/bin/env bash
# Run by vod-update.timer (as root): pulls this repo and the private settings repo as the owning user,
# reinstalls requirements if they changed, and restarts only what changed.
set -uo pipefail
REPO=${REPO:-$(cd "$(dirname "$0")/../.." && pwd)}
USER_NAME=${USER_NAME:-$(stat -c %U "$REPO")}
PRIV=$(cd "$REPO" && runuser -u "$USER_NAME" -- .venv/bin/python -c \
  "import sys; sys.path.insert(0, 'pipeline'); from common import load_config; print(load_config().get('_private', ''))" 2>/dev/null)
git_u() { runuser -u "$USER_NAME" -- git -C "$1" "${@:2}"; }

pull() {  # prints the changed files, if any
  local dir=$1 before after
  [ -d "$dir/.git" ] || return 0
  before=$(git_u "$dir" rev-parse HEAD)
  git_u "$dir" pull --ff-only -q || { echo "update: pull failed in $dir (local changes?)" >&2; return 0; }
  after=$(git_u "$dir" rev-parse HEAD)
  [ "$before" = "$after" ] || git_u "$dir" diff --name-only "$before" "$after"
}

code=$(pull "$REPO")
settings=$( [ -n "$PRIV" ] && pull "$PRIV" )
[ -z "$code$settings" ] && exit 0
echo "update: code changed: ${code:-none}; settings changed: ${settings:-none}"

if grep -qx 'requirements.txt' <<<"$code"; then
  runuser -u "$USER_NAME" -- "$REPO/.venv/bin/pip" install -q -r "$REPO/requirements.txt"
fi
if grep -q 'deploy/laptop/install.sh' <<<"$code"; then
  echo "update: install.sh changed; run 'sudo deploy/laptop/install.sh' to apply new service settings" >&2
fi
systemctl restart vod-web.service vod-admin.service
# restarting the logger costs a few seconds of chat; only do it when its code changed
if grep -qE '^pipeline/(chat_logger|common)\.py$|^config\.yaml$' <<<"$code"; then
  systemctl restart vod-chat-logger.service
fi
