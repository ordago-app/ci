from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HostStats:
    mem_available_mb: int
    load_1m: float
    # PSI `some avg60` from /proc/pressure/cpu: the share of the last minute in which
    # at least one runnable task waited for a CPU. Unlike load_1m it does not count
    # tasks blocked on IO. None when the kernel offers no reading -- never 0, which
    # would read as "CPUs idle".
    cpu_pressure_pct: float | None = None


def read_host_stats(
    meminfo: str = "/proc/meminfo",
    loadavg: str = "/proc/loadavg",
    cpu_pressure: str = "/proc/pressure/cpu",
) -> HostStats:
    mem_available_kb = 0
    with open(meminfo) as fh:
        for line in fh:
            if line.startswith("MemAvailable:"):
                mem_available_kb = int(line.split()[1])
                break
    with open(loadavg) as fh:
        load_1m = float(fh.read().split()[0])
    return HostStats(
        mem_available_mb=mem_available_kb // 1024,
        load_1m=load_1m,
        cpu_pressure_pct=_read_cpu_pressure(cpu_pressure),
    )


def _read_cpu_pressure(path: str) -> float | None:
    try:
        with open(path) as fh:
            for line in fh:
                fields = line.split()
                if fields and fields[0] == "some":
                    for field in fields[1:]:
                        key, _, value = field.partition("=")
                        if key == "avg60":
                            return float(value)
    except (OSError, ValueError):
        return None
    return None
