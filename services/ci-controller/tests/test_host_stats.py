from src.host_stats import read_host_stats


def _files(tmp_path, pressure: str | None):
    mi = tmp_path / "meminfo"
    mi.write_text("MemTotal:       16331640 kB\nMemAvailable:    2097152 kB\n")
    la = tmp_path / "loadavg"
    la.write_text("4.20 2.25 1.40 2/1234 9999\n")
    psi = tmp_path / "cpu"
    if pressure is not None:
        psi.write_text(pressure)
    return str(mi), str(la), str(psi)


def test_parses_meminfo_and_loadavg(tmp_path) -> None:
    s = read_host_stats(*_files(tmp_path, None))
    assert s.mem_available_mb == 2048  # 2097152 kB / 1024
    assert s.load_1m == 4.20


def test_reads_cpu_pressure_some_avg60(tmp_path) -> None:
    psi = (
        "some avg10=0.15 avg60=37.21 avg300=2.04 total=18088886309\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    )
    assert read_host_stats(*_files(tmp_path, psi)).cpu_pressure_pct == 37.21


def test_no_pressure_file_means_no_reading_not_zero(tmp_path) -> None:
    # Zero would read as "CPUs idle" and switch the load gate off on a kernel
    # without PSI; None keeps the gate's plain load-average meaning.
    assert read_host_stats(*_files(tmp_path, None)).cpu_pressure_pct is None
    assert read_host_stats(*_files(tmp_path, "garbage\n")).cpu_pressure_pct is None


def test_a_malformed_avg60_means_no_reading(tmp_path) -> None:
    psi = "some avg10=0.15 avg60=n/a avg300=2.04 total=1\n"
    assert read_host_stats(*_files(tmp_path, psi)).cpu_pressure_pct is None
