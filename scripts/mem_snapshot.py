#!/usr/bin/env python3
"""Memory / swap / zswap snapshot with since-last-briefing deltas.

Replaces the naive "swap used > 64 MiB => warn" heuristic. On a zswap
system, swap slot usage is a CONTEXT metric, never an alarm: it decomposes
into SwapCached (RAM-resident), Zswapped (RAM, compressed), and true
on-disk pages. This collector alarms only on CONSEQUENCES of pressure:

  crit:  oom_kill delta > 0
         MemAvailable < 5% of MemTotal
  warn:  pswpout/pswpin delta > 0        (real disk swap I/O — works
                                          with or without zswap; pswp*
                                          only counts block-layer I/O,
                                          zswap stores don't touch it)
         zswpwb / written_back_pages delta > 0  (zswap pool spilled)
         zswap reject_* delta > 0        (pool refusing pages — acute)
         zswap pool > 80% of cap         (rejects/writeback imminent)
         MemAvailable < 10% of MemTotal

Because all alarm inputs are monotonic since-boot counters, a once-daily
point-in-time read + a state file answers "did any real paging / OOM /
rejects happen since the last briefing?" with no continuous monitoring
daemon. A reboot (detected via boot_id) resets deltas to "since boot".

Counter sources, in preference order per signal:
  disk writeback:  /sys/kernel/debug/zswap/written_back_pages (root only,
                   skipped if unreadable) -> /proc/vmstat zswpwb (>=6.8)
                   -> pswpout covers it regardless (writeback IS disk I/O)
  rejects:         /sys/kernel/debug/zswap/reject_* (skipped if unreadable;
                   consequences still caught via pswpout/pool-fill)
  pool size:       /proc/meminfo Zswap -> debugfs pool_total_size

Usage:  mem_snapshot.py [state_dir]
Output: key=value lines. Composer consumes mem_status / mem_summary /
        mem_flags; the raw fields are for debugging from the tmpdir.
State:  <state_dir>/mem_counters.last (JSON). Written only when state_dir
        is given and writable; read-only failure degrades to since-boot.
"""
import json, os, pathlib, sys, time

ZSWAP_DEBUG = pathlib.Path("/sys/kernel/debug/zswap")
REJECT_KEYS = (
    "reject_alloc_fail", "reject_compress_fail", "reject_compress_poor",
    "reject_kmemcache_fail", "reject_reclaim_fail",
)


