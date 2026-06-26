# 12. powerserver CI scratch on NVMe; RAM is a dead-end

Date: 2026-06-26

## Status

Accepted. Supersedes the operational detail of the (now-deleted)
`powerserver-hardware-upgrade` plan. Hardware/disk facts live in
[topology.md](../topology.md); this ADR records the *why*.

## Context

`powerserver` (the homelab prod host, also the self-hosted CI box) hit a
CI-throughput wall. The original 2026-06 upgrade plan assumed two fixes: add RAM
(16→32 GB) and add a faster CI scratch disk. Investigation during the upgrade
changed the picture:

- The board is a **MEDION/MSI MS-7848** with **only 2 physical DIMM slots, 16 GB
  max** (8 GB/module). `dmidecode` reports 4 slots, but two are **phantom SMBIOS
  entries** with no physical socket — which is what made the plan (and the
  `[[powerserver-hardware-upgrade]]` memory) wrongly assume a 32 GB path.
- The old 30 GB SATA scratch SSD had a **1–6 second write-latency floor** and was
  **100 % full** of leaked CI work dirs.
- After migrating to a 500 GB NVMe, the **`ci-controller` disk budget
  (`disk_budget_gb.ssd: 6`)** — sized for the dead 30 GB drive — silently
  throttled concurrency to ~3 lanes via `DISK_FULL` deferrals, while RAM and CPU
  sat idle.

## Decision

1. **CI scratch is a 500 GB NVMe (WD_BLACK SN770) on a PCIe→M.2 adapter in the
   x16 slot**, not a SATA SSD. Pulling the unused GTX 760 freed the x16 slot;
   NVMe's deep queues suit concurrent CI far better than SATA's single shallow
   queue. The H81/H87 BIOS can't *boot* NVMe — irrelevant, it's scratch-only at
   `/mnt/ci-ssd`; the OS boots from the SATA HDD. All CI work + caches live on
   the NVMe so a job never touches the 7200rpm HDD.
2. **RAM is not upgradeable; do not try.** 16 GB is the board ceiling. The path
   to more RAM/cores is **replacing the whole box** (a used DDR4 mini-PC/SFF with
   6–8 cores + native NVMe), never feeding this 2013 LGA1150 platform.
3. **`disk_budget_gb` must track the actual disk size.** A budget left at the old
   drive's value is a silent concurrency throttle. After the NVMe it was lifted
   to 300 GB so `max_concurrent_lanes` (not the disk) is the binding cap.
4. **The CPU (4c/8t), shared with the co-tenant homelab services, is the real
   concurrency ceiling** — not RAM, not disk. `max_concurrent_lanes` is tuned
   against that shared budget, validated under a load burst.

## Consequences

- The disk write-stall and disk-full throttle are gone; concurrency is bounded by
  `max_concurrent_lanes` against the shared CPU, as intended.
- CI cannot scale past the ~8-thread wall on this box; meaningfully more CI
  throughput requires a hardware replacement, which also lifts the RAM ceiling.
- The old 30 GB Toshiba SSD is retained, unmounted (relabeled `ci-ssd-old`), as a
  one-fstab-line rollback for `/mnt/ci-ssd`.
- A `disk_budget_gb` that drifts from the mounted disk size will silently
  throttle again — keep it in sync on any future disk change.
