import os
from pathlib import Path

import yaml

SERVICE_ROOT = Path(__file__).resolve().parents[1]
SERVICES_PLAYBOOK = SERVICE_ROOT.parents[1] / "ansible" / "playbooks" / "services.yml"


def test_credential_helper_vendored_and_executable() -> None:
    # Regression: the worker runs `git fetch` against a PRIVATE repo over HTTPS
    # with no credentials, so fetch died with "could not read Username for
    # 'https://github.com'". It must authenticate as the reviewer App via the
    # same credential helper the agent containers use.
    helper = SERVICE_ROOT / "bin" / "git-credential-helper"
    assert helper.is_file(), "git-credential-helper must be vendored into the image build context"
    assert os.access(helper, os.X_OK), "git-credential-helper must be executable"
    body = helper.read_text()
    assert "internal/github-token?role=" in body, "helper must request a role-scoped token"
    assert "x-access-token" in body, "helper must return the App token as x-access-token"


def test_dockerfile_wires_credential_helper() -> None:
    dockerfile = (SERVICE_ROOT / "Dockerfile").read_text()
    assert "git-credential-helper" in dockerfile, "image must install the credential helper"
    assert "credential.helper" in dockerfile, "image must configure git credential.helper"


def test_compose_sets_reviewer_role() -> None:
    compose = (SERVICE_ROOT / "compose.yml").read_text()
    assert "AGENT_ROLE=reviewer" in compose, "compose must set AGENT_ROLE=reviewer for git auth"
    assert "ROUTER_INTERNAL_URL=" in compose, "compose must point the helper at the router"


def test_compose_runs_an_init_to_reap_zombies() -> None:
    # Regression: `python -m src.main` runs as PID 1 in the container. The
    # worker shells out to `git` (fetch/worktree/clean), and git's transient
    # helper children (git-remote-https, pack processes) get reparented to
    # PID 1 when their git parent exits. PID 1 here never reaps them, so they
    # linger as zombies on powerserver. `init: true` makes Docker run tini as
    # PID 1, which reaps exited children.
    compose = yaml.safe_load((SERVICE_ROOT / "compose.yml").read_text())
    svc = compose["services"]["github-review"]
    assert svc.get("init") is True, "compose must set init: true so PID 1 reaps git helper zombies"


def test_project_clone_worktrees_dir_is_owned_by_the_operator() -> None:
    # Regression: github-review runs as root but drops `git` to the operator uid
    # (main.py WORKTREE_RUN_AS_UID) so clone objects stay operator-owned for the
    # deploy. `git worktree add` mkdir's inside <repo>/.git/worktrees, and git
    # creates THAT directory itself, on first use, owned by whoever ran the first
    # `worktree add` — not by any ansible task.
    #
    # On ordago-apps the first one was a root-run agent session (May 2026), so
    # the directory was root-owned and every review died with:
    #
    #   fatal: could not create directory of '.git/worktrees/pr-406-…':
    #   Permission denied
    #
    # 15 failed jobs, 0 posted reviews, from 2026-06-21 until this fix — the
    # reviewer never once succeeded on that repo. homelab was unaffected only by
    # luck: its first worktree happened to be created by a uid-1000 process.
    #
    # git never re-chowns an existing worktrees dir, so ansible has to assert it.
    play = yaml.safe_load(SERVICES_PLAYBOOK.read_text())[0]
    owning = [
        task
        for task in play["tasks"]
        if ".git/worktrees" in str((task.get("ansible.builtin.file") or {}).get("path", ""))
    ]
    assert owning, (
        "services.yml must assert ownership of each project clone's .git/worktrees; "
        "without it a root-created worktrees dir permanently blocks github-review"
    )
    for task in owning:
        spec = task["ansible.builtin.file"]
        assert spec.get("owner") == "{{ operator_user }}", (
            f"{task.get('name')!r} must set owner to the operator, not root"
        )
        assert spec.get("state") == "directory"
        # recurse: the dir may already hold root-owned entries from the same
        # root-run session that created it (ordago-apps had one from May).
        # Fixing only the parent leaves those to break `worktree prune` later.
        assert spec.get("recurse") is True, (
            f"{task.get('name')!r} must recurse to repair pre-existing root-owned entries"
        )
