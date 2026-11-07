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
    # The reserve above cannot show a host out of CPU or free memory, which admission
    # holds lanes on (host_pressure): 2026-11-07, 5/11 GB reserved and nothing starting.
    # A pressure dict of another shape names the skew, like the lane states below.
    p = h.get("pressure")
    if p:
        keys = ("load_1m", "load_ceiling", "mem_available_mb", "free_ram_floor_mb", "binding")
        if any(k not in p for k in keys):
            print(f"{'':<14} pressure: (unexpected shape from the deployed controller: {p})")
        else:
            hold = "  HOLDING new lanes (host_pressure)" if p["binding"] else ""
            print(
                f"{'':<14} load {p['load_1m']:.1f}/{p['load_ceiling']:g}"
                f"   free {p['mem_available_mb']}/{p['free_ram_floor_mb']} MB floor{hold}"
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
    # A controller predating the stuck split reports every unattributed lane as "booting",
    # so this stays empty there rather than fabricating a state the payload never carried.
    stuck = [r for r in running if r["state"] == "stuck"]
    booting = [r for r in running if r["state"] == "booting"]
    # "busy:", not "running:": this counts only lanes GitHub has actually given a job, while
    # TOTAL above counts every lane holding a reservation. Labelled "running" the two silently
    # failed to add up whenever a lane was booting -- and the booting line below only prints
    # when it is non-empty, so there was nothing on screen to explain the difference.
    print("busy:     ", dict(Counter(r["class"] for r in busy)))
    if booting:
        ages = ", ".join(f"{r['class']}:{r['idle_seconds']}s" for r in booting)
        print(f"booting:   {len(booting)} ({ages})")
    # Printed last and per-lane, not aggregated: a stuck lane is the thing you act on, and
    # on 2026-08-20 exactly one of them held 7500 MB and head-of-line-blocked 7 deferred
    # jobs for 36 minutes while rendering here as an ordinary "booting" entry.
    for r in stuck:
        why = r.get("reap_blocked_reason") or "past idle cap, no reap attempt recorded"
        n = r.get("reap_block_count") or 0
        streak = f" x{n}" if n else ""
        print(
            f"STUCK:     {r['lane_id']} ({r['class']}, {r['ram_mb']} MB) "
            f"idle {r['idle_seconds']}s — {why}{streak}"
        )
print("deferred: ", dict(Counter(x["reason"] for x in d["deferred"])))
# Every host's own gates: `reason` is only the closest host's and can name a full
# budget elsewhere while the roomy host is holding on its CPU.
by_host = Counter(
    f"{host}: {'+'.join(gates)}"
    for x in d["deferred"]
    for host, gates in (x.get("gates_by_host") or {}).items()
)
if by_host:
    print("  by host:", dict(by_host))
print(f"mode:      {d['admission_mode']}")
# Same version-skew rule as the lane states above: say so rather than default it.
if "review_sessions" not in d:
    print("reviews:    (deployed controller predates review-aware admission — deploy to see it)")
else:
    reach = "" if d["review_state_reachable"] else "  github-review UNREACHABLE: tiers off"
    tiers = dict(Counter(x["review"] for x in d["deferred"] if x.get("review")))
    print(f"reviews:   {d['review_sessions']} running   deferred by head state {tiers}{reach}")
