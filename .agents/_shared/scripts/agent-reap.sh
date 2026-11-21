#!/usr/bin/env bash
# Reap one finished orchestrated worker: lander, slot, window, worktree, branch.
#
#   scripts/agent-reap.sh <name>              # refuses unless the worktree is clean and merged
#   options: --session <tmux-session> (default <project>-fleet) · --dry-run (print, run nothing)
#
# <name> is the worker's address, as given to agent-dispatch.sh: the worktree is
# <worktreesDir>/<name>. <project>/<worktreesDir> come from
# .agents/orchestrate.config.json, <base> from .agents/land.config.json.
#
# Why a script: git refuses a plain `git worktree remove` in any repo with a
# submodule, and by hand that refusal is what tempts `--force` — past exactly
# the uncommitted or unmerged work the two checks below exist to catch. A
# leader on 2026-11-20 reaped a whole batch with `worktree remove --force`
# while the skill said never to. The checks only hold if nobody types the
# steps by hand, so they live here.
#
# Exit: 0 reaped (or nothing left to reap) · 1 usage/config error ·
#       2 refused: the worktree is dirty or its HEAD is not on origin/<base>.
set -euo pipefail

usage() { sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//'; }

SESSION=""
DRY_RUN=0
POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --session) SESSION="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "agent-reap: unknown option $1" >&2; usage >&2; exit 1 ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done
[[ ${#POSITIONAL[@]} -eq 1 ]] || { usage >&2; exit 1; }
NAME="${POSITIONAL[0]}"
[[ "$NAME" =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "agent-reap: name must be kebab-case, got '$NAME'" >&2; exit 1; }

MAIN_ROOT="$(cd "$(git rev-parse --git-common-dir)/.." && pwd)"
config_value() {
  local file="$MAIN_ROOT/.agents/$1" query="$2" fallback="$3" v=""
  if [[ -f "$file" ]] && command -v jq >/dev/null; then v="$(jq -r "$query // empty" "$file")"; fi
  echo "${v:-$fallback}"
}
PROJECT="$(config_value orchestrate.config.json .project "$(basename "$MAIN_ROOT")")"
WORKTREES_DIR="$(config_value orchestrate.config.json .worktreesDir .claude/worktrees)"
BASE="$(config_value land.config.json .baseBranch develop)"
SESSION="${SESSION:-$PROJECT-fleet}"
WORKTREE="$MAIN_ROOT/$WORKTREES_DIR/$NAME"

run() {
  if [[ $DRY_RUN -eq 1 ]]; then printf '+'; printf ' %q' "$@"; printf '\n'; else "$@"; fi
}

if [[ ! -d "$WORKTREE" ]]; then
  echo "agent-reap: no worktree at $WORKTREE; closing its window and pruning only"
  run tmux kill-window -t "$SESSION:$NAME" 2>/dev/null || true
  run git -C "$MAIN_ROOT" worktree prune
  exit 0
fi

BRANCH="$(git -C "$WORKTREE" rev-parse --abbrev-ref HEAD)"
HEAD_SHA="$(git -C "$WORKTREE" rev-parse HEAD)"

# The two checks. Both are read-only, and both run before anything is touched.
git -C "$MAIN_ROOT" fetch --quiet origin "$BASE"
DIRTY="$(git -C "$WORKTREE" status --porcelain)"
if [[ -n "$DIRTY" ]]; then
  echo "agent-reap: REFUSED — $WORKTREE has uncommitted changes:" >&2
  echo "$DIRTY" | head -20 >&2
  exit 2
fi
if ! git -C "$MAIN_ROOT" merge-base --is-ancestor "$HEAD_SHA" "origin/$BASE"; then
  echo "agent-reap: REFUSED — $BRANCH ($HEAD_SHA) is not on origin/$BASE; report it, don't reap it" >&2
  exit 2
fi

# The lander first: a lander still polling would see its tree vanish.
if [[ -x "$WORKTREE/scripts/pr-land-bg.sh" ]]; then
  run bash -c 'cd "$1" && scripts/pr-land-bg.sh --kill' _ "$WORKTREE" || true
fi
if [[ -f "$WORKTREE/scripts/agent-env.sh" ]]; then
  run bash -c 'cd "$1" && source scripts/agent-env.sh --clean' _ "$WORKTREE" || true
fi
run tmux kill-window -t "$SESSION:$NAME" 2>/dev/null || true
run rm -rf "$WORKTREE"
run git -C "$MAIN_ROOT" worktree prune
# -d, never -D: it refuses an unmerged branch, which the check above already ruled out.
if [[ "$BRANCH" != "HEAD" ]]; then
  run git -C "$MAIN_ROOT" branch -d "$BRANCH" || true
fi
echo "agent-reap: reaped $NAME ($BRANCH at ${HEAD_SHA:0:8}, on origin/$BASE)"
