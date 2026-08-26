#!/usr/bin/env python3
"""Validate a pool config against the platform's schema.

This is THE authority on what a valid pool config is. A consumer keeps its own
`ci-controller.yml` and proves it valid by running this from the installed
collection, rather than importing platform code it no longer vendors.

Usage: validate_pool_config.py <ci-controller.yml> <repos.yml>
Exit 0 valid; exit 1 invalid, reason on stderr.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "ci-controller"))

from src.config import ControllerConfig


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    config, registry = Path(argv[1]), Path(argv[2])
    # ControllerConfig.load expects repos.yml as a sibling of the config, which a
    # consumer's layout need not satisfy. Stage both into a temp dir rather than
    # requiring the caller to rearrange their repo.
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / config.name
        shutil.copy(config, staged)
        shutil.copy(registry, Path(tmp) / "repos.yml")
        try:
            ControllerConfig.load(staged)
        except Exception as exc:
            print(f"invalid pool config {config}: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
