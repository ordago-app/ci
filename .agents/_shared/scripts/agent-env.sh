#!/usr/bin/env bash
# Source me inside an agent worktree to give it its own runtime:
#   source scripts/agent-env.sh            # allocate (or reuse) this worktree's slot
#   source scripts/agent-env.sh --clean    # release it
#
# In <worktreesDir>/<name>/ (from .agents/orchestrate.config.json, default
# .claude/worktrees):
#   - allocates a slot in the machine-wide registry (~/.agents/slots.json);
#   - if the repo has a firebase.json, writes firebase.agent.json beside it with
#     every emulator moved into the slot's port block. The repo's emulator test
#     harness must prefer that file when present — that, not an exported variable,
#     is what carries the slot, because agent shells (Claude Code's Bash tool among
#     them) do not keep environment variables from one command to the next;
#   - on first use, initializes submodules and runs the config's `worktreeSetup`
#     commands (a fresh worktree has no node_modules and an EMPTY submodule, so
#     every symlink into .agents/_shared dangles).
# In the main checkout it is a no-op: slot 0, the repo's own ports.

_agent_env_self="${BASH_SOURCE[0]:-$0}"
# Consumers reach this file through a symlink into the .agents/_shared submodule;
# resolve it so the library is found next to the real file.
_agent_env_self="$(readlink -f "$_agent_env_self" 2>/dev/null || echo "$_agent_env_self")"
# shellcheck source=lib/agent-slots.sh
source "$(cd "$(dirname "$_agent_env_self")" && pwd)/lib/agent-slots.sh"

_agent_env_config_value() {
  local root="$1" query="$2" fallback="$3" cfg="$1/.agents/orchestrate.config.json"
  if [[ -f "$cfg" ]]; then
    jq -r "$query // empty" "$cfg"
  fi | { read -r v && echo "$v" || echo "$fallback"; }
}

_agent_env_worktrees_dir() {
  local main_root
  main_root="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)")"
  _agent_env_config_value "$main_root" '.worktreesDir' '.claude/worktrees'
}

_agent_env_setup() {
  local wt="$1" done_marker cmd failed=0
  done_marker="$(git -C "$wt" rev-parse --path-format=absolute --git-path agent-setup.done)"
  [[ -f "$done_marker" ]] && return 0

  if [[ -f "$wt/.gitmodules" ]]; then
    git -C "$wt" submodule update --init --recursive >/dev/null 2>&1 \
      || { echo "agent-env: warning — submodule init failed; shared skills and scripts will not resolve." >&2; failed=1; }
  fi
  if [[ -f "$wt/.agents/orchestrate.config.json" ]]; then
    while IFS= read -r cmd; do
      [[ -n "$cmd" ]] || continue
      echo "agent-env: setup — $cmd"
      (cd "$wt" && bash -c "$cmd") || { echo "agent-env: warning — setup step failed: $cmd" >&2; failed=1; }
    done < <(jq -r '.worktreeSetup // [] | .[]' "$wt/.agents/orchestrate.config.json")
  fi
  # Marked done only on full success, so a failed install is retried next source.
  [[ $failed -eq 0 ]] && touch "$done_marker"
  return 0
}

agent_env_clean() {
  local marker wt slot
  marker="$(agent_resolve_worktree "$PWD" "$(_agent_env_worktrees_dir)")"
  case "$marker" in
    worktree:*)
      wt="${marker#worktree:}"
      slot="$(agent_slot_read "$wt")"
      rm -f "$wt/firebase.agent.json"
      if [[ -n "$slot" ]]; then
        agent_slot_release "$wt" && echo "agent-env: released slot $slot for $wt"
      else
        echo "agent-env: no slot allocated for $wt"
      fi
      ;;
    *)
      echo "agent-env: --clean only runs inside an agent worktree." >&2
      return 1
      ;;
  esac
}

agent_env_main() {
  local marker wt slot base name
  marker="$(agent_resolve_worktree "$PWD" "$(_agent_env_worktrees_dir)")"
  case "$marker" in
    main:*)
      echo "agent-env: main checkout (slot 0) — the repo's own ports, nothing to do."
      return 0
      ;;
    unknown)
      echo "agent-env: not inside an agent worktree; nothing to do." >&2
      return 0
      ;;
  esac

  wt="${marker#worktree:}"
  # An empty slot would still compute a port block — slot 0's, the main checkout's
  # — and collide with whatever runs there.
  if ! slot="$(agent_slot_for_path "$wt")" || [[ -z "$slot" ]]; then
    echo "agent-env: could not allocate a slot for $wt." >&2
    return 1
  fi
  export AGENT_SLOT="$slot"

  _agent_env_setup "$wt"

  if [[ -f "$wt/firebase.json" ]]; then
    agent_write_firebase_config "$slot" "$wt/firebase.json" "$wt/firebase.agent.json" || return 1
    while IFS=$'\t' read -r name port; do
      export "FIREBASE_EMULATOR_$(echo "$name" | tr '[:lower:]' '[:upper:]')_PORT=$port"
    done < <(jq -r '.emulators | to_entries[] | select(.value | type == "object") | [.key, .value.port] | @tsv' "$wt/firebase.agent.json")
  fi

  base="$(agent_slot_base "$slot")"
  echo "agent-env: slot $slot — ports $base-$((base + 99)) — $wt"
  if [[ -f "$wt/firebase.agent.json" ]]; then
    jq -r '.emulators | to_entries[] | select(.value | type == "object") | "  \(.key): \(.value.port)"' "$wt/firebase.agent.json"
  fi
}

if [[ "${1:-}" == "--clean" ]]; then
  agent_env_clean
else
  agent_env_main
fi
_agent_env_rc=$?
unset _agent_env_self
return $_agent_env_rc 2>/dev/null || exit $_agent_env_rc
