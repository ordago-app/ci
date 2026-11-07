# A wall-clock test fails on a busy lane

**Priority:** low

`services/github-review/tests/test_concurrency.py::test_polling_never_waits_behind_running_reviews`
asserts two ticks return in under one second of wall clock. On 2026-11-07 it failed on
homelab#370 while powerserver was loaded: the github-review suite took 170 s there against
8 s locally, and a re-run passed. Same test, same threshold, here and in homelab's copy.

The property it guards is that a tick never *blocks* on a running review, not that it is
fast. Assert that directly: hold the `GatedProvider` gate closed for the whole test and
check both ticks returned (for example, run them in a thread joined with a generous
timeout well under the gate's), so load can slow the test but cannot fail it.
