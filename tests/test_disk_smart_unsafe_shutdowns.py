"""An unsafe_shutdowns increase is a transport question, not a verdict.

The NVMe counter records that the controller lost power without logging a
shutdown notification. On a USB-bridged drive that happens on every orderly
reboot, because the kernel speaks UAS/SCSI to the bridge and never issues an
NVMe shutdown notification to the controller behind it. These tests pin the
cases apart so the briefing stops calling a clean reboot a power loss while
still shouting about a drive that drops while mounted.
"""

import disk_smart
import pytest


BRIDGED = {"kind": "nvme", "transport": "usb", "device_type": "nvme"}
NATIVE = {"kind": "nvme", "transport": "nvme", "device_type": "nvme"}


# (delta, power_cycle_delta, bridged, rebooted) -> (level, text fragment)
CASES = [
    # No denominator: cannot attribute, so do not pretend either way.
    ((3, None, True, True), ("WARN", "cause unclassified")),
    # More losses than power cycles: unexplained whatever the transport.
    ((4, 3, True, True), ("WARN", "cannot account for")),
    ((1, 0, True, True), ("WARN", "cannot account for")),
    # Matched, but the host never rebooted: dropped while mounted.
    ((1, 1, True, False), ("WARN", "no host reboot")),
    ((3, 3, False, False), ("WARN", "no host reboot")),
    # Matched across a reboot on a bridge: expected, note only.
    ((3, 3, True, True), (None, "not evidence of unexpected power loss")),
    ((3, 4, True, True), (None, "not evidence of unexpected power loss")),
    # Matched across a reboot on a native controller: still anomalous.
    ((3, 3, False, True), ("WARN", "directly attached")),
    # Boot history unknown: hedge rather than clear it.
    ((3, 3, True, None), ("WARN", "boot history is unavailable")),
]


@pytest.mark.parametrize("args,expected", CASES)
def test_classification_table(args, expected):
    level, reason = disk_smart.classify_unsafe_shutdowns(*args)
    assert level == expected[0]
    assert expected[1] in reason


def test_no_case_claims_unclean_power_loss():
    """The old wording asserted an OS-level power loss the counter cannot see."""
    for args, _ in CASES:
        _, reason = disk_smart.classify_unsafe_shutdowns(*args)
        assert "unclean power loss" not in reason


def _verdict(monkeypatch, tmp_path, summary, *, prior, boot_id):
    monkeypatch.setattr(disk_smart, "CACHE_PATH", tmp_path / "device_config.json")
    monkeypatch.setattr(disk_smart, "_read_boot_id", lambda: boot_id)
    if prior is not None:
        disk_smart._history_put("SERIAL1", prior)
    return disk_smart.health_verdict(summary, serial="SERIAL1")


def test_bridged_reboot_is_ok_with_a_note(monkeypatch, tmp_path):
    summary = {
        **BRIDGED,
        "ok": True,
        "smart_status": "PASSED",
        "unsafe_shutdowns": 84,
        "power_cycles": 120,
        "media_errors": 0,
        "num_err_log_entries": 0,
    }
    out = _verdict(
        monkeypatch,
        tmp_path,
        summary,
        prior={"unsafe_shutdowns": 81, "power_cycles": 117, "boot_id": "old-boot"},
        boot_id="new-boot",
    )
    assert out["verdict"] == "OK"
    assert out["reasons"] == ["all evaluated checks within thresholds"]
    assert out["deltas"]["unsafe_shutdowns"] == 3
    assert out["deltas"]["power_cycles"] == 3
    assert len(out["notes"]) == 1
    assert "not evidence of unexpected power loss" in out["notes"][0]


def test_drive_dropping_while_host_stays_up_still_warns(monkeypatch, tmp_path):
    summary = {
        **BRIDGED,
        "ok": True,
        "smart_status": "PASSED",
        "unsafe_shutdowns": 82,
        "power_cycles": 118,
        "media_errors": 0,
        "num_err_log_entries": 0,
    }
    out = _verdict(
        monkeypatch,
        tmp_path,
        summary,
        prior={"unsafe_shutdowns": 81, "power_cycles": 117, "boot_id": "same-boot"},
        boot_id="same-boot",
    )
    assert out["verdict"] == "WARN"
    assert "no host reboot" in out["reasons"][0]
    assert out["notes"] == []


