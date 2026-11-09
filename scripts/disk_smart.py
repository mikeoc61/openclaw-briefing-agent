#!/usr/bin/env python3
"""
disk_smart.py — cross-platform disk enumeration and SMART attribute retrieval.

Two distinct problems are kept separate on purpose:

  1. Enumeration / identification (model, serial, bus, vendor/product IDs).
     Reasonably portable across Linux and macOS.

  2. SMART attribute retrieval over USB bridges.
     NOT reliably portable. Linux usually works with the correct smartctl
     `-d` device-type token; macOS frequently cannot pass SMART over USB
     at all, regardless of flags. This module probes, caches what works,
     and degrades gracefully.

Privilege model:
  Hardened Linux deployments consume a fresh, root-owned snapshot by default.
  Missing, stale, or insecure snapshots produce a warning; the default
  briefing path never falls back to direct device access. Set
  SMART_SNAPSHOT_REQUIRED=0 only for standalone use on systems that
  intentionally allow direct SMART probing.

Direct probing requires two independent gates on Linux:
    a. open() of the device node  -> filesystem DAC: user in the `disk`
       group, OR CAP_DAC_OVERRIDE, OR root, OR sudo.
    b. ATA/NVMe pass-through ioctl -> CAP_SYS_RAWIO on the smartctl binary,
       OR root, OR sudo.
  Granting cap_sys_rawio alone is insufficient: open() still fails EACCES.

  Strategy is selected by SMART_PRIVILEGE_MODE:
    direct (default) - call smartctl directly; requires (a)+(b) pre-granted
                       via `disk` group + setcap, or run as root.
    sudo             - call `sudo -n smartctl`; requires a NOPASSWD sudoers
                       entry scoped to smartctl.
    auto             - try direct, fall back to sudo on a permission error.

Standalone test harness: run directly to dump enumeration + SMART + summary
for all detected devices as JSON.

Dependencies: smartctl (smartmontools) for SMART data. Enumeration uses only
OS-native tools (udevadm/lsblk on Linux, system_profiler/diskutil on macOS).
"""

from __future__ import annotations

import json
import os
import platform
import plistlib
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


CACHE_PATH = Path(
    os.environ.get(
        "SYS_AGENT_DEVICE_CACHE",
        os.path.expanduser("~/.config/sys_agent/device_config.json"),
    )
)

CACHE_TTL_SECONDS = 30 * 24 * 3600

PRIVILEGE_MODE = os.environ.get("SMART_PRIVILEGE_MODE", "direct").lower()

SNAPSHOT_PATH = Path(
    os.environ.get(
        "SMART_SNAPSHOT_PATH",
        "/var/cache/openclaw-health/disk-smart.json",
    )
)
SNAPSHOT_MAX_AGE_SECONDS = int(
    os.environ.get("SMART_SNAPSHOT_MAX_AGE_SECONDS", str(2 * 3600))
)
SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_REQUIRED = os.environ.get(
    "SMART_SNAPSHOT_REQUIRED",
    "1" if platform.system().lower() == "linux" else "0",
) != "0"

DEVICE_TYPE_CANDIDATES = ["sat", "sntasmedia", "sntjmicron", "scsi", "nvme", "auto"]

# Known USB bridge / device IDs -> required smartctl -d token.
# 04e8:4001 is the Samsung Portable SSD T7 (NVMe drive behind a Samsung-
# branded ASMedia bridge); it self-reports as "USB NVMe ASMedia".
KNOWN_BRIDGE_HINTS = {
    "04e8:4001": "sntasmedia",
    "174c:55aa": "sntasmedia",
    "174c:1153": "sntasmedia",
    "152d:0578": "sntjmicron",
    "152d:0583": "sntjmicron",
    "152d:9561": "sntjmicron",
}

# USB vendor IDs whose devices are NVMe-behind-bridge; for these, skip the
# pointless `sat` probe (SAT is ATA-only and will never work).
NVME_BRIDGE_VENDORS = {"04e8"}

# Transports and smartctl -d tokens that mean "NVMe behind a USB bridge".
# This distinction drives unsafe_shutdowns interpretation: on a bridged
# device the kernel speaks UAS/SCSI to the bridge and never NVMe, so nothing
# in the Linux shutdown path issues an NVMe shutdown notification (CC.SHN)
# to the controller behind it. Whether one is sent is bridge firmware's
# business, and many bridges never do -- so VBUS dropping during an orderly
# reboot still increments the counter. See classify_unsafe_shutdowns().
BRIDGED_TRANSPORTS = {"usb", "ieee1394", "firewire"}
BRIDGED_DEVICE_TYPES = {"sat", "scsi", "sntasmedia", "sntjmicron"}

# Kernel boot UUID; changes on every boot. Absent on non-Linux.
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")

# Device-node basename prefixes that do not implement ATA or NVMe SMART.
NO_SMART_PREFIXES = ("mmcblk", "loop", "ram", "zram", "md", "dm-", "sr")