def read_kv_file(path, split=None):
    """Parse 'key: value' (meminfo) or 'key value' (vmstat) into {str: int}."""
    out = {}
    try:
        for line in pathlib.Path(path).read_text().splitlines():
            parts = line.replace(":", " ").split()
            if len(parts) >= 2:
                try:
                    out[parts[0]] = int(parts[1])   # meminfo values are kB
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def read_int(path):
    try:
        return int(pathlib.Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def read_str(path):
    try:
        return pathlib.Path(path).read_text().strip()
    except OSError:
        return None


def collect():
    """Gather current point-in-time metrics + monotonic counters."""
    meminfo = read_kv_file("/proc/meminfo")          # kB
    vmstat = read_kv_file("/proc/vmstat")            # pages / events
    page_kb = os.sysconf("SC_PAGE_SIZE") // 1024

    counters = {}
    for k in ("pswpin", "pswpout", "oom_kill", "zswpin", "zswpout", "zswpwb",
              "pgscan_direct_throttle"):
        if k in vmstat:
            counters[k] = vmstat[k]
    # debugfs is root-only on most setups; every read fails soft.
    wb = read_int(ZSWAP_DEBUG / "written_back_pages")
    if wb is not None:
        counters["written_back_pages"] = wb
    for k in REJECT_KEYS:
        v = read_int(ZSWAP_DEBUG / k)
        if v is not None:
            counters[k] = v

    pool_kb = meminfo.get("Zswap")
    if pool_kb is None:
        pool_bytes = read_int(ZSWAP_DEBUG / "pool_total_size")
        pool_kb = pool_bytes // 1024 if pool_bytes is not None else None

    max_pool_pct = read_int("/sys/module/zswap/parameters/max_pool_percent")
    zswap_enabled = read_str("/sys/module/zswap/parameters/enabled") in ("Y", "1")

    return {
        "boot_id": read_str("/proc/sys/kernel/random/boot_id") or "unknown",
        "ts": int(time.time()),
        "page_kb": page_kb,
        "mem_total_kb": meminfo.get("MemTotal", 0),
        "mem_avail_kb": meminfo.get("MemAvailable", 0),
        "swap_total_kb": meminfo.get("SwapTotal", 0),
        "swap_free_kb": meminfo.get("SwapFree", 0),
        "swap_cached_kb": meminfo.get("SwapCached", 0),
        "zswapped_kb": meminfo.get("Zswapped"),     # uncompressed size in pool
        "zswap_pool_kb": pool_kb,                    # RAM cost of pool
        "zswap_enabled": zswap_enabled,
        "max_pool_pct": max_pool_pct,
        "counters": counters,
    }


def deltas_since(cur, prev):
    """Counter deltas vs previous run. Reboot => delta is value since boot."""
    rebooted = not prev or prev.get("boot_id") != cur["boot_id"]
    base = {} if rebooted else prev.get("counters", {})
    d = {k: v - base.get(k, 0) for k, v in cur["counters"].items()}
    # Clamp: a counter that shrank without a boot_id change is bogus input.
    d = {k: max(v, 0) for k, v in d.items()}
    return d, rebooted


def classify(cur, deltas):
    """Return (status, flags, summary). Pure function — unit-testable."""
    flags = []
    status = "ok"

    def escalate(level, msg):
        nonlocal status
        flags.append(msg)
        if level == "crit" or (level == "warn" and status == "ok"):
            status = level if level == "crit" else "warn"

    total, avail = cur["mem_total_kb"], cur["mem_avail_kb"]
    avail_pct = (avail / total * 100) if total else 100.0
    page_kb = cur["page_kb"]
    c, d = cur["counters"], deltas

    if d.get("oom_kill", 0) > 0:
        escalate("crit", f"OOM kills +{d['oom_kill']}")
    if avail_pct < 5:
        escalate("crit", f"MemAvailable {avail_pct:.0f}% (<5%)")
    elif avail_pct < 10:
        escalate("warn", f"MemAvailable {avail_pct:.0f}% (<10%)")

    so, si = d.get("pswpout", 0), d.get("pswpin", 0)
    if so > 0:
        escalate("warn", f"disk swap-out +{so * page_kb // 1024}M")
    if si > 0:
        escalate("warn", f"disk swap-in +{si * page_kb // 1024}M")

    wb = d.get("written_back_pages", d.get("zswpwb", 0))
    if wb > 0:
        escalate("warn", f"zswap writeback +{wb} pages")
    rejects = sum(d.get(k, 0) for k in REJECT_KEYS)
    if rejects > 0:
        escalate("warn", f"zswap rejects +{rejects}")

    pool_fill_pct = None
    if cur["zswap_pool_kb"] is not None and cur["max_pool_pct"] and total:
        cap_kb = total * cur["max_pool_pct"] / 100
        pool_fill_pct = cur["zswap_pool_kb"] / cap_kb * 100
        if pool_fill_pct > 80:
            escalate("warn", f"zswap pool {pool_fill_pct:.0f}% of cap")

    # ── Summary line (context, not alarm) ──────────────────────────────
    def gm(kb):  # kB -> human
        return f"{kb / 1024 ** 2:.1f}G" if kb >= 1024 ** 2 else f"{kb // 1024}M"

    parts = [f"{gm(avail)} avail / {gm(total)} "
             f"({(total - avail) / total * 100:.0f}% used)" if total else "mem: n/a"]

    slots_kb = cur["swap_total_kb"] - cur["swap_free_kb"]
    if cur["zswap_enabled"] and cur["zswapped_kb"] is not None and slots_kb > 0:
        zs, zp = cur["zswapped_kb"], cur["zswap_pool_kb"] or 0
        ratio = f" {zs / zp:.1f}x" if zp else ""
        parts.append(f"swap {gm(slots_kb)} slots: {gm(cur['swap_cached_kb'])} cached"
                     f" + {gm(zs)} zswap({gm(zp)} RAM{ratio})")
        # Ground truth for "is any of it on disk": the writeback counter.
        wb_life = c.get("written_back_pages", c.get("zswpwb"))
        parts.append("disk 0" if wb_life == 0 else
                     f"disk wb {wb_life} pages" if wb_life is not None else
                     f"disk I/O since last: {'+' if so or si else ''}{so + si}p")
    elif slots_kb > 0:
        parts.append(f"swap {gm(slots_kb)} used"
                     f"{', no I/O since last' if not (so or si) else ''}")

    return status, flags, ", ".join(parts)


def main():
    state_path = None
    if len(sys.argv) > 1:
        state_path = pathlib.Path(sys.argv[1]) / "mem_counters.last"

    cur = collect()
    prev = None
    if state_path is not None:
        try:
            prev = json.loads(state_path.read_text())
        except (OSError, ValueError):
            prev = None
    d, rebooted = deltas_since(cur, prev)
    status, flags, summary = classify(cur, d)

    baseline = ("boot" if rebooted or not prev else
                time.strftime("%m-%d %H:%M", time.localtime(prev["ts"])))

    print(f"mem_status={status}")
    print(f"mem_summary={summary}")
    print(f"mem_flags={'; '.join(flags)}")
    print(f"mem_baseline={baseline}")
    for k in sorted(cur["counters"]):
        print(f"raw_{k}={cur['counters'][k]} delta={d.get(k, 0)}")

    if state_path is not None:
        try:
            tmp = state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"boot_id": cur["boot_id"], "ts": cur["ts"],
                                       "counters": cur["counters"]}))
            tmp.replace(state_path)
        except OSError:
            pass  # read-only state dir: every run reports since-boot deltas


if __name__ == "__main__":
    main()
