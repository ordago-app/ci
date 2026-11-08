# Federated CI pool across two operators

**Priority:** medium

Moved here from the first operator's homelab repo on 2026-11-08. Links to ADRs this repo
does not carry name them as homelab's.

## Goal

Let `powerserver`, `powervaro-ci`, and a cofounder-owned always-on server form **one CI
pool** serving both the Ordago org and Álvaro's personal repos — sharing one
implementation, one admission ledger, and one capacity model — **without** the cofounder
gaining access to the private homelab, and without either operator's production tailnet
being exposed to CI.

It superseded homelab's `multi-repo-runner-pool` idea (deleted 2026-11-08), which solved the
narrower single-operator case (one machine, several repos, one owner).

## Context

[ADR 0012](../../decisions/0012-powerserver-ci-scratch-nvme-and-ram-ceiling.md) established
that `powerserver` is a capacity dead end; homelab ADR 0016
added `powervaro-ci` as opportunistic relief under a **single controller with a single
ledger**, and [ADR 0017](../../decisions/0017-ci-lane-host-as-an-opt-in-vm.md) made that
host an opt-in Multipass VM.

Two facts changed:

1. A cofounder now runs an always-on VPS and is a **co-operator on equal footing** — he
   deploys, holds secrets, and operates the CI platform, rather than donating a machine
   Álvaro administers.
2. Ordago CI and personal CI want the same machinery, but the homelab repo also contains
   private services, the personal MCP ACL, and personal secrets. None of that may be
   shared.

Today's `ci-controller` cannot serve this: it holds the GitHub App key **and** owns the
ledger in one process, so any shared deployment of it is a shared credential store for two
organizations.

## Non-goals

- Replacing the homelab's non-CI services. They stay in homelab until a second consumer
  actually exists. Extracting a general
  "homelab platform" is deferred: designing a reusable interface against one user is how
  neutrality decays.
- Public ingress. Everything stays tailnet-only (homelab ADR 0002).
- Distributed admission. There remains exactly one ledger.
- Multi-environment release tiers. Still single-tier.

## Decisions

### 1. Split `ci-controller` along the credential line

Two components, split precisely where the trust boundary is:

| Component | Instances | Holds credentials | Responsibility |
|---|---|---|---|
| **Dispatcher** | one per org, in that org's trust domain | yes — that org's GitHub App key | polls its own org's job queue, requests capacity, mints the JIT runner token, spawns the lane on the assigned host, releases the reservation |
| **Scheduler** | exactly one, neutral | **no** | host inventory, health checks, reservation ledger, capacity gates, metrics DB, placement decisions |

The scheduler never contacts GitHub, never sees job contents, and cannot authenticate as
either organization. Compromising it yields scheduling denial and job metadata — not the
ability to write to either org's repositories.

ADR 0016's invariant survives verbatim: **one scheduler, one ledger, one `events` table.**
Admission stays globally consistent, host selection stays headroom-based with a
deterministic name-ascending tie-break, and `ci-bench`'s single-join measurement model is
unchanged.

*Rejected: one controller holding both orgs' App keys.* Simpler, but makes a single
compromise a two-organization repository-write incident.

*Rejected: two independent controllers with a lease protocol.* Preserves credential
separation and pooling, but replaces a solved problem (one ledger) with distributed
admission — the hardest thing in this design — and breaks the single-join model.

### 2. Three tailnets; CI runs on a dedicated fabric

| Tailnet | Members | Never contains |
|---|---|---|
| personal | `powerserver` (host), private homelab services | anything Ordago |
| ordago production | Ordago services, cofounder's server (host) | CI workloads |
| **CI fabric** | both dispatchers, the scheduler, every lane host's socket proxy | anything not CI |

Pooling requires *some* shared fabric — a foreign dispatcher must open a connection to a
local socket proxy for its jobs to run on local hardware. The design choice is only which
component spans. A dedicated fabric puts the span in a purpose-built place: neither
production tailnet is ever a party to CI, and a CI compromise reaches CI and stops.

