#!/usr/bin/env bash
# Start one orchestrated worker: worktree + tmux window + claude, confirmed running.
#
#   scripts/agent-dispatch.sh <name> <branch> <prompt-file>   # new worker, new worktree off origin/<base>
#   scripts/agent-dispatch.sh --resume <name> [prompt-file]   # relaunch `claude --continue` in its existing worktree
#   options: --session <tmux-session> (default <project>-fleet) · --dry-run (print, run nothing)
#
# <name> is the worker's address everywhere: the worktree directory
# (<worktreesDir>/<name>), the tmux window and the session's `-n` name.
# <project> and <worktreesDir> come from .agents/orchestrate.config.json and
# <base> from .agents/land.config.json's baseBranch (default develop).
#
# The hand-copied recipe this replaces failed silently three ways, each of which
# cost a batch real time:
#   1. Every fresh `claude` stops on a dialog (a settings warning, a folder-trust
#      prompt) before reading its prompt. Six workers sat idle behind one until
#      the leader noticed. This script answers it and confirms the run started.
#   2. Panes in one shared window vanish when a worker exits and take its last
#      output with them. One window per worker, kept open after exit.
#   3. A worktree cut from the local base branch inherits however stale the main
#      checkout is. The base is origin/<base>, fetched first.
set -euo pipefail

usage() { sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; }

SESSION=""
DRY_RUN=0
RESUME=0
POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --session) SESSION="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --resume) RESUME=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "agent-dispatch: unknown option $1" >&2; usage >&2; exit 1 ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done