SUBPROCESS_TIMEOUT = 20

PERMISSION_MARKERS = ("permission denied", "operation not permitted")

# ATA attribute names that matter for an IO-health probe. Vendor labels vary;
# matching is substring, case-insensitive.
HEALTH_ATTR_KEYS = {
    "reallocated": ["reallocated_sector", "reallocated_event"],
    "pending": ["current_pending_sector"],
    "uncorrectable": ["offline_uncorrectable", "reported_uncorrect"],
    "wear": ["wear_leveling", "media_wearout", "percent_lifetime", "ssd_life_left"],
    "power_on_hours": ["power_on_hours"],
    "power_cycles": ["power_cycle_count"],
    "temperature": ["temperature_celsius", "airflow_temperature"],
    "udma_crc": ["udma_crc_error"],
}

# NVMe critical_warning is a bitmask (NVMe spec, Figure: SMART / Health Log).
# Any non-zero value is the controller declaring a fault.
NVME_CRITICAL_WARNING_BITS = {
    0: "available spare below threshold",
    1: "temperature outside operating range",
    2: "reliability degraded (excessive media errors)",
    3: "media placed in read-only mode",
    4: "volatile memory backup failed",
    5: "persistent memory region unreliable",
}

# Verdict thresholds. Override per-call via the `thresholds` arg to
# health_verdict(). Values reflect NVMe/ATA spec semantics, not arbitrary picks.
DEFAULT_THRESHOLDS = {
    "nvme_pct_used_warn": 80,      # endurance estimate; >=100 still operable
    "temp_warn_c": 70,            # typical NVMe thermal-throttle onset
    "ata_wear_margin": 10,        # normalized wear value this far above thresh
}