Each participating machine runs **one additional `tailscaled`** as a sidecar container,
with the component that needs the fabric sharing its network namespace
(`network_mode: "service:<sidecar>"`). The machine's own tailnet membership is untouched.

**The sidecar's mode differs by direction, and this was measured rather than assumed**
(2026-08-25, against the real fabric):

| Side | Mode | Why |
|---|---|---|
| lane host (socket proxy) | `TS_USERSPACE=true`, no `NET_ADMIN` | only *accepts* connections, which userspace handles |
| dispatcher | `TS_USERSPACE=false` + `NET_ADMIN` + `/dev/net/tun` | *originates* connections, which userspace cannot do |

A container sharing a **userspace** tailscaled's namespace cannot open outbound
connections to tailnet IPs at all — `curl` to a peer times out (exit 28), and only
tailscaled's own SOCKS5 proxy works, which `docker-py` cannot use for a `tcp://`
endpoint. With a tun device a `tailscale0` interface appears in the namespace and the
same request returns 200. This section previously said `TS_USERSPACE=true` for
everything, which would have shipped a dispatcher that could not reach any lane host.

The privilege lands where it costs least: the lane host, which runs untrusted job code,
keeps userspace and no `NET_ADMIN`.

The dispatcher's sidecar also joins the `homelab` docker network and sets
`dns: 100.100.100.100`, which puts MagicDNS *behind* docker's embedded resolver rather
than in front of it — verified that docker service names and tailnet FQDNs then both
resolve. Short tailnet names do not (no search domain), so lane-host endpoints are
written as FQDNs.
`powerserver` therefore runs two daemons: the host-level one on the personal tailnet
exactly as today, and the sidecar on the fabric.

Because every machine looks identical from the fabric's perspective, the platform never
encodes which private tailnet a host also belongs to.

*Rejected: reuse either production tailnet.* Membership is the wrong granularity — it
places a foreign component inside a trust domain whose other members are private services,
leaving ACLs as the only thing between them.

*Rejected: mTLS over public ingress.* Removes the tailnet question but adds public ingress
and a CA lifecycle to a repo that deliberately has neither.

### 3. Lane containers are not on the fabric

A lane is untrusted job code. It gets plain bridge networking with outbound NAT — enough to
reach GitHub, npm, PyPI — and **no fabric membership**. This removes any lateral path from
job code to a socket proxy, which would otherwise let a lane spawn containers across the
whole pool.

Jobs that genuinely need private access (a staging deploy, a private registry) declare it
per lane class in **tenant** config, which attaches that tenant's own tailnet sidecar to
those lanes only. Least privilege by default; extensions explicit, per-class, per-tenant.

### 4. Two repositories, with deployment authority per operator

```
ordago-app/ci         shared, both write    platform code + joint pool config
                                            (hosts, classes, budgets, dispatchers,
                                             scheduler, fabric ACL, ADRs, runbook)

powervaro/homelab     private, Álvaro only  private services + personal config;
                                            contributes powerserver as a lane host
```

The CI pool is **one system, not two tenants** — one scheduler, one ledger, one fabric,
serving two orgs. Its config therefore lives in one tree, which satisfies the agent-centric
requirement: every machine, class, budget and dispatcher is visible in one place, next to
the code that acts on it.

What stays separate is **deployment authority**: each operator provisions only their own
machines from the shared code. A cofounder commit cannot reach `powerserver` until Álvaro
deploys it — a reviewed, deliberate act — which delivers the supply-chain property without
a third repository and without the cross-repo tax on agents.

The homelab repo stops running `ci-controller` entirely. It keeps a lane-host role: socket
proxy plus fabric sidecar.

*Rejected: a third neutral platform repo.* Strictly better isolation for a hypothetical
third participant, at the cost of three trees, three `AGENTS.md`, no cross-repo grep, and a
two-PR loop for every CI fix. Revisit if a third operator ever appears.

### 5. Identity on the fabric is infra-backed, via Tailscale ACL tags

Every node is tagged: `tag:ci-dispatcher-ordago`, `tag:ci-dispatcher-personal`,
`tag:ci-scheduler`, `tag:ci-lane-host-<name>`. ACLs permit only dispatcher→socket-proxy and
dispatcher→scheduler. Nothing else routes.

