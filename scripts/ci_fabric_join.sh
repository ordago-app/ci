#!/usr/bin/env bash
# Join a lane host to the CI fabric tailnet.
#
#   scripts/ci_fabric_join.sh <host>
#
# The key is read from secrets/secrets.vm.yml on the OPERATOR's workstation and
# consumed by `tailscale up` inside the sidecar on the guest, leaving only
# tailscaled state in the volume. It is never written to the lane host's disk,
# which is what ADR 0016/0017 require -- the operator holding one is fine, and is
# already how `make ci-lane-up` obtains the personal-tailnet key.
#
# Read from the file rather than prompted: the VM gets rebuilt often enough that
# an interactive step here is one someone eventually skips, and this is the step
# that decides whether the host is reachable by its dispatcher at all.
#
# The join sets NO non-default preferences -- not even a hostname. containerboot
# runs its own `tailscale up --accept-dns=false --hostname=...` when the sidecar
# starts, and `tailscale up` refuses to change settings without mentioning every
# non-default one already set:
#
#     Error: changing settings via 'tailscale up' requires mentioning all
#     non-default flags.
#
# The sidecar then SIGTERMs itself in a restart loop. Leaving prefs at their
# defaults here makes containerboot the single owner of them; this volume carries
# only the node identity. Hit on 2026-08-25 deploying powervaro-ci.
#
# A script rather than a Makefile recipe: make runs recipes under /bin/sh (dash),
# where the bash-isms this needs do not exist. See scripts/secrets_set.sh.
set -euo pipefail

HOST="${1:?usage: scripts/ci_fabric_join.sh <host> [ssh-user]}"
# The login on the target. An argument, not a constant: this is a shared platform
# and a second operator's machines do not carry the first operator's username.
# Defaults to $USER, which is right for the common case of joining your own host.
SSH_USER="${2:-${USER:?set USER or pass an ssh-user argument}}"
VM_SECRETS="secrets/secrets.vm.yml"
TS_IMAGE="tailscale/tailscale:v1.102.2"

test -f "$VM_SECRETS" || {
  echo "no $VM_SECRETS — see docs/multipass-test.md" >&2
  exit 1
}

KEY="$(VM="$VM_SECRETS" python3 -c '
import os, sys, yaml
key = (yaml.safe_load(open(os.environ["VM"])) or {}).get("ci_fabric_auth_key") or ""
sys.stdout.write(key)
')"
test -n "$KEY" || {
  echo "ci_fabric_auth_key missing from $VM_SECRETS —" >&2
  echo "  make secrets-set KEY=ci_fabric_auth_key FILE=$VM_SECRETS" >&2
  exit 1
}

# The key goes over stdin, not argv: an argv would be visible in `ps` on the guest.
printf '%s' "$KEY" | ssh "${SSH_USER}@${HOST}" 'read -r K; docker run --rm \
  -v ci-fabric-state:/var/lib/tailscale \
  -e TS_KEY="$K" '"$TS_IMAGE"' sh -c "
    tailscaled --tun=userspace-networking --state=/var/lib/tailscale/tailscaled.state \
      --socket=/tmp/ts.sock >/dev/null 2>&1 &
    for i in \$(seq 1 40); do [ -S /tmp/ts.sock ] && break; done
    tailscale --socket=/tmp/ts.sock up --authkey=\"\$TS_KEY\"
  "'

echo "==> ${HOST} joined the CI fabric; bring the stack up with the ci-lane-host play"
