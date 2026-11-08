"""Every compose file the roles ship must be one docker compose accepts.

PyYAML keeps the last of two equal keys without a word; docker compose refuses the
file ("mapping key already defined"). A three-way merge left `INFO: 1` twice in the
controller's socket proxy, every test here still passed, and the first deploy at that
pin would have replaced the host's compose file with one the stack-up could not parse.
"""

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE_FILES = sorted(
    p
    for p in REPO.glob("services/**/compose*.y*ml")
    if ".venv" not in p.parts and "node_modules" not in p.parts
)


class _StrictLoader(yaml.SafeLoader):
    pass


def _no_duplicate_keys(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False):
    seen: set[object] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys)


def test_there_are_compose_files_to_check() -> None:
    assert COMPOSE_FILES, "the glob found nothing; the check below would pass vacuously"


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_no_compose_file_repeats_a_key(path: Path) -> None:
    yaml.load(path.read_text(), Loader=_StrictLoader)
