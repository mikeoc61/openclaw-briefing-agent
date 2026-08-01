"""Unit tests for mem_snapshot classification and delta logic."""
import sys, pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
from mem_snapshot import classify, deltas_since  # noqa: E402


def base_cur(**over):
    """Current Pi state: ~1G swap slots, all RAM-resident, zero pressure."""
    cur = {
        "boot_id": "abc",
        "page_kb": 16,                # Pi 5 default 16K pages
        "mem_total_kb": 8_288_876,    # 7.9G
        "mem_avail_kb": 5_557_452,    # 5.3G
        "swap_total_kb": 2_097_148,
        "swap_free_kb": 1_054_924,    # -> 1,042,224 kB slots used
        "swap_cached_kb": 626_112,
        "zswapped_kb": 380_816,
        "zswap_pool_kb": 127_296,
        "zswap_enabled": True,
        "max_pool_pct": 20,
        "counters": {"pswpin": 0, "pswpout": 0, "oom_kill": 0,
                     "written_back_pages": 0, "reject_alloc_fail": 0},
    }
    cur.update(over)
    return cur


def test_phantom_swap_is_ok():
    """1G of swap slots with zero writeback must NOT warn."""
    cur = base_cur()
    status, flags, summary = classify(cur, {k: 0 for k in cur["counters"]})
    assert status == "ok"
    assert flags == []
    assert "disk 0" in summary          # decomposition names the disk truth
    assert "zswap" in summary
    assert "3.0x" in summary            # 380816/127296 compression ratio


def test_disk_swapout_warns():
    cur = base_cur()
    status, flags, _ = classify(cur, {"pswpout": 1000})
    assert status == "warn"
    assert any("swap-out" in f for f in flags)


def test_writeback_delta_warns():
    cur = base_cur()
    status, flags, _ = classify(cur, {"written_back_pages": 5})
    assert status == "warn"
    assert any("writeback" in f for f in flags)


def test_reject_delta_warns():
    cur = base_cur()
    status, flags, _ = classify(cur, {"reject_alloc_fail": 3})
    assert status == "warn"
    assert any("rejects" in f for f in flags)


def test_pool_over_80pct_warns():
    # cap = 8288876 * 20% = ~1.66G; pool at 90% of that
    cur = base_cur(zswap_pool_kb=int(8_288_876 * 0.20 * 0.9))
    status, flags, _ = classify(cur, {})
    assert status == "warn"
    assert any("pool" in f for f in flags)


def test_oom_is_crit_and_outranks_warn():
    cur = base_cur(mem_avail_kb=700_000)   # ~8.4% -> warn floor too
    status, flags, _ = classify(cur, {"oom_kill": 1})
    assert status == "crit"
    assert any("OOM" in f for f in flags)
    assert any("MemAvailable" in f for f in flags)


def test_memavailable_floors():
    warn = classify(base_cur(mem_avail_kb=700_000), {})[0]    # ~8.4%
    crit = classify(base_cur(mem_avail_kb=300_000), {})[0]    # ~3.6%
    ok = classify(base_cur(), {})[0]                          # ~67%
    assert (warn, crit, ok) == ("warn", "crit", "ok")


def test_no_zswap_counters_still_classifies():
    """Kernel without zswpwb + unreadable debugfs: pswp* alone suffices."""
    cur = base_cur(counters={"pswpin": 0, "pswpout": 0, "oom_kill": 0},
                   zswapped_kb=None, zswap_pool_kb=None, max_pool_pct=None,
                   zswap_enabled=False)
    status, flags, summary = classify(cur, {"pswpout": 0})
    assert status == "ok"
    assert "swap" in summary            # slot usage still reported as context


def test_deltas_normal_and_reboot():
    prev = {"boot_id": "abc", "counters": {"pswpout": 100, "oom_kill": 1}}
    cur = base_cur(counters={"pswpout": 150, "oom_kill": 1})
    d, rebooted = deltas_since(cur, prev)
    assert not rebooted and d["pswpout"] == 50 and d["oom_kill"] == 0

    prev["boot_id"] = "old-boot"
    cur["counters"] = {"pswpout": 10, "oom_kill": 0}
    d, rebooted = deltas_since(cur, prev)
    assert rebooted and d["pswpout"] == 10    # since-boot, not negative


def test_first_run_no_state():
    cur = base_cur()
    d, rebooted = deltas_since(cur, None)
    assert rebooted and d == cur["counters"]
