# ci

Federated CI platform shared by two operators. One pool, one ledger, two
organisations, machines owned and administered separately.

- **Dispatcher** — per org, holds that org's GitHub App key, spawns lanes.
- **Scheduler** — exactly one, credential-free, owns placement and the ledger.
- **Lane host** — any machine offering capacity: scoped Docker socket proxy plus
  a CI-fabric sidecar. Holds no secrets.

Start at [`AGENTS.md`](AGENTS.md), then
[`docs/decisions/0100-one-scheduler-one-ledger.md`](docs/decisions/0100-one-scheduler-one-ledger.md).

Consumers pin this repo **by commit SHA**. That pin is what keeps deployment
authority with each machine's owner.