The scheduler derives the caller's identity from the tailnet connection rather than from a
header or bearer token — no shared secret to store, leak, or rotate. Per homelab's
`enforce-trust-boundary` skill this is an infra-backed gate, preferred over an app-level one for a predicate the
infrastructure can enforce natively.

The ACL policy file is **version-controlled in the shared repo and applied from CI** — never
edited in the Tailscale admin console, which would put the system's real access rules
outside the repo an agent can read.

### 6. Per-host org allow-list generates the ACL

Each host declares which orgs may use it, alongside the `allowed_classes` ADR 0016 already
established, using the same inheritance pattern.

```yaml
hosts:
  powerserver:
    docker_endpoint: ...
    allowed_classes: [light, node, emulator]
    allowed_orgs: [ordago, personal]
  powervaro-ci:
    allowed_classes: [light]
    allowed_orgs: [ordago, personal]
  cofounder-vps:
    allowed_classes: [light]
    allowed_orgs: [ordago]
```

This single fact has two consumers: the scheduler's placement filter, and the **generated
tailnet ACL** that permits only the listed orgs' dispatcher tags to reach that host's socket
proxy. Placement logic therefore cannot grant access the fabric does not already allow — a
buggy or compromised dispatcher that skips the scheduler and dials a socket proxy directly
is refused at the network layer.

Default: the cofounder's machine is Ordago-only. Changing it is one line plus a regenerated
ACL, not a redesign.

### 7. Secrets stay in each operator's own store

The shared repo commits the **schema** (`secrets.example.yml`), the recipient list
(`.sops.yaml`) and the **ciphertext** (`secrets.enc.yml`) — never a *plaintext* value.
The ciphertext is what makes the store reproducible from a clone; omitting it would
reduce both operators to passing secrets out of band. Each
operator's real secrets stay in their own sops+age store
(homelab ADR 0011): Ordago's App key in the shared
repo's encrypted file with both operators as age recipients; the personal dispatcher's App
key in homelab, where the cofounder has no access and no recipient key.

Lane hosts continue to hold **no secrets at all** (ADR 0016/0017). The JIT runner token is
minted by the dispatcher and injected at spawn; it is short-lived and single-use.

## Data flow

```
1. push to <org>/<repo>            GitHub queues a job

2. <ORG> DISPATCHER polls GitHub                        public internet
   sees the queued job

3. <ORG> DISPATCHER  ->  SCHEDULER                      CI fabric
   "capacity for a light lane"
   "use powerserver, reservation #123"                  bookkeeping only

4. <ORG> DISPATCHER mints a JIT runner token            public internet

5. <ORG> DISPATCHER  ->  powerserver socket proxy       CI fabric
   create + start lane, token injected                  allowed by ACL tag

6. lane registers as an ephemeral runner, runs, exits   outbound NAT only

7. reservation #123 released; event written to ledger
```

Per tick the scheduler health-checks every host; a failing host is skipped for admission and
its in-flight lanes are reaped as `infra_failure`, unchanged from ADR 0016.

## Trust boundaries

| Boundary | Enforced by | Failure mode if wrong |
|---|---|---|
| foreign dispatcher → local docker | socket proxy allow-list (`CONTAINERS`/`POST`/`EVENTS` only; `EXEC`, `IMAGES`, `VOLUMES`, `NETWORKS`, `SECRETS`, `BUILD`, `SYSTEM` denied) | container creation is the ceiling; no exec, no secrets, no volume mounts |
| which dispatcher may reach which host | generated Tailscale ACL from `allowed_orgs` | cross-org spawning on a machine that excluded that org |
| lane → anything private | lanes are off the fabric; outbound NAT only | untrusted job code with lateral reach across the pool |
| org A reserving as org B | scheduler reads tailnet identity, not a header | ledger corruption, capacity theft |
| cofounder commit → powerserver | per-operator deployment authority; reviewed deploy | unreviewed code on the private host |
| private homelab content | never enters the shared repo | disclosure |

## Testing

