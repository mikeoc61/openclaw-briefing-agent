#!/usr/bin/env python3
"""Collect a small, sanitized SMART snapshot for an unprivileged consumer.

Install this as a root-owned executable outside the writable project tree.
It accepts only explicit device specifications and writes atomically. Full
smartctl output is never persisted.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1
SMARTCTL_TIMEOUT_SECONDS = 30


def _value(value):
    if isinstance(value, dict):
        return value.get("value", value.get("raw", value.get("string")))
    return value


def _parse_device_spec(value: str) -> tuple[str, str, str]:
    try:
        label, path, device_type = value.split("|", 2)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "device must be LABEL|PATH|SMARTCTL_TYPE"
        ) from exc
    if not label or not path.startswith("/dev/disk/by-id/") or not device_type:
        raise argparse.ArgumentTypeError(
            "device requires a label, /dev/disk/by-id path, and type"
        )
    return label, path, device_type


def _error_text(stdout: str, stderr: str, returncode: int) -> str:
    for text in (stderr, stdout):
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith(("{", "}", '"')):
                return stripped[:300]
    return f"smartctl failed (exit bitmask {returncode})"


def _summary(data: dict, device: str) -> dict:
    status = data.get("smart_status")
    passed = status.get("passed") if isinstance(status, dict) else None
    smart_status = None if passed is None else ("PASSED" if passed else "FAILING")
    nvme = data.get("nvme_smart_health_information_log")
    if not isinstance(nvme, dict):
        raise ValueError("NVMe SMART health log missing")
    return {
        "device": device,
        "ok": True,
        "smart_status": smart_status,
        "device_type": "nvme",
        "kind": "nvme",
        "critical_warning": _value(nvme.get("critical_warning")),
        "percentage_used": _value(nvme.get("percentage_used")),
        "available_spare": _value(nvme.get("available_spare")),
        "available_spare_threshold": _value(nvme.get("available_spare_threshold")),
        "media_errors": _value(nvme.get("media_errors")),
        "num_err_log_entries": _value(nvme.get("num_err_log_entries")),
        "unsafe_shutdowns": _value(nvme.get("unsafe_shutdowns")),
        "power_cycles": _value(nvme.get("power_cycles")),
        "power_on_hours": _value(nvme.get("power_on_hours")),
        "data_units_written": _value(nvme.get("data_units_written")),
        "data_units_read": _value(nvme.get("data_units_read")),
        "temperature_c": _value(nvme.get("temperature")),
    }


def collect_device(label: str, path: str, device_type: str) -> dict:
    entry = {"label": label, "device_id": path, "device_type": device_type}
    try:
        resolved = str(Path(path).resolve(strict=True))
    except OSError as exc:
        entry["summary"] = {
            "device": path,
            "ok": False,
            "error_kind": "missing",
            "error": f"device unavailable: {exc}",
        }
        return entry

    try:
        proc = subprocess.run(
            ["/usr/sbin/smartctl", "-j", "-d", device_type, "-x", path],
            capture_output=True,
            text=True,
            timeout=SMARTCTL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        entry["summary"] = {
            "device": path,
            "ok": False,
            "error_kind": "collector",
            "error": f"smartctl execution failed: {exc}",
        }
        return entry

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        data = {}

    entry.update(
        {
            "device": resolved,
            "model": data.get("model_name") or label,
            "serial": data.get("serial_number"),
            "transport": "usb" if "usb-" in Path(path).name else "nvme",
        }
    )
    if proc.returncode & 0b11:
        entry["summary"] = {
            "device": path,
            "ok": False,
            "error_kind": "collector",
            "error": _error_text(proc.stdout, proc.stderr, proc.returncode),
        }
        return entry

    try:
        entry["summary"] = _summary(data, path)
    except ValueError as exc:
        entry["summary"] = {
            "device": path,
            "ok": False,
            "error_kind": "collector",
            "error": str(exc),
        }
    return entry


def write_snapshot(output: Path, payload: dict) -> None:
    output.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, output)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", action="append", type=_parse_device_spec, required=True)
    args = parser.parse_args()

    now = time.time()
    disks = [collect_device(*spec) for spec in args.device]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_epoch": int(now),
        "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "disks": disks,
    }
    write_snapshot(args.output, payload)
    return 1 if any(not disk.get("summary", {}).get("ok") for disk in disks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
