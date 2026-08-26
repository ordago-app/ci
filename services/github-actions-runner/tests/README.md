# Why there is no compose-render test here

`test_compose_render.py` did not come across from homelab, and should not be
re-added in the form it had there.

It read `personal/github-runners.yml` and `personal/repos.yml` **at module
scope** and rendered `compose.yml.j2` against them. That makes it a test of one
operator's runner pool, not of this template: it cannot even be collected
without a `personal/` directory, and a shared platform that shipped one
operator's host config would be exactly the neutrality decay
`ci-controller/tests/test_no_operator_defaults.py` exists to prevent.

The division that is actually correct:

- **This repo ships the template** and should prove it renders — against a
  **fixture** pool config committed here, asserting on the template's own
  contract (labels, work-dir routing, env wiring).
- **Each consumer proves their own config renders** against the template they
  have pinned. homelab keeps its copy for exactly that.

`test_compose_render_fixture.py` is that fixture-based test: it renders
`compose.yml.j2` against `fixtures/pool.yml` and `fixtures/repos.yml` — a pool
that belongs to no real operator — and asserts on the template's own contract
(service naming, KVM device wiring, disabled-runner skipping).

`test_entrypoint_token.py` stayed: its 15 tests assert on `entrypoint.sh`'s own
token handling and read no operator config.