def _run(cmd: list[str], timeout: int = SUBPROCESS_TIMEOUT) -> tuple[int, str, str]:
    """Run a command, returning (returncode, stdout, stderr). Never raises."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s: {' '.join(cmd)}"
    except Exception as exc:
        return 1, "", f"{type(exc).__name__}: {exc}"


def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


def _is_permission_failure(rc: int, out: str, err: str) -> bool:
    """A permission failure is identified by smartctl's stderr/stdout text,
    NOT by exit code. smartctl's exit is a bitmask; bit 1 (value 2) is set
    for ANY device-open or IDENTIFY failure -- wrong -d type, dead bridge,
    permission denial alike -- so rc cannot discriminate the cause."""
    blob = (out + " " + err).lower()
    return any(marker in blob for marker in PERMISSION_MARKERS)


def _permission_error_line(out: str, err: str) -> str:
    """Return the specific line that carries a permission marker.

    Cannot use splitlines()[-1]: under `-j` the readable diagnostic
    ('Smartctl open device: ... failed: Permission denied') is emitted
    alongside partial JSON, and the last physical line is often a stray
    '}' from the truncated JSON envelope, not the message."""
    for source in (err, out):
        for line in source.splitlines():
            low = line.lower()
            if any(marker in low for marker in PERMISSION_MARKERS):
                return line.strip()
    return "open failed"


@dataclass
class DiskInfo:
    device: str
    model: Optional[str] = None
    serial: Optional[str] = None
    bus: Optional[str] = None
    transport: Optional[str] = None
    vendor_id: Optional[str] = None
    product_id: Optional[str] = None
    bridge_hint: Optional[str] = None
    smart_capable: bool = True
    platform: str = field(default_factory=lambda: platform.system().lower())


@dataclass
class SmartResult:
    device: str
    ok: bool
    device_type: Optional[str] = None
    smart_status: Optional[str] = None
    attributes: dict = field(default_factory=dict)
    source: Optional[str] = None
    error: Optional[str] = None
    error_kind: Optional[str] = None  # privilege | device_type | unsupported | missing
    from_cache: bool = False


def _load_cache() -> dict:
    try:
        with open(CACHE_PATH, "r") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        with open(tmp, "w") as fh:
            json.dump(cache, fh, indent=2, sort_keys=True)
        tmp.replace(CACHE_PATH)
    except OSError as exc:
        print(f"warning: could not write cache {CACHE_PATH}: {exc}", file=sys.stderr)


def _cache_get(serial: Optional[str]) -> Optional[dict]:
    if not serial:
        return None
    entry = _load_cache().get(serial)
    if not entry:
        return None
    if time.time() - entry.get("last_verified", 0) > CACHE_TTL_SECONDS:
        return None
    return entry


def _cache_put(serial: Optional[str], device_type: str, plat: str) -> None:
    if not serial:
        return
    cache = _load_cache()
    cache[serial] = {
        "device_type": device_type,
        "platform": plat,
        "last_verified": int(time.time()),
    }
    _save_cache(cache)


def _cache_invalidate(serial: Optional[str]) -> None:
    """Drop the device_type entry only; preserve verdict history."""
    if not serial:
        return
    cache = _load_cache()
    entry = cache.get(serial)
    if entry:
        for key in ("device_type", "platform", "last_verified"):
            entry.pop(key, None)
        if entry:
            cache[serial] = entry
        else:
            del cache[serial]
        _save_cache(cache)


def _history_get(serial: Optional[str]) -> dict:
    """Return the prior verdict-relevant snapshot for delta comparison."""
    if not serial:
        return {}
    entry = _load_cache().get(serial) or {}
    return entry.get("history", {})


def _history_put(serial: Optional[str], snapshot: dict) -> None:
    """Persist a verdict snapshot under the serial's history sub-key.

    Stored alongside device_type in the same cache file; uses a distinct
    'history' sub-key so probe-cache invalidation never erases it."""
    if not serial:
        return
    cache = _load_cache()
    entry = cache.get(serial, {})
    entry["history"] = {**snapshot, "recorded": int(time.time())}
    cache[serial] = entry
    _save_cache(cache)


def _smart_capable(node_basename: str) -> bool:
    return not node_basename.startswith(NO_SMART_PREFIXES)


def _enumerate_linux() -> list[DiskInfo]:
    disks: list[DiskInfo] = []
    rc, out, _ = _run(["lsblk", "-J", "-d", "-o", "NAME,TYPE,TRAN"])
    names: list[tuple[str, Optional[str]]] = []
    if rc == 0:
        try:
            for dev in json.loads(out).get("blockdevices", []):
                if dev.get("type") == "disk":
                    names.append((dev["name"], dev.get("tran")))
        except (json.JSONDecodeError, KeyError):
            pass
    if not names:
        seen = [p.name for p in Path("/sys/block").glob("sd*")]
        seen += [p.name for p in Path("/sys/block").glob("nvme*")]
        seen += [p.name for p in Path("/sys/block").glob("mmcblk*")]
        names = [(n, None) for n in seen]

    for name, tran in names:
        node = f"/dev/{name}"
        info = DiskInfo(device=node, transport=tran, smart_capable=_smart_capable(name))
        rc, out, _ = _run(["udevadm", "info", "-n", node, "--query=property"])
        if rc == 0:
            props = {}
            for line in out.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    props[k] = v
            info.model = props.get("ID_MODEL") or props.get("ID_MODEL_FROM_DATABASE")
            info.serial = props.get("ID_SERIAL_SHORT") or props.get("ID_SERIAL")
            info.bus = props.get("ID_BUS")
            info.vendor_id = props.get("ID_VENDOR_ID")
            info.product_id = props.get("ID_MODEL_ID")
        if info.vendor_id and info.product_id:
            key = f"{info.vendor_id}:{info.product_id}".lower()
            info.bridge_hint = KNOWN_BRIDGE_HINTS.get(key)
        disks.append(info)
    return disks


def _enumerate_macos() -> list[DiskInfo]:
    disks: list[DiskInfo] = []
    rc, out, _ = _run(["diskutil", "list", "-plist", "physical"])
    nodes: list[str] = []
    if rc == 0:
        try:
            nodes = ["/dev/" + d for d in plistlib.loads(out.encode()).get("WholeDisks", [])]
        except Exception:
            pass

    usb_index: dict[str, dict] = {}
    rc, out, _ = _run(["system_profiler", "SPUSBDataType", "-json"])
    if rc == 0:
        try:
            sp = json.loads(out)

            def walk(items):
                for it in items:
                    if "_items" in it:
                        walk(it["_items"])
                    serial = it.get("serial_num")
                    if serial:
                        usb_index[serial] = it

            walk(sp.get("SPUSBDataType", []))
        except json.JSONDecodeError:
            pass

    for node in nodes:
        base = node.rsplit("/", 1)[-1]
        info = DiskInfo(device=node, smart_capable=_smart_capable(base))
        rc, out, _ = _run(["diskutil", "info", "-plist", node])
        if rc == 0:
            try:
                d = plistlib.loads(out.encode())
                info.model = d.get("MediaName") or d.get("IORegistryEntryName")
                info.serial = d.get("IORegistryEntrySerialNumber")
                proto = (d.get("BusProtocol") or "").lower()
                info.bus = proto or None
                info.transport = proto or None
            except Exception:
                pass
        if info.serial and info.serial in usb_index:
            usb = usb_index[info.serial]
            vid = usb.get("vendor_id", "")
            pid = usb.get("product_id", "")
            for raw, target in ((vid, "vendor_id"), (pid, "product_id")):
                hexpart = raw.split()[0].replace("0x", "").lower() if raw else None
                setattr(info, target, hexpart)
            if info.vendor_id and info.product_id:
                key = f"{info.vendor_id}:{info.product_id}"
                info.bridge_hint = KNOWN_BRIDGE_HINTS.get(key)
        disks.append(info)
    return disks


def enumerate_disks() -> list[DiskInfo]:
    """Identify physical disks (SATA, USB, NVMe, SD/eMMC) on this host.

    SD/eMMC and other non-SMART devices are still listed, with
    smart_capable=False so callers can skip pointless SMART probing.
    """
    plat = platform.system().lower()
    if plat == "linux":
        return _enumerate_linux()
    if plat == "darwin":
        return _enumerate_macos()
    return []


def check_privilege() -> dict:
    """Report whether SMART pass-through is usable, and how.

    Checks BOTH gates: device-node open access and the cap_sys_rawio ioctl
    capability. Reports the active strategy and a concrete recommendation.
    """
    plat = platform.system().lower()
    smartctl = shutil.which("smartctl")
    is_root = (os.geteuid() == 0) if hasattr(os, "geteuid") else False
    state: dict = {
        "platform": plat,
        "smartctl_path": smartctl,
        "privilege_mode": PRIVILEGE_MODE,
        "is_root": is_root,
        "has_cap_sys_rawio": False,
        "in_disk_group": False,
        "sudo_smartctl_ok": False,
        "recommendation": None,
    }
    if not smartctl:
        state["recommendation"] = "install smartmontools"
        return state

    if plat == "linux":
        if _have("getcap"):
            rc, out, _ = _run(["getcap", smartctl])
            state["has_cap_sys_rawio"] = "cap_sys_rawio" in out.lower()
        rc, out, _ = _run(["id", "-Gn"])
        if rc == 0:
            state["in_disk_group"] = "disk" in out.split()
        rc, _, _ = _run(["sudo", "-n", smartctl, "--version"])
        state["sudo_smartctl_ok"] = rc == 0

    direct_ok = is_root or (state["has_cap_sys_rawio"] and state["in_disk_group"])
    if plat != "linux":
        direct_ok = is_root

    if direct_ok:
        state["recommendation"] = "ok: direct SMART access available"
    elif state["sudo_smartctl_ok"]:
        state["recommendation"] = (
            "direct access incomplete; set SMART_PRIVILEGE_MODE=sudo "
            "(NOPASSWD smartctl sudoers entry is present)"
        )
    elif plat == "linux":
        missing = []
        if not state["has_cap_sys_rawio"]:
            missing.append(f"setcap cap_sys_rawio+ep {smartctl}")
        if not state["in_disk_group"]:
            missing.append("usermod -aG disk <user> (re-login required)")
        state["recommendation"] = "needs: " + "; ".join(missing)
    elif plat == "darwin":
        state["recommendation"] = (
            "macOS: USB SMART pass-through often unavailable; "
            "diskutil status-only fallback will be used"
        )
    return state


def _smartctl_cmd(args: list[str]) -> list[str]:
    """Prefix smartctl args per the active privilege strategy."""
    base = ["smartctl"] + args
    if PRIVILEGE_MODE == "sudo":
        return ["sudo", "-n"] + base
    return base


def _ordered_candidates(info: DiskInfo, cached_type: Optional[str]) -> list[str]:
    """Probe order: cached -> bridge hint -> transport/vendor-biased defaults."""
    order: list[str] = []
    for t in (cached_type, info.bridge_hint):
        if t and t not in order:
            order.append(t)
    if (info.transport or "").lower() == "nvme" and "nvme" not in order:
        order.append("nvme")
    skip_sat = (info.vendor_id or "").lower() in NVME_BRIDGE_VENDORS
    for t in DEVICE_TYPE_CANDIDATES:
        if t == "sat" and skip_sat:
            continue
        if t not in order:
            order.append(t)
    return order


def _parse_smartctl_json(text: str) -> tuple[Optional[str], dict]:
    """Extract overall status and per-attribute / NVMe-log data."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, {}
    status = None
    sm = data.get("smart_status")
    if isinstance(sm, dict) and "passed" in sm:
        status = "PASSED" if sm["passed"] else "FAILING"
    attrs: dict = {}
    table = data.get("ata_smart_attributes", {}).get("table", [])
    for row in table:
        name = row.get("name")
        if name:
            attrs[name] = {
                "id": row.get("id"),
                "value": row.get("value"),
                "worst": row.get("worst"),
                "thresh": row.get("thresh"),
                "raw": row.get("raw", {}).get("string"),
                "raw_value": row.get("raw", {}).get("value"),
            }
    nvme = data.get("nvme_smart_health_information_log")
    if nvme:
        attrs["_nvme_log"] = nvme
    return status, attrs


