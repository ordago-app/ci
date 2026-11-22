# The fabric ACL generator runs from this repo, on the operator's own config

**Priority:** high — the fabric ACL is the only control on a lane host's port 2375, and issue #7 (the other operator, open since 2026-08-30, no reply) is blocked on it to add a lane host
**Gate:** decision:Who applies the ACL to the shared fabric tailnet — the operator who runs the scheduler, from their own repo, or this repo, from a committed input and a workflow?

**Goal:** `scripts/gen_tailscale_acl.py` takes the pool config, the repo registry and the output path as arguments, is tested here, and every doc says the same thing about who generates and who applies the ACL — so adding a machine stops depending on files this repo does not contain.

## Context

As of `30c5e5c` (drafted 2026-11-21, re-checked against `origin/main` 2026-11-22).

- `scripts/gen_tailscale_acl.py` resolves three constants against this repo's root:
  `CONTROLLER_CONFIG = personal/ci-controller.yml`, `REPO_REGISTRY = personal/repos.yml`,
  `ACL_PATH = tailscale/acl.hujson`. None of the three exists on `origin/main`; running
  the script here ends in `FileNotFoundError`. The script has not changed since it
  arrived (`4c5f93f` ci-fabric: config, allowed_orgs placement filter, and the
  generated tailnet ACL (#182)).
- Its docstring says the output "is COMMITTED and applied from CI. Never edit the
  Tailscale admin console by hand". No workflow here applies an ACL:
  `.github/workflows/` holds `ansible`, `gitleaks`, `images`, `mypy`, `pytest`, `ruff`.
  `docs/plans/ideas/federated-ci-pool.md` decision 5 says the same ("version-controlled
  in the shared repo and applied from CI"), and `services/ci-lane-host/compose.yml`
  names `tailscale/acl.hujson` as what decides reachability.
- It has no test: `scripts/tests/` holds `test_ci_status.py` and
  `test_validate_pool_config.py` (`ci_metrics_snapshot.py` has none either, but it is
  not an access control). The consumer's pre-extraction copy does have one, written
  against that consumer's own committed files, so it did not cross. The wiring test
  `federated-ci-pool.md` § Testing asks for ("assert the generated ACL matches
  `allowed_orgs` for every host") does not exist here.
- The ACL carries the weight alone. `README.md` (lane-host role): "There is no
  `ci_lane_allowed_dispatchers` and no host firewall … If you add a machine, the ACL is
  what you edit." `HostConfig.allowed_orgs` in `services/ci-controller/src/config.py`:
  "The SAME fact generates the fabric's tailnet ACL … so placement can never grant
  reach the network refuses." `.agents/land.config.json` lists the generator under
  `hardStop` as "the CI fabric's only access control (ADR 0026)".
- What actually happens today: the consumer that runs the scheduler still carries its
  own pre-extraction copy of the generator and its test, generates the ACL from its
  own pool config, commits it there, and pastes it into the admin console by hand (its
  own runbook says so). So
  the working mechanism is per operator and manual, and neither is what this repo's
  text says.
- The input cannot be found here by design: this repo "ships no pool config for you"
  (`README.md`), and the host inventory with `allowed_orgs` lives in the config of
  whoever runs the one scheduler (invariant 1).
- The sibling script already has the right contract. `scripts/validate_pool_config.py`:
  "Usage: validate_pool_config.py <ci-controller.yml> <repos.yml>", run by a consumer
  "from the installed collection". `galaxy.yml` does not ignore `scripts/`, so the
  generator ships in the collection too.
- Two links to the onboarding sequence are dead: ADR 0026 and
  `services/ci-lane-host/README.md` both point at `docs/runbook.md`, which did not
  cross at the extraction.
- One correction to issue #7: the fixtures under
  `services/github-actions-runner/tests/fixtures/` are a runner config with no
  `hosts:`, so they cannot feed this test. `test_validate_pool_config.py` builds its
  pool inline in `tmp_path`; the new test does the same.

## Design

**The generator is a tool the consumer points at its own files; this repo owns its
behaviour and its tests, and commits no ACL.**

- **Arguments, no defaults.** Illustrative:
  `gen_tailscale_acl.py <ci-controller.yml> <repos.yml> --out <acl.hujson>` and the same
  with `--check` (exit 1 when the file at `--out` is stale). The three module constants
  and the `parents[1]` root lookup go. A missing argument is a usage error (exit 2), a
  missing or unreadable input is exit 1 with the path named — the same contract
  `validate_pool_config.py` documents. `render_acl` keeps its signature and its
  deterministic output, so an operator who regenerates sees an empty diff.
- **The docstring states the decision** from the Gate: which operator generates, where
  the file is committed, how it reaches the tailnet. "Applied from CI" stays only if
  that is what the answer makes true.
- **One place describes adding a machine.** The `README.md` lane-host paragraph gains
  the actual steps (add the host to the pool config, regenerate, apply, mint the tagged
  key), and ADR 0026, `services/ci-lane-host/README.md` and the comment in
  `services/ci-lane-host/compose.yml` point there instead of at a missing runbook and a
  path this repo does not hold. `federated-ci-pool.md` decision 5 is corrected to the
  decision.
- **Migration.** The consumer deletes its copy and calls the collection's script with
  its own three paths; its committed ACL does not change. That edit belongs to the
  consumer's own plan, after its pin moves past this change. Issue #7 closes with the PR.

**Alternatives considered:**
- *The ACL is committed here with a workflow that applies it* (issue #7, option 2). It
  needs a committed copy of every host's `allowed_orgs` and every org, which duplicates
  the one fact the scheduler reads from its operator's config, puts both operators'
  machine names in a public repo that "ships no pool config", and gives this repo's
  GitHub-hosted CI a tailnet-admin credential. It remains the right answer only if the
  Gate says the ACL must not sit with one operator.
- *Move the script to the consumer* (option 1, literally). The `allowed_orgs` → ACL
  mapping would then be tested away from `HostConfig`, in a private repo the other
  operator cannot read.
- *Only fix the docstring* (option 3). Leaves a hard-stop script that cannot run and has
  no test.

**Testing strategy:** new `scripts/tests/test_gen_tailscale_acl.py`, on an inline
fixture pool in `tmp_path`, never a real operator's names:
- the mapping: a host restricted to one org admits only that org's dispatcher tag; a
  host with no `allowed_orgs` admits every dispatcher; every dispatcher reaches the
  scheduler port and nothing else is accepted; every tag has an owner; no `group:` is
  used (the Personal-plan cap in `federated-ci-pool.md`);
- parity with the scheduler: for the same fixture, an org may be placed on a host by
  ci-controller's `allowed_orgs` filter exactly when its dispatcher tag is in that
  host's rule — the wiring test the federated plan asks for;
- the command line: writes `--out`; `--check` exits 0 when fresh and 1 when stale;
  a missing input exits 1 naming the path.
The `scripts` job in `.github/workflows/pytest.yml` already runs `scripts/tests` with
`pytest`, `pydantic` and `pyyaml`, so no workflow changes.

## Paths

As of 2026-11-22.

- `scripts/gen_tailscale_acl.py`
- `scripts/tests/test_gen_tailscale_acl.py` (new)
- `README.md`
- `docs/decisions/0026-ci-fabric-tailnet.md`
- `docs/plans/ideas/federated-ci-pool.md`
- `services/ci-lane-host/README.md`
- `services/ci-lane-host/compose.yml` (comment only)
- `services/ci-controller/src/config.py` (comment only, if the wording changes)

## Approval

**Needs your yes** — the Gate question, which is a decision for both operators.
Recommended answer: the operator who runs the scheduler generates and applies it from
their own repo, because the host inventory with `allowed_orgs` already lives only in
that operator's config, so the ACL stays one fact with two readers and no tailnet-admin
credential enters this public repo. `scripts/gen_tailscale_acl.py` and
`services/ci-lane-host/**` are `hardStop` paths: the PR is handed over, never
self-merged.

## Out of scope

- Applying the ACL through Tailscale's API or a GitOps action, and any check of the
  committed file against the live tailnet policy. Today's only check compares the file
  with the generator's output.
- A committed ACL or pool config in this repo.
- The consumer's own cut-over to the collection's script.
- The dispatcher tag hardcoded in `services/ci-controller/compose.yml`
  (`controller-compose-names-one-operator.md`).
- Onboarding the new lane host itself.
