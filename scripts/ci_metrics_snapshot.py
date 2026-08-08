"""Emit a consistent snapshot of the ci-controller metrics DB to stdout.

Run on the host (piped in over ssh). `cat` on the live DB can tear — the
controller commits to it on every tick — so take a real sqlite backup first.
"""

import pathlib
import sqlite3
import sys
import tempfile

SRC = "/opt/personal/state/ci-controller/metrics.db"

with tempfile.TemporaryDirectory() as tmp:
    dest = pathlib.Path(tmp) / "snapshot.db"
    source = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
    target = sqlite3.connect(str(dest))
    source.backup(target)
    target.close()
    source.close()
    sys.stdout.buffer.write(dest.read_bytes())