if [[ $RESUME -eq 1 ]]; then
  [[ ${#POSITIONAL[@]} -ge 1 && ${#POSITIONAL[@]} -le 2 ]] || { usage >&2; exit 1; }
  NAME="${POSITIONAL[0]}"; BRANCH=""; PROMPT_FILE="${POSITIONAL[1]:-}"
else
  [[ ${#POSITIONAL[@]} -eq 3 ]] || { usage >&2; exit 1; }
  NAME="${POSITIONAL[0]}"; BRANCH="${POSITIONAL[1]}"; PROMPT_FILE="${POSITIONAL[2]}"
fi

[[ "$NAME" =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "agent-dispatch: name must be kebab-case, got '$NAME'" >&2; exit 1; }
if [[ -n "$PROMPT_FILE" ]]; then
  [[ -s "$PROMPT_FILE" ]] || { echo "agent-dispatch: prompt file '$PROMPT_FILE' is missing or empty" >&2; exit 1; }
  PROMPT_FILE="$(cd "$(dirname "$PROMPT_FILE")" && pwd)/$(basename "$PROMPT_FILE")"
fi

# Worktrees always live under the MAIN checkout, wherever this is run from.
MAIN_ROOT="$(cd "$(git rev-parse --git-common-dir)/.." && pwd)"

# Repo facts, from the consuming repo's config (never from this file's location:
# consumers reach it through a symlink into the submodule).
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
SETTINGS='{"crossSessionInbound":"accept"}'

run() {
  if [[ $DRY_RUN -eq 1 ]]; then printf '+'; printf ' %q' "$@"; printf '\n'; else "$@"; fi
}

# Every precondition is checked before the repo is touched: a refusal must leave
# nothing behind, or the retry is refused too ("already exists — use --resume")
# for a worker that was never launched.
if tmux list-windows -t "$SESSION" -F '#W' 2>/dev/null | grep -qxF "$NAME"; then
  echo "agent-dispatch: window $SESSION:$NAME already exists" >&2; exit 1
fi
if [[ $RESUME -eq 1 ]]; then
  [[ -d "$WORKTREE" ]] || { echo "agent-dispatch: no worktree at $WORKTREE to resume" >&2; exit 1; }
else
  [[ ! -e "$WORKTREE" ]] || { echo "agent-dispatch: $WORKTREE already exists — use --resume" >&2; exit 1; }
  if git -C "$MAIN_ROOT" show-ref --verify --quiet "refs/heads/$BRANCH"; then
    echo "agent-dispatch: branch '$BRANCH' already exists" >&2; exit 1
  fi
fi

# A step that fails after the worktree exists but before the window is up takes
# back exactly what this run created — the worktree, and the branch only while
# it still sits on the commit it was cut from — so a retry starts clean.
CREATED=0
LAUNCHED=0
BASE_SHA=""
rollback() {
  echo "agent-dispatch: rolling back the worktree and branch this run created" >&2
  rm -rf "$WORKTREE"
  git -C "$MAIN_ROOT" worktree prune
  if [[ "$(git -C "$MAIN_ROOT" rev-parse --verify --quiet "refs/heads/$BRANCH")" == "$BASE_SHA" ]]; then
    git -C "$MAIN_ROOT" branch -D --quiet "$BRANCH"
  fi
}
trap 'rc=$?; if [[ $rc -ne 0 && $CREATED -eq 1 && $LAUNCHED -eq 0 ]]; then rollback; fi; exit $rc' EXIT

if [[ $RESUME -eq 1 ]]; then
  # --continue picks up the worker's own transcript, so the warm worktree keeps its context.
  CLAUDE_ARGS=(claude --continue -n "$NAME" --permission-mode auto --settings "$SETTINGS")
else
  run git -C "$MAIN_ROOT" fetch --quiet origin "$BASE"
  [[ $DRY_RUN -eq 1 ]] || BASE_SHA="$(git -C "$MAIN_ROOT" rev-parse "origin/$BASE")"
  run git -C "$MAIN_ROOT" worktree add --quiet "$WORKTREE" -b "$BRANCH" "origin/$BASE"
  [[ $DRY_RUN -eq 1 ]] || CREATED=1
  # Git never populates submodules in a new worktree; without .agents/_shared the
  # worker's skills load as nothing and `pnpm pr:land` cannot resolve.
  run git -C "$WORKTREE" submodule update --init --quiet
  CLAUDE_ARGS=(claude -n "$NAME" --permission-mode auto --settings "$SETTINGS")
fi

if [[ $DRY_RUN -eq 1 ]] || ! tmux has-session -t "$SESSION" 2>/dev/null; then
  run tmux new-session -d -s "$SESSION" -n fleet
fi

# The prompt is read from the file inside the window, so no quoting layer ever
# sees it. The trailing sleep keeps the window, and the worker's last output,
# alive after it exits. The `cd` is because a tmux server whose own directory was
# deleted (a reaped worktree) ignores `-c` and starts panes there.
# shellcheck disable=SC2016
LAUNCH='cd "$WORKTREE" && "$@" ${PROMPT_FILE:+"$(cat "$PROMPT_FILE")"}; echo "[worker exited $?]"; sleep 100000'
run tmux new-window -d -t "$SESSION:" -n "$NAME" -c "$WORKTREE" \
  -e "WORKTREE=$WORKTREE" -e "PROMPT_FILE=$PROMPT_FILE" bash -c "$LAUNCH" _ "${CLAUDE_ARGS[@]}"
LAUNCHED=1

if [[ $DRY_RUN -eq 1 ]]; then exit 0; fi

# Answer the startup dialogs, then wait for proof the prompt is being worked.
TARGET="$SESSION:$NAME"
for _ in $(seq 1 30); do
  sleep "${AGENT_DISPATCH_POLL_SECONDS:-2}"
  PANE="$(tmux capture-pane -pt "$TARGET" 2>/dev/null || true)"
  # Exit first: the window keeps its scrollback, so a worker that started and
  # died shows BOTH markers — read the other way round, that is a "success".
  if grep -qE 'worker exited' <<<"$PANE"; then
    echo "agent-dispatch: $NAME exited during startup:" >&2; tail -20 <<<"$PANE" >&2; exit 1
  fi
  if grep -qE 'esc to interrupt' <<<"$PANE"; then
    echo "dispatched $NAME — window $TARGET, worktree $WORKTREE${BRANCH:+, branch $BRANCH}"
    exit 0
  fi
  # Only the two startup dialogs, never a generic "proceed?": that wording is
  # also a tool-permission prompt, and Enter there would approve it unread.
  if grep -qiE 'Settings Warning|trust (the files|this folder)' <<<"$PANE"; then
    tmux send-keys -t "$TARGET" Enter
  fi
done
echo "agent-dispatch: $NAME started but never showed it was working after 60 s — read it with" >&2
echo "  tmux capture-pane -pt $TARGET" >&2
exit 3
