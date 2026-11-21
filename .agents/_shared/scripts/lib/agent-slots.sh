#!/usr/bin/env bash
# Agent slot allocator + port mapper. Sourced by scripts/agent-env.sh, from bash or
# zsh; never executed directly. Needs git, jq and perl.
#
# One registry per MACHINE, keyed by absolute worktree path, so worktrees of
# different repos never share a slot number — and therefore never a port block.

if [[ -n "${_AGENT_SLOTS_SOURCED:-}" ]]; then
  return 0 2>/dev/null || exit 0
fi
_AGENT_SLOTS_SOURCED=1

agent_slots_file() {
  echo "${AGENT_SLOTS_FILE:-$HOME/.agents/slots.json}"
}

agent_slots_init() {
  local file
  file="$(agent_slots_file)"
  if ! { mkdir -p "$(dirname "$file")" && { [[ -s "$file" ]] || echo '{"version":1,"slots":{}}' > "$file"; }; } 2>/dev/null; then
    echo "agent-slots: cannot create the slot file $file." >&2
    return 1
  fi
}

# No function here may name a local `path`: in zsh `path` is the array tied to
# $PATH, so such a local empties PATH for the whole body and git/jq stop resolving.
agent_slot_read() {
  agent_slots_init || return 1
  jq -r --arg p "$1" '.slots[$p] // empty' "$(agent_slots_file)"
}

agent_slot_next_free() {
  jq -r '
    (.slots | to_entries | map(.value)) as $used
    | first(range(1; 400) | select(. as $n | ($used | index($n) | not)))' \
    "$(agent_slots_file)"
}

# Run "$@" in a subshell holding an exclusive kernel lock on the slot file, so two
# agents never read the same "next free" slot. flock(2) via perl, not flock(1):
# flock(1) is util-linux only, and on macOS `( flock -x 9 … )` ran its body
# UNLOCKED after "command not found" — eight concurrent allocators all got slot 1.
# A kernel lock is released when its holder dies, so there is no stale lock to
# recover; two userspace recovery schemes each left a window where two got in.
agent_slots_locked() {
  (
    if ! perl -MFcntl=:flock -e 'open(my $fh, ">&=", 9) or die "fd 9: $!\n"; flock($fh, LOCK_EX) or die "flock: $!\n"'; then
      echo "agent-slots: could not lock $(agent_slots_file).lock." >&2
      exit 1
    fi
    "$@"
  ) 9>"$(agent_slots_file).lock"
}

_agent_slot_for_path_unlocked() {
  local target="$1" file existing next tmp
  file="$(agent_slots_file)"
  existing="$(jq -r --arg p "$target" '.slots[$p] // empty' "$file")"
  if [[ -n "$existing" ]]; then
    echo "$existing"
    return 0
  fi
  next="$(agent_slot_next_free)"
  [[ -n "$next" ]] || { echo "agent-slots: no free slot left." >&2; return 1; }
  # Same directory as the slot file, so the rename is atomic.
  tmp="$(mktemp "$(dirname "$file")/slots.XXXXXX")"
  jq --arg p "$target" --argjson n "$next" '.slots[$p] = $n' "$file" > "$tmp" && mv "$tmp" "$file"
  echo "$next"
}

agent_slot_for_path() {
  agent_slots_init || return 1
  agent_slots_locked _agent_slot_for_path_unlocked "$1"
}

_agent_slot_release_unlocked() {
  local file tmp
  file="$(agent_slots_file)"
  tmp="$(mktemp "$(dirname "$file")/slots.XXXXXX")"
  jq --arg p "$1" 'del(.slots[$p])' "$file" > "$tmp" && mv "$tmp" "$file"
}

agent_slot_release() {
  agent_slots_init || return 1
  agent_slots_locked _agent_slot_release_unlocked "$1"
}

# First port of a slot's block of 100. 20000+, below the ephemeral range, and
# clear of the 18000+ blocks older per-repo allocators used.
agent_slot_base() {
  echo $((20000 + $1 * 100))
}

# Offsets inside a block, one per Firebase emulator. Explicit rather than "last two
# digits of the default" — auth (9099) and storage (9199) would collide.
AGENT_EMULATOR_OFFSETS='{"ui":0,"functions":1,"hosting":2,"eventarc":3,"tasks":4,"apphosting":5,"dataconnect":6,"database":9,"storage":19,"hub":50,"logging":51,"firestore":80,"pubsub":85,"auth":99}'
# Firestore's Emulator-UI websocket. Left unset, firebase-tools takes 9150 with
# portFixed=false and silently scans UPWARD on a collision, so two slots land on
# 9150/9151 — outside either slot's block and unreapable by a slot-scoped teardown.
AGENT_WEBSOCKET_OFFSET=90

# Identify what a directory is, relative to the worktree convention:
#   main:<abs-root>        — the main checkout
#   worktree:<abs-path>    — inside <worktreesDir>/<name>/
#   unknown                — anything else
agent_resolve_worktree() {
  local abs common_dir main_root wt_root rel
  abs="$(cd "$1" 2>/dev/null && pwd -P)" || { echo "unknown"; return 0; }
  # `--git-common-dir` is the MAIN .git whether we are in the main checkout, a
  # registered worktree, or a subdirectory of either.
  common_dir="$(git -C "$abs" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || {
    echo "unknown"
    return 0
  }
  main_root="$(cd "$(dirname "$common_dir")" && pwd -P)"
  wt_root="$main_root/$2"
  if [[ "$abs" == "$main_root" ]]; then
    echo "main:$main_root"
    return 0
  fi
  if [[ "$abs" == "$wt_root"/* ]]; then
    rel="${abs#"$wt_root"/}"
    echo "worktree:$wt_root/${rel%%/*}"
    return 0
  fi
  echo "unknown"
}

# Write <out> from <src> with every emulator moved into the slot's port block,
# plus the hub, logging and Firestore websocket ports, which firebase.json usually
# leaves to defaults. Setting them explicitly also makes firebase-tools treat them
# as fixed, so a collision fails loudly instead of drifting onto a free port.
agent_write_firebase_config() {
  local slot="$1" src="$2" out="$3" base unknown
  [[ -f "$src" ]] || { echo "agent-env: $src not found." >&2; return 1; }
  base="$(agent_slot_base "$slot")"
  unknown="$(jq -r --argjson off "$AGENT_EMULATOR_OFFSETS" '
    (.emulators // {}) | to_entries[]
    | select((.value | type) == "object" and $off[.key] == null) | .key' "$src")"
  if [[ -n "$unknown" ]]; then
    echo "agent-env: no slot offset for emulator(s): $unknown — add one to AGENT_EMULATOR_OFFSETS." >&2
    return 1
  fi
  jq --argjson off "$AGENT_EMULATOR_OFFSETS" --argjson base "$base" --argjson ws "$AGENT_WEBSOCKET_OFFSET" '
    .emulators = (
      (.emulators // {})
      | with_entries(
          if (.value | type) == "object" then .value.port = ($base + $off[.key]) else . end)
      | .hub = ((.hub // {}) + {port: ($base + $off.hub)})
      | .logging = ((.logging // {}) + {port: ($base + $off.logging)})
      | if .firestore then .firestore.websocketPort = ($base + $ws) else . end
    )' "$src" > "$out"
}
