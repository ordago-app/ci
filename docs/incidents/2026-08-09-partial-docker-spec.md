# Partial docker_container spec recreates a container

## Symptom

Restarting `ci-lane-socket-proxy` via
`ansible ... -m community.docker.docker_container -a 'name=X state=started'` came back
listening on nothing (`PortBindings: {}`), and then blocked `docker compose up` with a name
conflict the next play run couldn't resolve.

## Root cause

`community.docker.docker_container` is declarative: it compares the live container against the
spec it was given. A spec with no `ports`/`volumes`/`labels` means "those should be absent," so
the module recreated the container without them — including its compose project labels, which
made it a stranger squatting the name as far as compose was concerned.

## Fix

`docker rename`d the orphan aside (non-destructive, and works even when container removal is
blocked), then let compose create the real container.

## What still bites

- Never use `community.docker.docker_container` with only `name`+`state` to "restart" a
  compose-managed container — it silently drops ports/volumes/labels. Drive restarts through
  compose (`community.docker.docker_compose_v2`) or the owning playbook/service unit instead.
- If a module call must be used, pass the full spec, never a partial one.
- Recovery when it happens again: `docker rename` the orphan aside, then let compose or the
  boot unit create the real one.
