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
running = d["running"]
# This script is piped from a git checkout into whatever controller is deployed, so the
# two versions drift apart between a merge and the next deploy. Name the skew instead of
# defaulting the missing field: a default would report every lane as busy, which is both
# wrong and indistinguishable from the truth. Report what the old payload does support.
if any("state" not in lane for lane in running):
    print("lanes:    ", dict(Counter(r["class"] for r in running)))
    print("           (deployed controller predates the busy/booting split — deploy to see it)")
else:
    busy = [r for r in running if r["state"] == "busy"]
    booting = [r for r in running if r["state"] != "busy"]
    # "busy:", not "running:": this counts only lanes GitHub has actually given a job, while
    # TOTAL above counts every lane holding a reservation. Labelled "running" the two silently
    # failed to add up whenever a lane was booting -- and the booting line below only prints
    # when it is non-empty, so there was nothing on screen to explain the difference.
    print("busy:     ", dict(Counter(r["class"] for r in busy)))
    if booting:
        ages = ", ".join(f"{r['class']}:{r['idle_seconds']}s" for r in booting)
        print(f"booting:   {len(booting)} ({ages})")
print("deferred: ", dict(Counter(x["reason"] for x in d["deferred"])))
print(f"mode:      {d['admission_mode']}")
