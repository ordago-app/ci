# 0026 — CI traffic runs on a dedicated fabric tailnet

**Status:** accepted
**Date:** 2026-08-25

## Context

[ADR 0016](0016-opportunistic-second-ci-host.md) added a second lane host, reached at
its personal-tailnet address with port 2375 published and restricted by an iptables
allowlist. That works for one operator. It does not extend to a second: pooling
requires a foreign dispatcher to open a connection to a local socket proxy, and doing
that over either operator's *personal* tailnet places a foreign component inside a
trust domain whose other members are private services.

[`docs/plans/ideas/federated-ci-pool.md`](../plans/ideas/federated-ci-pool.md)
decision 2 chose a third, dedicated tailnet for exactly this span.

## Decision

CI traffic runs on a **CI fabric** tailnet, `tail037f40.ts.net`, created under the
`ordago-app` GitHub organisation. Each participating machine runs one extra
`tailscaled` as a sidecar container; the component that needs the fabric shares its
network namespace.

**The sidecar's mode differs by direction.** This was measured, not assumed:

| Side | Mode | Why |
|---|---|---|
| lane host (socket proxy) | `TS_USERSPACE=true`, no `NET_ADMIN` | only *accepts* connections |
| dispatcher | `TS_USERSPACE=false` + `NET_ADMIN` + `/dev/net/tun` | *originates* them |

A container sharing a **userspace** tailscaled's namespace cannot open outbound
connections to tailnet IPs at all — `curl` to a peer times out and only tailscaled's
own SOCKS5 proxy works, which `docker-py` cannot use for a `tcp://` endpoint. With a
tun device the same request returns 200. The privilege therefore lands on the machine
that does **not** run untrusted job code.

Node identity is a Tailscale ACL **tag**, and the ACL is generated from each host's
`allowed_orgs` — the same fact the scheduler filters placement on, so placement can
never grant reach the network refuses.

## Consequences

- A lane host's socket proxy **publishes no host port**. It listens only inside the
  sidecar's namespace. This deleted `CI_LANE_BIND_ADDR` and its guard, whose failure
  mode was publishing an unauthenticated container-create API on `0.0.0.0`, and
  structurally removed the 2026-08-09 incident where docker raced tailscaled for the
  bind address and gave up.
- Verified on the lane host: a container on the lane network cannot reach the proxy by
  fabric name, docker DNS, or host gateway, while outbound internet still works.
- The iptables allowlist on lane hosts is **kept** rather than removed. The namespace
  boundary should make it redundant, and the measurement above supports that, but it
  costs nothing and this repo's incident log is largely made of removals justified by
  reasoning rather than evidence.
- Onboarding a lane host is documented in [the runbook](../runbook.md); every failure
  mode it lists reports success, so its verification steps are not optional.

## Known gap, not introduced here

**On `powerserver`, a lane container can reach the socket proxy.** Lanes default to
`docker_network: homelab`, `ci-controller-socket-proxy` is on that same network, and
`DOCKER-USER` is empty. Measured 2026-08-25: `http://docker-socket-proxy:2375/version`
returns 200 from a container on `homelab`.

That is a container-create API, so a lane could start a container bind-mounting `/`.
It predates this ADR — the proxy has always shared that network with lanes — and the
lane-host firewall ADR 0016 added exists precisely to close the same hole on a *lane
host*, which makes the asymmetry hard to defend now that it is written down.

Two candidate fixes, neither taken here: move powerserver's proxy into the `ci-fabric`
namespace so it leaves `homelab` (which also prepares stage 4, where a foreign
dispatcher must reach it), or add a `DOCKER-USER` rule mirroring the lane host's. This
needs its own decision rather than being folded into a networking change.

## Alternatives rejected

- **Reuse either production tailnet.** Membership is the wrong granularity: it puts a
  foreign component inside a trust domain of private services, leaving ACLs as the only
  thing between them.
- **mTLS over public ingress.** Removes the tailnet question, adds public ingress and a
  CA lifecycle to a repo that deliberately has neither ([ADR 0002](0002-path-based-routing.md)).
- **Userspace sidecars everywhere**, as the spec originally specified. Measured not to
  work for the dispatcher; would have shipped a dispatcher unable to reach any lane host.