- **Unit** (pytest, per `services/AGENTS.md`): scheduler placement given synthetic host
  state; `allowed_orgs` filtering; reservation lifecycle including release-on-crash;
  dispatcher token minting with a mocked GitHub App.
- **Wiring**: assert the generated ACL matches `allowed_orgs` for every host — the same
  shape as the existing test asserting `ci-lane-host`'s proxy capabilities match the
  controller's.
- **Ansible**: `--check --diff` idempotency for the sidecar + socket-proxy role; a re-run is
  a no-op.
- **Integration** (`test/` multipass harness): two mock dispatchers against one scheduler and
  two lane hosts; assert an Ordago dispatcher cannot reach a `personal`-only host, and that a
  lane container cannot reach any socket proxy.

## Migration

Staged, so nothing is rebuilt before it is needed. Each stage leaves a working system.

| Stage | Work | Ends with |
|---|---|---|
| 0 | *(done)* Convert `alvaro-francisco-gil/ordago-apps` to a real GitHub org; add cofounder | equal footing is possible at all |
| 1 | *(done, homelab#175)* Split the controller **inside homelab**: extract the scheduler; today's controller becomes the personal dispatcher | two components, one org, no behaviour change |
| 2 | Create `ordago-app/ci`; move `ci-controller`, `github-actions-runner`, `ci-lane-host`, `github-review` and the plays they need; homelab consumes them as a pinned ansible collection. Done for `ci-lane-host`; the rest is [`../ongoing/one-copy-of-the-ci-platform.md`](../ongoing/one-copy-of-the-ci-platform.md) | shared code, unchanged behaviour |
| 3 | Stand up sidecars and the tagged ACL generated from `allowed_orgs`, on the fabric tailnet *(created 2026-08-25)* | dispatcher↔host traffic on the fabric |
| 3a | *(done, homelab#177)* lane-host allowlist reads `ci_lane_allowed_dispatchers` from config instead of naming one host | a lane host can admit more than one dispatcher |
| 4 | Cofounder's VPS joins as a lane host (`allowed_orgs: [ordago]`); Ordago dispatcher deployed in Ordago's trust domain | pooling live |
| 5 | Move lanes off any private tailnet; per-class opt-in network extras | least-privilege lanes |

~~Stage 1 is the only genuine refactor of working code; stages 2 and 3 are moves and
additions.~~ **This was wrong, twice over, and is corrected here rather than left for a
reader to trip over.** Stage 2 also required (a) neutralising five source defaults that
named one operator's machine, and (b) building an ansible collection seam that had no prior
art in the repo — `ansible/roles/` was empty and every play was flat. Stage 3 likewise
turned out to need a measured userspace-vs-tun distinction the design had assumed away.
Read [`../ongoing/one-copy-of-the-ci-platform.md`](../ongoing/one-copy-of-the-ci-platform.md)
before treating any remaining stage as "just a move".

**Stage 2 left two copies of the platform** (found 2026-09-28 for `github-review`, 2026-11-07
for the rest): the consumer kept deploying its own trees and development kept landing
there. Ending that is [`../ongoing/one-copy-of-the-ci-platform.md`](../ongoing/one-copy-of-the-ci-platform.md).

**Why the refactor comes before the extraction** (reordered 2026-08-22, after reading the
code): `Controller.reconcile()` simultaneously reads GitHub, mutates the ledger and reaps
containers, so the credential line cuts straight through the reconcile loop — this is a real
refactor of ~3.6k LOC with 5.5k LOC of tests around it, not a file move. Doing it inside
homelab keeps it where the pytest harness already runs and where nothing is blocked on the
org conversion. Moving the result afterwards is `git mv` plus a collection wrapper. Shipped
in homelab#175; see [ADR 0025](../../decisions/0025-ci-scheduler-dispatcher-split.md).

## Operator prerequisites

Stage 0 (the GitHub org) is **done**: `ordago-app/ordago-apps` exists and all three Apps —
runner, developer, reviewer — resolve their own installation on it (verified 2026-08-24).

Stage 3's own prerequisite — a dedicated tailnet — is **satisfied** (2026-08-25). A tailnet
belongs to an account and one login gets exactly one, so the fabric could not reuse
the first operator's personal tailnet; it was created under the
`ordago-app` **GitHub organisation** instead, and its suffix is **`tail037f40.ts.net`**.
Stage 3 is planned in homelab's `ci-fabric-tailnet` plan.

### Tailscale plan mechanism

Worth recording, because it is not obvious and it cost a round of investigation: Tailscale
assigns the billing plan **at signup, from the identity type**, not from how many tailnets
an operator owns. A public-domain identity (Gmail, or a *personal* GitHub account) lands on
the free Personal plan; a custom-owned domain — and a GitHub **organisation** counts as one
— is enrolled in a 14-day Enterprise trial that otherwise converts to paid. Creating the
fabric under `ordago-app` therefore produced a trial, which was downgraded to Personal.
Downgrading requires ≤6 users and that the tailnet not be judged commercial; the option was
offered and taken while Ordago is pre-revenue. **Revisit if the app starts earning** — the
correct answer for company infrastructure is then to pay for the org tailnet (Standard,
$8/user/month), not to keep a Personal plan that no longer fits.

### Constraints this places on stage 3

- **Design against Personal-tier limits, not against what the console shows today.** The
  downgrade takes effect at the end of the billing period, so Enterprise features remain
  visible until then and anything built on them would break at the drop.
- Personal includes ACLs and device tags, and caps **ACL groups at 3**. Decision 5 puts
  identity on *tags* (`tag:ci-dispatcher-*`, `tag:ci-scheduler`, `tag:ci-lane-host-*`), so
  the cap is a boundary to respect rather than an obstacle — but a future ACL must not
  reach for `group:` where a tag will do.
- Personal permits **6 users**. Two operators today; a third participant is well within it.

### The remaining blocker is hardware, not networking

With the tailnet in hand, nothing about stage 3 is gated on Tailscale any more. What stage 3
*buys* is isolation between two operators' CI on shared machines — and that value only
exists once a second participant can actually host lanes. Open question 2 below is therefore
the real gate: a shared-vCPU VPS is a good control-plane host and a poor lane host. Building
the fabric before there is a second lane host isolates a pool of one from nobody.

Nothing in stage 3 is blocked *conceptually* — the sidecars, the tagged ACL and the
`allowed_orgs` schema can all be written first — but a trust boundary with no second party
cannot be meaningfully verified, and shipping an unverifiable trust boundary is what this
repo's incident log is largely made of. homelab#177 deliberately landed only the part that stands
alone and could be checked against a real host.

## Open questions

1. **Scheduler placement.** Deploy-time config, deliberately not fixed here. Start on
   `powerserver`; the cofounder's VPS or a GCE e2-micro are both valid later. Invariants:
   always-on, credential-free, reachable by every dispatcher, able to health-check every
   host. Cloud Run is disqualified — the ledger needs persistent local state.
2. **Is the VPS lane capacity at all?** Provisionally ~8 GB RAM, to be confirmed. A shared-vCPU Hostinger VPS is a good control-plane
   host and a poor lane host. If so, the "pool all the resources" goal still awaits real
   hardware, and Stage 4 delivers less than it appears to.
3. **Ledger visibility.** One `events` table holds both orgs' repo names, timings and
   outcomes, readable by both operators. Assumed acceptable between cofounders; revisit if a
   third participant appears.
4. **Personal dispatcher placement.** Runs in Álvaro's trust domain, but which host — and
   does it need the always-on guarantee the Ordago one does?
5. **Who health-checks hosts after the split?** Today `reconcile()` derives host health
   from `docker.snapshot()`, which the dispatcher owns. A scheduler that pings hosts itself
   would need socket-proxy reach on every host — which would let a credential-free scheduler
   spawn containers pool-wide, defeating decision 1. Options: dispatchers report health
   (whose report wins with two dispatchers?), or a ping-only proxy port for the scheduler.
   Must be resolved before the scheduler moves off-box; irrelevant while it is in the same
   compose stack.
6. **Runner registration scope.** Org-level tokens need the org from Stage 0; per-repo
   registration is the fallback and silos nothing here, since siloing now happens at the
   scheduler rather than at registration.
