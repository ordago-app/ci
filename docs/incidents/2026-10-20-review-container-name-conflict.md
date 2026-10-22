# Review container name conflict

## Symptom

github-review failed every attempt at several PR heads with
`409 Conflict ... The container name "/codex-review-<job>" is already in use`. The
host held a handful of `codex-review-*` containers in state `Created` that had
never been started, some of them weeks old.

## Root cause

`containers.run` is two Docker API calls, create then start. Under IO pressure on
the host ([same night](2026-10-20-ci-held-by-io-wait-load.md)) dockerd answered the
start after the 60 s client timeout, so docker-py raised with the container
already created. `start_review_session` raised before it returned a `SessionRef`,
and the worker's `finally` only cleans up sessions it was handed. The name is
`codex-review-<job id>`, stable across a job's retries, so every later attempt hit
the conflict until the job's attempts ran out.

## Fix

`CodexReviewProvider.start_review_session` removes any container already under the
job's name before creating one (safe, because a job is claimed by exactly one
worker, the caller), and removes the container if create or start raises.
`tests/test_session_container_start.py` covers this against a fake daemon that
creates the container before the start times out.

## What still bites

Containers that are created but never started are not running, so a dispatcher
that charges review containers by listing running ones does not see them. They
leak names, not RAM.
