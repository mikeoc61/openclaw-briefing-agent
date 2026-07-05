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

Privilege model (Linux):
  SMART over USB needs TWO independent gates satisfied:
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

    Delta tracking compares against the last snapshot stored per serial.
    unsafe_shutdowns / media_errors / num_err_log_entries are evaluated by
    *rate of change*, not absolute value, since they accumulate over drive
    life. Pass track_history=False for a stateless evaluation.
    """
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    out: dict = {
        "device": smart_summary.get("device"),
        "kind": smart_summary.get("kind"),
        "verdict": "OK",
        "reasons": [],
        "deltas": {},
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
                        bump(
                            "WARN",
                            f"unsafe_shutdowns increased by {delta} since last "
                            "sweep (unclean power loss)",
                        )

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


def collect(include_summary: bool = True, include_verdict: bool = True) -> dict:
    """Full sweep: enumerate, attempt SMART per device, summarize, evaluate."""
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
            if include_summary:
                entry["summary"] = summ
            if include_verdict:
                entry["verdict"] = health_verdict(summ, serial=info.serial)
        result["disks"].append(entry)
    return result


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2))
