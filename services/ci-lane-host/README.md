# ci-lane-host

An **opportunistic CI lane host**: a machine that lends spare capacity to the
homelab's CI pool without becoming part of its trusted core.

`powervaro-ci` — an **opt-in** Multipass VM on the operator's desktop, off unless
lent — is the first one. See
[ADR 0017](../../docs/decisions/0017-ci-lane-host-as-an-opt-in-vm.md), and
[ADR 0016](../../docs/decisions/0016-opportunistic-second-ci-host.md) for the
controller-side design it still governs.

## What a lane host runs

Docker, sshd, and the scoped `docker-socket-proxy` in [`compose.yml`](compose.yml).
That is the whole inventory.

## What a lane host deliberately does not have

| Not here | Why |
| --- | --- |
| A second `ci-controller` | Two controllers would each see the same queued job against separate ledgers and both spawn a lane, leaving orphan ephemeral runners. One controller, one ledger, one metrics DB. |
| The runner GitHub App private key | The controller mints registration tokens on powerserver and passes them in per lane. A lane host never authenticates as the App. |
| Any file under `secrets/` | Nothing here needs decrypting, so nothing here can leak. |
| The metrics DB | All hosts' events land in powerserver's single `metrics.db`, which is what makes `make ci-report` a fleet-wide view. |
| A raw docker socket exposed to the controller | The controller talks to the scoped proxy, which permits container create/start/list/remove and denies exec, images, volumes, networks and secrets. |

## Provisioning

Two steps, both operator-run — see the "Second CI lane host" section of
[`docs/runbook.md`](../../docs/runbook.md) for the full sequence:

1. `make ci-lane-up`, which creates the VM and joins it to the tailnet. It cannot
   be an ansible play: there is nothing to SSH into until it has run. The same
   command later just starts the VM, so it is also how the machine is lent.
2. `make ci-lane-host host=powervaro-ci`, which runs
   `ansible/playbooks/ci-lane-host.yml` — docker network, work dirs, the runner
   image, the proxy stack, and the stale-work-dir prune timer.

## The runner image must already be here

The proxy denies `IMAGES` and `BUILD`, so the controller can only **run** an
image that is already present on this host — it can never pull or build one.
The playbook builds `homelab/github-actions-runner:light` locally for exactly
that reason.

If it is missing, every admission to this host fails with a 404 from the docker
API, which reads like a controller bug and is not one. The controller reaps
those lanes as `infra_failure`, so the symptom is visible in
`docs/ci-capacity-report.md` under this host's row.

## Taking a lane host out of service

Set `enabled: false` on its entry in the `hosts:` map of
`personal/ci-controller.yml` and redeploy the controller. Admission skips it
immediately; its in-flight lanes are reaped as `infra_failure` on the next tick.

Simply shutting the machine down also works and is safe — the controller
health-checks every host each tick and skips the ones that do not answer. That
is the designed steady state for a desktop that sleeps at night, not an error.
