from src.host_stats import read_host_stats


def test_parses_meminfo_and_loadavg(tmp_path) -> None:
    mi = tmp_path / "meminfo"
    mi.write_text("MemTotal:       16331640 kB\nMemAvailable:    2097152 kB\n")
    la = tmp_path / "loadavg"
    la.write_text("4.20 2.25 1.40 2/1234 9999\n")
    s = read_host_stats(str(mi), str(la))
    assert s.mem_available_mb == 2048  # 2097152 kB / 1024
    assert s.load_1m == 4.20
