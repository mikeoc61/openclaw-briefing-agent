import smart_snapshot_collector as collector


def test_nvme_summary_is_sanitized():
    raw = {
        "smart_status": {"passed": True},
        "serial_number": "must-not-be-copied-by-summary",
        "nvme_smart_health_information_log": {
            "critical_warning": 0,
            "percentage_used": 4,
            "available_spare": 100,
            "available_spare_threshold": 10,
            "media_errors": 0,
            "num_err_log_entries": 2,
            "unsafe_shutdowns": 1,
            "power_cycles": 20,
            "power_on_hours": 1086,
            "data_units_written": 123,
            "data_units_read": 456,
            "temperature": 29,
            "vendor_private_blob": "excluded",
        },
    }
    result = collector._summary(raw, "/dev/disk/by-id/test")
    assert result["ok"] is True
    assert result["smart_status"] == "PASSED"
    assert result["percentage_used"] == 4
    assert result["temperature_c"] == 29
    assert "serial_number" not in result
    assert "vendor_private_blob" not in result


def test_device_spec_requires_stable_by_id_path():
    try:
        collector._parse_device_spec("disk|/dev/sda|sntasmedia")
    except Exception as exc:
        assert "/dev/disk/by-id" in str(exc)
    else:
        raise AssertionError("unstable device path was accepted")