def get_smart(info: DiskInfo) -> SmartResult:
    """Best-effort SMART retrieval. Probes -d candidates, caches what works.

    Skips devices flagged smart_capable=False (SD/eMMC etc.). Honors the
    SMART_PRIVILEGE_MODE strategy; classifies failure as privilege vs
    device_type using smartctl's text output, never the bare exit code.
    """
    node = info.device
    if not info.smart_capable:
        return SmartResult(
            device=node, ok=False, error_kind="unsupported",
            error="device class does not implement ATA/NVMe SMART",
        )
    if not _have("smartctl"):
        return SmartResult(
            device=node, ok=False, error_kind="missing",
            error="smartctl not installed",
        )

    cached = _cache_get(info.serial)
    cached_type = cached.get("device_type") if cached else None

    saw_permission_error = False
    last_err = ""
    for dtype in _ordered_candidates(info, cached_type):
        rc, out, err = _run(_smartctl_cmd(["-j", "-d", dtype, "-x", node]))
        if _is_permission_failure(rc, out, err):
            saw_permission_error = True
            last_err = _permission_error_line(out, err)
            continue
        status, attrs = _parse_smartctl_json(out)
        # bits 0-1 of smartctl's exit bitmask = command/open failure.
        if attrs and not (rc & 0b11):
            _cache_put(info.serial, dtype, info.platform)
            return SmartResult(
                device=node, ok=True, device_type=dtype, smart_status=status,
                attributes=attrs, source=f"smartctl -d {dtype}",
                from_cache=(dtype == cached_type),
            )

    _cache_invalidate(info.serial)

    if saw_permission_error:
        hint = (
            "set SMART_PRIVILEGE_MODE=sudo, or grant disk group + cap_sys_rawio"
        )
        return SmartResult(
            device=node, ok=False, error_kind="privilege",
            error=f"smartctl permission denied ({last_err}); {hint}",
        )

    if platform.system().lower() == "darwin":
        rc, out, _ = _run(["diskutil", "info", "-plist", node])
        if rc == 0:
            try:
                d = plistlib.loads(out.encode())
                smart = d.get("SMARTStatus")
                if smart:
                    return SmartResult(
                        device=node, ok=True, smart_status=smart,
                        source="diskutil (status only; no attributes over USB)",
                    )
            except Exception:
                pass

    return SmartResult(
        device=node, ok=False, error_kind="device_type",
        error="no smartctl -d candidate returned parseable attributes; "
        "USB SMART pass-through likely unavailable for this bridge",
    )


