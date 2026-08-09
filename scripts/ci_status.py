"""Print the ci-controller's live state. Piped into the controller container's
python over SSH by `make ci-status` (runs inside the container so it can reach
the controller's localhost:8000 status endpoint, which is not host-exposed)."""

import json
import urllib.request
from collections import Counter

d = json.load(urllib.request.urlopen("http://localhost:8000/status"))

# Per-host first. The aggregate line below counts lanes across every host but can
# only show one ceiling, so on a multi-host pool it reads as an over-admission that
# is not happening: 6 lanes against powerserver's "5" when the real ceiling is 5+2.
# An unhealthy host is called out because it is invisible otherwise -- it simply
# stops receiving work, which looks identical to an idle pool.
hosts = d.get("hosts") or {}
for name, h in sorted(hosts.items()):
    health = "" if h["healthy"] else "  UNHEALTHY (skipped for admission)"
    print(
        f"{name:<14} lanes {h['lanes']}/{h['max_lanes']}"
        f"   ram {h['ram_mb']}/{h['budget_ram_mb']} MB{health}"
    )

total_max = sum(h["max_lanes"] for h in hosts.values()) if hosts else d["max_lanes"]
print(f"TOTAL          lanes {d['lanes_running']}/{total_max}   ram {d['ledger_ram_mb']} MB")
print("disk_gb:  ", d["disk_gb"])
print("running:  ", dict(Counter(r["class"] for r in d["running"])))
print("deferred: ", dict(Counter(x["reason"] for x in d["deferred"])))
print(f"mode:      {d['admission_mode']}")
