# Plans

Temporary coordination docs, one file per topic, moving between folders as the
work matures: `ideas/` (proposed) → `ready/` (decided, tasks written, not
started) → `ongoing/` (in progress, Status section required). A finished plan is
**deleted**, not archived — durable rationale goes to
[`../decisions/`](../decisions/), a production record to
[`../incidents/`](../incidents/). Git remembers the rest.

The lifecycle is the shared `managing-plans-lifecycle` skill
(`.agents/_shared/`). What is specific to this repo:

- Every plan declares one **`**Priority:** high | medium | low`** line under
  its title.
- Filenames are bare kebab-case. **No date prefix** — brainstorming output
  that arrives dated is renamed on landing.
- No `soak/`. Deploy here is per operator, by SHA pin; there is no shared
  environment a plan could be "landed" on. A plan retires when the behaviour
  is verified on at least one operator's pool.
- A plan that spans this repo and an operator's private inventory lives here
  only for the part this repo owns. Nothing operator-private goes in a plan.