def _match_attr(attrs: dict, needles: list[str]) -> Optional[dict]:
    for name, payload in attrs.items():
        low = name.lower()
        if any(n in low for n in needles):
            return {"attr": name, **payload}
    return None


def summary(result: SmartResult) -> dict:
    """Flatten a SmartResult into the fields an IO-health check needs.

    NVMe: reads the native health log including critical_warning bitmask,
    endurance (percentage_used, data_units_written), spare, and shutdown
    integrity counters. ATA: extracts canonical health attributes.
    """
    out: dict = {
        "device": result.device,
        "ok": result.ok,
        "smart_status": result.smart_status,
        "device_type": result.device_type,
    }
    if not result.ok:
        out["error"] = result.error
        out["error_kind"] = result.error_kind
        return out

    attrs = result.attributes
    def _extract_val(v):
        """Normalize NVMe values that smartctl sometimes emits as dicts."""
        if isinstance(v, dict):
            return v.get("value", v.get("raw", v.get("string", 0)))
        return v

    nvme = attrs.get("_nvme_log")
    if nvme:
        out["kind"] = "nvme"
        out["critical_warning"] = _extract_val(nvme.get("critical_warning"))
        out["percentage_used"] = _extract_val(nvme.get("percentage_used"))
        out["available_spare"] = _extract_val(nvme.get("available_spare"))
        out["available_spare_threshold"] = _extract_val(nvme.get("available_spare_threshold"))
        out["media_errors"] = _extract_val(nvme.get("media_errors"))
        out["num_err_log_entries"] = _extract_val(nvme.get("num_err_log_entries"))
        out["unsafe_shutdowns"] = _extract_val(nvme.get("unsafe_shutdowns"))
        out["power_cycles"] = _extract_val(nvme.get("power_cycles"))
        out["power_on_hours"] = _extract_val(nvme.get("power_on_hours"))
        out["data_units_written"] = _extract_val(nvme.get("data_units_written"))
        out["data_units_read"] = _extract_val(nvme.get("data_units_read"))
        out["temperature_c"] = _extract_val(nvme.get("temperature"))
        return out

    out["kind"] = "ata"
    for label, needles in HEALTH_ATTR_KEYS.items():
        match = _match_attr(attrs, needles)
        if match:
            out[label] = {
                "raw": match.get("raw"),
                "raw_value": match.get("raw_value"),
                "normalized": match.get("value"),
                "worst": match.get("worst"),
                "thresh": match.get("thresh"),
            }
    return out


def _verdict_escalate(current: str, new: str) -> str:
    """Return the more severe of two verdict levels."""
    rank = {"OK": 0, "WARN": 1, "CRITICAL": 2}
    return current if rank[current] >= rank[new] else new


def _read_boot_id() -> Optional[str]:
    """Current kernel boot UUID, or None where /proc is unavailable."""
    try:
        return BOOT_ID_PATH.read_text().strip() or None
    except OSError:
        return None


