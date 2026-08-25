#!/usr/bin/env python3
"""Render the CI fabric's Tailscale ACL from each host's `allowed_orgs`.

One fact, two consumers (docs/plans/ideas/federated-ci-pool.md decision 6): the
scheduler filters placement on `allowed_orgs`, and this renders the network-layer
rule that makes the same restriction infra-backed. A dispatcher that skips the
scheduler and dials a socket proxy directly is refused by the fabric, not merely
by our code -- which is the difference the `enforce-trust-boundary` skill draws
between an infra-backed gate and an app-level one.

The output is COMMITTED and applied from CI. Never edit the Tailscale admin
console by hand: that would put the system's real access rules outside the repo
an agent can read, and the committed file would then be fiction.

Dependency-free on purpose (pyyaml only). It reads committed YAML rather than
importing ci-controller's pydantic config, so it runs in pytest.yml's repo-tests
job without dragging a service's dependency tree in behind it.

Usage:  python3 scripts/gen_tailscale_acl.py          # rewrite tailscale/acl.hujson
        python3 scripts/gen_tailscale_acl.py --check  # exit 1 if it is stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ACL_PATH = Path("tailscale/acl.hujson")
CONTROLLER_CONFIG = Path("personal/ci-controller.yml")
REPO_REGISTRY = Path("personal/repos.yml")

DISPATCHER_TAG = "tag:ci-dispatcher-{org}"
LANE_HOST_TAG = "tag:ci-lane-host-{host}"
SCHEDULER_TAG = "tag:ci-scheduler"
SOCKET_PROXY_PORT = 2375
SCHEDULER_PORT = 8001

# Only an Owner/Admin may apply these. Naming an owner rather than leaving the list
# empty keeps the policy readable and survives a non-admin operator being added.
TAG_OWNERS = ["autogroup:admin"]


def load_orgs(repo: Path) -> list[str]:
    """Every GitHub org the pool serves, from the one file that maps a project to
    its `owner/name` (personal/repos.yml). Deriving them rather than listing them
    again is what stops a new project's org silently missing an ACL rule."""
    registry = yaml.safe_load((repo / REPO_REGISTRY).read_text())["project_repos"]
    return sorted({owner_name.split("/", 1)[0] for owner_name in registry.values()})


def load_hosts(repo: Path) -> dict[str, list[str] | None]:
    """Each host and the orgs allowed on it. `None` means unrestricted, matching
    ci-controller's HostConfig.allowed_orgs default exactly -- the two readings of
    this field must not diverge."""
    config = yaml.safe_load((repo / CONTROLLER_CONFIG).read_text())
    return {
        name: (host or {}).get("allowed_orgs") for name, host in (config.get("hosts") or {}).items()
    }


def render_acl(hosts: dict[str, list[str] | None], *, orgs: list[str]) -> str:
    """The policy file, deterministic so a regeneration diffs cleanly."""
    dispatcher_tags = [DISPATCHER_TAG.format(org=org) for org in orgs]

    acls: list[dict[str, object]] = [
        {
            # Every dispatcher reaches the scheduler. It is credential-free and
            # answering them is its entire job.
            "action": "accept",
            "src": dispatcher_tags,
            "dst": [f"{SCHEDULER_TAG}:{SCHEDULER_PORT}"],
        }
    ]
    for host, allowed in sorted(hosts.items()):
        srcs = (
            dispatcher_tags
            if allowed is None
            else [DISPATCHER_TAG.format(org=o) for o in sorted(allowed)]
        )
        acls.append(
            {
                "action": "accept",
                "src": srcs,
                "dst": [f"{LANE_HOST_TAG.format(host=host)}:{SOCKET_PROXY_PORT}"],
            }
        )

    policy = {
        "tagOwners": {
            **{tag: TAG_OWNERS for tag in dispatcher_tags},
            SCHEDULER_TAG: TAG_OWNERS,
            **{LANE_HOST_TAG.format(host=h): TAG_OWNERS for h in sorted(hosts)},
        },
        "acls": acls,
        # Tailscale default-denies anything no rule accepts once `acls` is present.
        # Stated so a reader does not have to know that to trust the file.
        "ssh": [],
    }
    return json.dumps(policy, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if the file is stale")
    args = parser.parse_args(argv)

    repo = Path(__file__).resolve().parents[1]
    rendered = render_acl(load_hosts(repo), orgs=load_orgs(repo))
    target = repo / ACL_PATH

    if args.check:
        current = target.read_text() if target.exists() else ""
        if current != rendered:
            print(
                f"{ACL_PATH} is stale — run: python3 scripts/gen_tailscale_acl.py",
                file=sys.stderr,
            )
            return 1
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered)
    print(f"wrote {ACL_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
