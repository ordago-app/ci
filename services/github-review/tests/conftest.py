import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def project_repo_registry(tmp_path: Path) -> Path:
    """personal/repos.yml beside every agent-review.yml a test writes.

    ReviewConfig.load resolves each project's GitHub `owner/name` from there —
    the config itself no longer carries one — so a test config without this
    fixture is not loadable, which is the point: the mapping has exactly one
    home."""
    path = tmp_path / "repos.yml"
    path.write_text(
        "project_repos:\n"
        "  homelab: alvaro-francisco-gil/homelab\n"
        "  ordago-apps: ordago-app/ordago-apps\n"
    )
    return path


@pytest.fixture()
def repo_dir(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "base"], check=True)
    subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feature"], check=True)
    (repo / "README.md").write_text("head\n")
    subprocess.run(["git", "-C", str(repo), "commit", "-am", "head", "-q"], check=True)
    return repo