def _is_bridged(smart_summary: dict) -> bool:
    """True when the drive sits behind a USB/1394 bridge rather than on a
    native NVMe/SATA port. Transport is authoritative when known; the
    privileged snapshot hardcodes device_type="nvme" for bridged NVMe, so
    the -d token is only a fallback for the direct-probe path."""
    transport = (smart_summary.get("transport") or "").lower()
    if transport:
        return transport in BRIDGED_TRANSPORTS
    return (smart_summary.get("device_type") or "").lower() in BRIDGED_DEVICE_TYPES


def classify_unsafe_shutdowns(
    delta: int,
    power_cycle_delta: Optional[int],
    bridged: bool,
    rebooted: Optional[bool],
) -> tuple[Optional[str], str]:
    """Decide what an unsafe_shutdowns increase actually means.

    Returns (level, reason); level None means "note it, do not raise a
    verdict". The NVMe counter records that the controller lost power
    without logging a shutdown notification -- NOT that the OS lost power.
    Power cycles are the denominator that separates the readings:

      * more unsafe shutdowns than power cycles -> losses the host cannot
        account for. Unexplained, so warn.
      * every unsafe shutdown matched by a power cycle, no intervening host
        reboot -> the drive power-cycled while the host stayed up. A
        marginal cable or supply dropping a mounted volume; warn loudly.
      * matched across a host reboot, bridged device -> the bridge dropped
        VBUS without passing a shutdown notification down. Expected for
        this hardware; note it.
      * matched across a host reboot, directly attached -> a native
        controller should log a clean shutdown. Still anomalous; warn.

    A matched count across a reboot cannot prove *every* increment was a
    reboot (a cable glitch in the same window looks identical), so the note
    reports both numbers rather than declaring the drive healthy.
    """
    if power_cycle_delta is None:
        return (
            "WARN",
            f"unsafe_shutdowns increased by {delta} since last sweep "
            "(cause unclassified: no prior power_cycles reading)",
        )
    if delta > power_cycle_delta:
        return (
            "WARN",
            f"unsafe_shutdowns +{delta} exceeds power_cycles "
            f"+{power_cycle_delta} since last sweep: power losses the host "
            "cannot account for",
        )
    if rebooted is False:
        return (
            "WARN",
            f"unsafe_shutdowns +{delta} (power_cycles +{power_cycle_delta}) "
            "with no host reboot: drive lost power while mounted; check "
            "cable and supply",
        )
    if rebooted is None:
        return (
            "WARN",
            f"unsafe_shutdowns +{delta} matched by power_cycles "
            f"+{power_cycle_delta}, but boot history is unavailable, so an "
            "unexpected power loss is not ruled out",
        )
    if not bridged:
        return (
            "WARN",
            f"unsafe_shutdowns +{delta} across a host reboot on a directly "
            "attached device, which should log a clean shutdown",
        )
    return (
        None,
        f"unsafe_shutdowns +{delta} matched by power_cycles "
        f"+{power_cycle_delta} across a host reboot; this bridged device "
        "records no NVMe shutdown notification on reboot, so this is not "
        "evidence of unexpected power loss",
    )


