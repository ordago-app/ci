#!/usr/bin/env bash
# Reclaim disk on a remote CI lane host. A BACKSTOP, no longer the primary cleanup.
#
# History, because it explains both the shape of this script and why it was demoted.
# The controller deletes a lane's work dir when it reaps the lane, but only on its OWN
# filesystem — it cannot see a remote host's disk. This script was therefore the only
# cleanup a lane host got. Its first version ran `find -mtime +1`, so a work dir had to
# be a full day old before it was eligible; on 2026-08-09 four PRs each fanned out to
# seven services across two workflows and the burst filled the 40 GB VM well inside that
# window, ENOSPC-ing every job until the VM lost networking. Eligibility then became
# LIVENESS, not age: a work dir whose lane container no longer exists is finished,
# whatever its mtime. That is the logic below, and it still runs.
#
# What changed is that lane hosts now set work_dir_mode=volume (ADR 0023), so a lane's
# workspace is an anonymous docker volume that dockerd drops together with the container
# — no host path, no timer, and cleanup that survives SIGKILL, an OOM kill and a dockerd
# crash, none of which run the entrypoint's EXIT trap. Nothing routine is left for this
# script to find. Keep it anyway: it reclaims volumes orphaned by a daemon crash (where
# auto_remove never fires), it clears anything left by a host still in bind mode, and it
# costs nothing when there is nothing to do. Do NOT let it become load-bearing again —
# a silent failure here should no longer be able to fill a disk.
#
# THE TRAP, do not remove: the socket proxy on this host denies IMAGES and BUILD
# (see ansible/playbooks/ci-lane-host.yml), so the controller can neither pull nor
# build the runner image. `docker system prune -a` or `docker image prune -a`
# would delete homelab/github-actions-runner:light with nothing able to restore
# it, and every subsequent admission would 404 — a silently dead host that still
# passes its health check. Only dangling/untagged layers are pruned here.
set -euo pipefail

WORK_DIR="${CI_LANE_WORK_DIR:?CI_LANE_WORK_DIR must be set}"

# Must equal LANE_LABEL in services/ci-controller/src/docker_adapter.py; a test
# asserts the two agree, because a rename there would silently turn this script
# into "delete every work dir".
LANE_LABEL="com.homelab.ci-controller.lane"

before="$(df -BM --output=avail "$WORK_DIR" | tail -1 | tr -dc '0-9')"

# Lanes docker still knows about, running or not — `docker ps -a`, because a lane
# that exited seconds ago may not have been reaped yet and its dir is not ours to
# delete. An empty/failed listing means we cannot prove anything is dead, so we
# delete nothing rather than guess.
#
# Keyed on the lane LABEL, not the container name. The container is named
# `github-runner-<lane_id>` (docker_adapter.py) while the work dir is
# `<lane_id>-work`, so comparing against names matches nothing and deletes the
# work dirs of RUNNING lanes — far worse than the full disk this script exists to
# prevent. The label carries the bare lane id and cannot drift with naming.
if ! live="$(docker ps -a --filter "label=${LANE_LABEL}" \
             --format "{{.Label \"${LANE_LABEL}\"}}" 2>/dev/null)"; then
  echo "docker unreachable; skipping work-dir prune" >&2
  live=""
  skip_workdirs=1
fi

if [ -z "${skip_workdirs:-}" ]; then
  shopt -s nullglob
  for dir in "$WORK_DIR"/*-work; do
    lane="$(basename "$dir")"
    lane="${lane%-work}"
    if ! printf '%s\n' "$live" | grep -qxF "$lane"; then
      echo "removing finished lane work dir: $dir"
      rm -rf -- "$dir"
    fi
  done
fi

# Stopped containers and dangling layers only. `image prune` without -a keeps every
# tagged image, which is what protects the runner image above.
docker container prune -f >/dev/null 2>&1 || true
docker image prune -f >/dev/null 2>&1 || true
docker builder prune -af >/dev/null 2>&1 || true

# Lane workspaces under work_dir_mode=volume. auto_remove drops a lane's anonymous
# volume with its container, so this finds nothing in the normal case — it exists for
# the one path that skips auto_remove entirely: dockerd dying mid-lane.
#
# Deliberately NOT -a, for the same reason `image prune` is not: without -a, docker
# removes only ANONYMOUS volumes (verified on the 29.x daemon these hosts run), which is
# exactly the set lanes create. With -a it would also take unused NAMED volumes, and a
# lane host that later runs anything with a named volume would find it deleted between
# jobs. The narrow form cannot make that mistake.
docker volume prune -f >/dev/null 2>&1 || true

after="$(df -BM --output=avail "$WORK_DIR" | tail -1 | tr -dc '0-9')"
echo "avail ${before}M -> ${after}M on $WORK_DIR"
