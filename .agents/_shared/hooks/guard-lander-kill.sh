#!/bin/bash
# PreToolUse(Bash): refuse pattern-wide kills that can hit another session's lander.
#
# `pkill -f` / `killall` / `pgrep -f … | xargs kill` match every process on the
# machine whose command line contains the pattern — every worktree's
# `pnpm pr:land`, and the calling shell itself (the mystery exit 144). The
# victim sees only EXIT=143, which reads like an outcome and is not. The rule
# was written in AGENTS.md, ai-review.md and the orchestrate skill, and an agent
# still did it on 2026-10-04 — so it is enforced here instead of restated.
#
# Scope: only kills whose text names something a lander runs as (pr-land,
# pr:land, pnpm, node). Killing an emulator or Metro by its own name is untouched.
set -euo pipefail

cmd=$(jq -r '.tool_input.command // empty')
[ -z "$cmd" ] && exit 0

# Command position only (line start, or after ; & | ( ` $( ), so prose that
# merely mentions pkill — a commit message, a doc edit — is not refused.
cmd_start='(^|[;&|(`]|\$\()[[:space:]]*(sudo[[:space:]]+)?'
kill_by_pattern="${cmd_start}(pkill|killall)([[:space:]]|\$)|${cmd_start}pgrep[[:space:]][^|]*-[a-zA-Z]*f[^|]*\|[[:space:]]*(xargs[[:space:]]+)?kill"
lander_shaped='pr[-:]land|pnpm|(^|[^a-z])node([^a-z_-]|$)'

if grep -Eq "$kill_by_pattern" <<<"$cmd" && grep -Eq "$lander_shaped" <<<"$cmd"; then
  jq -n --arg reason "Refused: a pattern-wide kill naming pr-land/pnpm/node also kills every other worktree's lander and can self-match this shell. Stop YOUR lander with scripts/pr-land-bg.sh --kill (it resolves /proc/<pid>/cwd and touches only this worktree). For any other process, find it with 'ps -eo pid,args', confirm ownership with 'readlink /proc/<pid>/cwd', and kill that pid alone." \
    '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: $reason}}'
fi
exit 0
