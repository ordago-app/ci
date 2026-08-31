# The SHA pin does not gate ci-controller

**Priority:** high

## The problem

`AGENTS.md` invariant 5 says a commit here "reaches nobody's hardware until that
machine's owner deploys it. Consumers pin this repo **by commit SHA, never by
branch** — the pin *is* the boundary."

For `ci_lane_host` that is true. For **ci-controller it is not**. The dispatcher
and scheduler running on the first operator's pool are built from a full copy of
`services/ci-controller/` living in that operator's private homelab repo, copied
to the host and built in place by its `services.yml`:

```yaml
- name: ci-controller compose file
  ansible.builtin.copy:
    src: ../../services/ci-controller/compose.yml   # homelab's own copy
- name: ci-controller build context (Dockerfile + src/ + pyproject.toml + uv.lock)
  ansible.posix.synchronize:
    src: ../../services/ci-controller/
```

The SHA pin in that repo's `ansible/requirements.yml` governs only the
collection, and the collection's `ci_controller` role is not what deploys the
dispatcher there. Nothing in the path reads this repository.

## Why it matters

1. **Changes landed here do not ship.** Discovered on 2026-08-31 while deploying
   ordago-app/ci#10 — the fix had to be copied into the other repo by hand before
   it could reach a host. Every commit to `services/ci-controller/` here is dead
   code with respect to that pool until someone remembers to port it.

2. **The merge bar rests on the claim.** `.agents/land.config.json` justifies
   `requireApprovingReview: false` partly on invariant 5: "nothing on main reaches
   anyone's hardware until that machine's owner bumps their SHA pin. The pin is the
   real gate." Where there is no pin in the path, that sentence is not describing
   this repository's actual deployment topology.

3. **Silent drift decides correctness.** As of 2026-08-31 the two copies were
   byte-identical apart from the change being ported, so the port was clean. That
   is luck, not a mechanism. The first change made in one copy and not the other
   drifts with no test, no CI signal, and no error — and the direction of the drift
   decides whether a pool is running reviewed code.

## Options

1. **Consume the collection, like `ci_lane_host` already does.** The consumer
   deletes its `services/ci-controller/` copy and calls `ordago.ci.ci_controller`,
   pinned by SHA. The role exists and already takes the disk layout as input
   (175b6f7). This makes invariant 5 true rather than aspirational, and is the only
   option that removes the duplication rather than policing it.

2. **Keep both copies, add a drift check.** A CI job that fails when the two
   diverge. Cheaper, but it needs to read a private repo from this public one,
   which is the same structural limit that already blocks the reviewer
   (`land.config.json`) and the collection install in the consumer's own CI.

3. **Do nothing, port by hand.** Status quo. The failure mode is silent and the
   detection path is a person noticing CI behaves unlike the code.

Option 1 is the recommendation. Option 3 is what happens by default, which is the
argument for writing this down.

## Open questions

- Does `roles/ci_controller/` reach parity with what the consumer's inline tasks
  do today? Both carry the work-dir prune systemd units — the inline copy with
  hardcoded paths, the role templated. A task-by-task diff is the first work item.
- The second operator's deployment path was never inline — confirm they already
  consume the role, so this is one consumer to migrate rather than two.

## Verification

The plan retires when the consumer's `services/ci-controller/` is gone, its
`services.yml` calls `ordago.ci.ci_controller`, and a deliberate commit here plus
a pin bump there is the only way to change what runs on a host.