def health_verdict(
    smart_summary: dict,
    serial: Optional[str] = None,
    thresholds: Optional[dict] = None,
    track_history: bool = True,
) -> dict:
    """Evaluate a summary() dict into an OK / WARN / CRITICAL verdict.

    Returns: {device, verdict, reasons[], deltas{}, kind}.
      verdict  - worst level across all checks
      reasons  - human-readable strings, one per triggered check
      deltas   - changes vs the prior sweep (history-cache backed)
      notes    - explained, non-actionable observations; no verdict impact

    Delta tracking compares against the last snapshot stored per serial.
    unsafe_shutdowns / media_errors / num_err_log_entries are evaluated by
    *rate of change*, not absolute value, since they accumulate over drive
    life. Pass track_history=False for a stateless evaluation.

    power_cycles and the kernel boot id are snapshotted alongside them so an
    unsafe_shutdowns increase can be attributed -- a bridged drive logs one
    on every orderly reboot, which is not the same event as losing power
    while mounted. See classify_unsafe_shutdowns().
    """
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    out: dict = {
        "device": smart_summary.get("device"),
        "kind": smart_summary.get("kind"),
        "verdict": "OK",
        "reasons": [],
        "deltas": {},
        "notes": [],
    }

    if not smart_summary.get("ok"):
        kind = smart_summary.get("error_kind")
        if kind == "unsupported":
            out["verdict"] = "OK"
            out["reasons"].append("device does not implement SMART; not evaluated")
        else:
            out["verdict"] = "WARN"
            out["reasons"].append(
                f"SMART data unavailable ({kind}): {smart_summary.get('error')}"
            )
        return out

    def bump(level: str, reason: str) -> None:
        out["verdict"] = _verdict_escalate(out["verdict"], level)
        out["reasons"].append(reason)

    prior = _history_get(serial) if track_history else {}
    snapshot: dict = {}

    if smart_summary.get("kind") == "nvme":
        cw = smart_summary.get("critical_warning")
        if cw:
            bits = [
                NVME_CRITICAL_WARNING_BITS.get(b, f"bit {b}")
                for b in range(8)
                if cw & (1 << b)
            ]
            bump("CRITICAL", f"NVMe critical_warning=0x{cw:02x}: {', '.join(bits)}")

        spare = smart_summary.get("available_spare")
        spare_thr = smart_summary.get("available_spare_threshold")
        if spare is not None and spare_thr is not None and spare < spare_thr:
            bump("CRITICAL", f"available_spare {spare}% below threshold {spare_thr}%")

        pct = smart_summary.get("percentage_used")
        if pct is not None and pct >= th["nvme_pct_used_warn"]:
            bump("WARN", f"endurance percentage_used at {pct}%")

        media = smart_summary.get("media_errors")
        if media:
            bump("WARN", f"media_errors={media} (uncorrected integrity events)")

        temp = smart_summary.get("temperature_c")
        if temp is not None and temp >= th["temp_warn_c"]:
            bump("WARN", f"temperature {temp}C at/above {th['temp_warn_c']}C")

        # power_cycles and boot_id exist in the snapshot purely to interpret
        # an unsafe_shutdowns increase; neither raises a verdict on its own.
        pc_delta = None
        pc_cur = smart_summary.get("power_cycles")
        if pc_cur is not None:
            snapshot["power_cycles"] = pc_cur
            pc_prior = prior.get("power_cycles")
            if isinstance(pc_cur, int) and isinstance(pc_prior, int):
                pc_delta = pc_cur - pc_prior
                if pc_delta < 0:
                    pc_delta = None  # serial reuse or bogus input; unusable
                else:
                    out["deltas"]["power_cycles"] = pc_delta

        cur_boot = _read_boot_id()
        if cur_boot:
            snapshot["boot_id"] = cur_boot
        prior_boot = prior.get("boot_id")
        rebooted = (cur_boot != prior_boot) if (cur_boot and prior_boot) else None

        for fld in ("unsafe_shutdowns", "media_errors", "num_err_log_entries"):
            cur = smart_summary.get(fld)
            snapshot[fld] = cur
            if cur is not None and fld in prior and prior[fld] is not None:
                delta = cur - prior[fld]
                if delta > 0:
                    out["deltas"][fld] = delta
                    if fld in ("media_errors", "num_err_log_entries"):
                        bump("WARN", f"{fld} increased by {delta} since last sweep")
                    elif fld == "unsafe_shutdowns":
                        level, reason = classify_unsafe_shutdowns(
                            delta,
                            pc_delta,
                            _is_bridged(smart_summary),
                            rebooted,
                        )
                        if level:
                            bump(level, reason)
                        else:
                            out["notes"].append(reason)

    elif smart_summary.get("kind") == "ata":
        for fld, level in (
            ("pending", "CRITICAL"),
            ("uncorrectable", "CRITICAL"),
            ("reallocated", "WARN"),
            ("udma_crc", "WARN"),
        ):
            attr = smart_summary.get(fld)
            if attr:
                raw = attr.get("raw_value")
                if raw:
                    snapshot[fld] = raw
                    label = {
                        "pending": "current pending sectors",
                        "uncorrectable": "offline-uncorrectable sectors",
                        "reallocated": "reallocated sectors",
                        "udma_crc": "UDMA CRC errors (cable/bridge)",
                    }[fld]
                    bump(level, f"{label}: {raw}")

        wear = smart_summary.get("wear")
        if wear:
            val = wear.get("normalized")
            thresh = wear.get("thresh")
            if val is not None and thresh is not None:
                if val <= thresh:
                    bump("CRITICAL", f"wear normalized {val} at/below threshold {thresh}")
                elif val - thresh <= th["ata_wear_margin"]:
                    bump("WARN", f"wear normalized {val} near threshold {thresh}")

    if smart_summary.get("smart_status") == "FAILING":
        bump("CRITICAL", "drive self-reports SMART status FAILING")

    if track_history and snapshot:
        _history_put(serial, snapshot)

    if not out["reasons"]:
        out["reasons"].append("all evaluated checks within thresholds")
    return out


def _snapshot_error_entry(message: str, disk: Optional[dict] = None) -> dict:
    disk = disk or {}
    device = disk.get("device_id") or disk.get("device") or str(SNAPSHOT_PATH)
    info = {
        "device": device,
        "model": disk.get("model") or disk.get("label") or "SMART snapshot",
        "serial": disk.get("serial"),
        "bus": None,
        "transport": disk.get("transport"),
        "vendor_id": None,
        "product_id": None,
        "bridge_hint": disk.get("device_type"),
        "smart_capable": True,
        "platform": "linux",
    }
    summ = {
        "device": device,
        "ok": False,
        "smart_status": None,
        "device_type": disk.get("device_type"),
        "error_kind": "snapshot",
        "error": message,
    }
    return {
        "info": info,
        "smart": {
            "device": device,
            "ok": False,
            "device_type": disk.get("device_type"),
            "smart_status": None,
            "attributes": {},
            "source": "privileged SMART snapshot",
            "error": message,
            "error_kind": "snapshot",
            "from_cache": True,
        },
        "summary": summ,
        "verdict": health_verdict(summ, serial=info.get("serial")),
    }