def test_losses_exceeding_power_cycles_warn(monkeypatch, tmp_path):
    summary = {
        **BRIDGED,
        "ok": True,
        "smart_status": "PASSED",
        "unsafe_shutdowns": 84,
        "power_cycles": 118,
        "media_errors": 0,
        "num_err_log_entries": 0,
    }
    out = _verdict(
        monkeypatch,
        tmp_path,
        summary,
        prior={"unsafe_shutdowns": 81, "power_cycles": 117, "boot_id": "old-boot"},
        boot_id="new-boot",
    )
    assert out["verdict"] == "WARN"
    assert "cannot account for" in out["reasons"][0]


def test_native_controller_logging_unsafe_on_reboot_warns(monkeypatch, tmp_path):
    summary = {
        **NATIVE,
        "ok": True,
        "smart_status": "PASSED",
        "unsafe_shutdowns": 3,
        "power_cycles": 40,
        "media_errors": 0,
        "num_err_log_entries": 0,
    }
    out = _verdict(
        monkeypatch,
        tmp_path,
        summary,
        prior={"unsafe_shutdowns": 0, "power_cycles": 37, "boot_id": "old-boot"},
        boot_id="new-boot",
    )
    assert out["verdict"] == "WARN"
    assert "directly attached" in out["reasons"][0]


def test_power_cycles_are_snapshotted_for_the_next_sweep(monkeypatch, tmp_path):
    """Without this the denominator never exists and every sweep hedges."""
    summary = {
        **BRIDGED,
        "ok": True,
        "smart_status": "PASSED",
        "unsafe_shutdowns": 81,
        "power_cycles": 117,
        "media_errors": 0,
        "num_err_log_entries": 0,
    }
    _verdict(monkeypatch, tmp_path, summary, prior=None, boot_id="boot-a")
    history = disk_smart._history_get("SERIAL1")
    assert history["power_cycles"] == 117
    assert history["boot_id"] == "boot-a"


def test_first_sweep_after_rollout_hedges_rather_than_asserting(monkeypatch, tmp_path):
    """Prior history predates power_cycles tracking: no denominator yet."""
    summary = {
        **BRIDGED,
        "ok": True,
        "smart_status": "PASSED",
        "unsafe_shutdowns": 84,
        "power_cycles": 120,
        "media_errors": 0,
        "num_err_log_entries": 0,
    }
    out = _verdict(
        monkeypatch,
        tmp_path,
        summary,
        prior={"unsafe_shutdowns": 81},
        boot_id="new-boot",
    )
    assert out["verdict"] == "WARN"
    assert "cause unclassified" in out["reasons"][0]


def test_shrinking_power_cycles_is_treated_as_unusable(monkeypatch, tmp_path):
    """A counter that went backwards means serial reuse, not a clean drive."""
    summary = {
        **BRIDGED,
        "ok": True,
        "smart_status": "PASSED",
        "unsafe_shutdowns": 84,
        "power_cycles": 5,
        "media_errors": 0,
        "num_err_log_entries": 0,
    }
    out = _verdict(
        monkeypatch,
        tmp_path,
        summary,
        prior={"unsafe_shutdowns": 81, "power_cycles": 117, "boot_id": "old-boot"},
        boot_id="new-boot",
    )
    assert out["verdict"] == "WARN"
    assert "cause unclassified" in out["reasons"][0]


def test_bridged_detection_prefers_transport_over_device_type():
    assert disk_smart._is_bridged({"transport": "usb", "device_type": "nvme"})
    assert not disk_smart._is_bridged({"transport": "nvme", "device_type": "nvme"})
    # Direct-probe path: no transport recorded, fall back to the -d token.
    assert disk_smart._is_bridged({"device_type": "sntasmedia"})
    assert not disk_smart._is_bridged({"device_type": "nvme"})
