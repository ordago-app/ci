"""Print the ci-controller's live state. Piped into the controller container's
python over SSH by `make ci-status` (runs inside the container so it can reach
the controller's localhost:8000 status endpoint, which is not host-exposed)."""

import json
import urllib.request
from collections import Counter

d = json.load(urllib.request.urlopen("http://localhost:8000/status"))
print(f"lanes:     {d['lanes_running']} / {d['max_lanes']}")
print(f"ram:       {d['ledger_ram_mb']} / {d['budget_ram_mb']} MB")
print("disk_gb:  ", d["disk_gb"])
print("running:  ", dict(Counter(r["class"] for r in d["running"])))
print("deferred: ", dict(Counter(x["reason"] for x in d["deferred"])))
print(f"mode:      {d['admission_mode']}")