def _collect_from_snapshot() -> tuple[Optional[dict], Optional[str]]:
    """Load a fresh, root-owned snapshot, returning (result, error).

    A present but invalid snapshot is never silently ignored: callers surface a
    warning rather than falling back to privileged device access or reporting
    stale health as current.
    """
    try:
        st = SNAPSHOT_PATH.stat()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"cannot stat {SNAPSHOT_PATH}: {exc}"

    if not stat.S_ISREG(st.st_mode):
        return None, "snapshot path is not a regular file"
    if st.st_uid != 0:
        return None, f"snapshot owner uid is {st.st_uid}, expected root"
    if st.st_mode & 0o022:
        return None, "snapshot is group/world writable"
    try:
        parent_st = SNAPSHOT_PATH.parent.stat()
    except OSError as exc:
        return None, f"cannot stat snapshot directory: {exc}"
    if parent_st.st_uid != 0 or parent_st.st_mode & 0o022:
        return None, "snapshot directory is not root-owned and non-writable"

    try:
        payload = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"snapshot unreadable: {exc}"

    if payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        return None, "snapshot schema version is unsupported"
    generated = payload.get("generated_at_epoch")
    if not isinstance(generated, (int, float)):
        return None, "snapshot timestamp is missing"
    age = time.time() - generated
    if age < -300:
        return None, "snapshot timestamp is in the future"
    if age > SNAPSHOT_MAX_AGE_SECONDS:
        return None, f"snapshot is stale ({int(age // 60)} minutes old)"
    disks = payload.get("disks")
    if not isinstance(disks, list) or not disks:
        return None, "snapshot contains no disks"

    entries = []
    for disk in disks:
        if not isinstance(disk, dict) or not isinstance(disk.get("summary"), dict):
            return None, "snapshot contains an invalid disk entry"
        device = disk.get("device_id") or disk.get("device") or "unknown"
        info = {
            "device": device,
            "model": disk.get("model") or disk.get("label"),
            "serial": disk.get("serial"),
            "bus": None,
            "transport": disk.get("transport"),
            "vendor_id": None,
            "product_id": None,
            "bridge_hint": disk.get("device_type"),
            "smart_capable": True,
            "platform": "linux",
        }
        summ = dict(disk["summary"])
        summ["device"] = device
        # The collector records transport on the disk entry, not the summary;
        # the verdict needs it to tell a bridged drive from a native one.
        summ.setdefault("transport", disk.get("transport"))
        smart_ok = bool(summ.get("ok"))
        smart = {
            "device": device,
            "ok": smart_ok,
            "device_type": summ.get("device_type") or disk.get("device_type"),
            "smart_status": summ.get("smart_status"),
            "attributes": {},
            "source": "privileged SMART snapshot",
            "error": summ.get("error"),
            "error_kind": summ.get("error_kind"),
            "from_cache": True,
        }
        entries.append(
            {
                "info": info,
                "smart": smart,
                "summary": summ,
                "verdict": health_verdict(summ, serial=info.get("serial")),
            }
        )
    return {
        "platform": "linux",
        "source": "privileged SMART snapshot",
        "snapshot_generated_at": payload.get("generated_at"),
        "disks": entries,
    }, None


def collect(include_summary: bool = True, include_verdict: bool = True) -> dict:
    """Use a trusted snapshot, requiring one by default on Linux."""
    snapshot, snapshot_error = _collect_from_snapshot()
    if snapshot:
        return snapshot
    if snapshot_error is None and SNAPSHOT_REQUIRED:
        snapshot_error = f"snapshot is missing ({SNAPSHOT_PATH})"
    if snapshot_error:
        return {
            "platform": platform.system().lower(),
            "source": "privileged SMART snapshot",
            "disks": [_snapshot_error_entry(snapshot_error)],
        }

    result: dict = {
        "platform": platform.system().lower(),
        "privilege": check_privilege(),
        "disks": [],
    }
    for info in enumerate_disks():
        smart = get_smart(info)
        entry: dict = {"info": asdict(info), "smart": asdict(smart)}
        if include_summary or include_verdict:
            summ = summary(smart)
            summ.setdefault("transport", info.transport)
            if include_summary:
                entry["summary"] = summ
            if include_verdict:
                entry["verdict"] = health_verdict(summ, serial=info.serial)
        result["disks"].append(entry)
    return result


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2))
