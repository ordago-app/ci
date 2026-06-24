from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HostStats:
    mem_available_mb: int
    load_1m: float


def read_host_stats(meminfo: str = "/proc/meminfo", loadavg: str = "/proc/loadavg") -> HostStats:
    mem_available_kb = 0
    with open(meminfo) as fh:
        for line in fh:
            if line.startswith("MemAvailable:"):
                mem_available_kb = int(line.split()[1])
                break
    with open(loadavg) as fh:
        load_1m = float(fh.read().split()[0])
    return HostStats(mem_available_mb=mem_available_kb // 1024, load_1m=load_1m)
