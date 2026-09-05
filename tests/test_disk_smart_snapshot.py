import json
import stat
import time

import disk_smart


def _write_snapshot(path, *, age=0, mode=0o644):
    payload = {
        "schema_version": 1,
        "generated_at_epoch": int(time.time() - age),
        "generated_at": "2026-09-05T00:00:00+00:00",
        "disks": [
            {
                "label": "Test SSD",
                "device_id": "/dev/disk/by-id/nvme-test",
                "device_type": "nvme",
                "model": "Test SSD",
                "serial": "TEST123",
                "transport": "nvme",
                "summary": {
                    "ok": True,
                    "smart_status": "PASSED",
                    "device_type": "nvme",
                    "kind": "nvme",
                    "critical_warning": 0,
                    "percentage_used": 4,
                    "available_spare": 100,
                    "available_spare_threshold": 10,
                    "media_errors": 0,
                    "num_err_log_entries": 0,
                    "unsafe_shutdowns": 0,
                    "power_on_hours": 100,
                    "temperature_c": 30,
                },
            }
        ],
    }
    path.write_text(json.dumps(payload))
    path.chmod(mode)


class _RootOwnedPath:
    def __init__(self, path):
        self.path = path

    def stat(self):
        real = self.path.stat()
        return type(
            "Stat", (), {"st_mode": stat.S_IFREG | stat.S_IMODE(real.st_mode), "st_uid": 0}
        )()

    def read_text(self, *args, **kwargs):
        return self.path.read_text(*args, **kwargs)

    def __str__(self):
        return str(self.path)

    @property
    def parent(self):
        return _RootOwnedPath(self.path.parent)


def test_fresh_root_owned_snapshot_is_consumed(tmp_path, monkeypatch):
    snapshot = tmp_path / "disk-smart.json"
    _write_snapshot(snapshot)
    monkeypatch.setattr(disk_smart, "SNAPSHOT_PATH", _RootOwnedPath(snapshot))
    monkeypatch.setattr(disk_smart, "SNAPSHOT_MAX_AGE_SECONDS", 7200)
    result, error = disk_smart._collect_from_snapshot()
    assert error is None
    assert result["source"] == "privileged SMART snapshot"
    assert result["disks"][0]["verdict"]["verdict"] == "OK"


def test_stale_snapshot_is_rejected(tmp_path, monkeypatch):
    snapshot = tmp_path / "disk-smart.json"
    _write_snapshot(snapshot, age=7201)
    monkeypatch.setattr(disk_smart, "SNAPSHOT_PATH", _RootOwnedPath(snapshot))
    monkeypatch.setattr(disk_smart, "SNAPSHOT_MAX_AGE_SECONDS", 7200)
    result, error = disk_smart._collect_from_snapshot()
    assert result is None
    assert "stale" in error


def test_writable_snapshot_is_rejected(tmp_path, monkeypatch):
    snapshot = tmp_path / "disk-smart.json"
    _write_snapshot(snapshot, mode=0o666)
    monkeypatch.setattr(disk_smart, "SNAPSHOT_PATH", _RootOwnedPath(snapshot))
    result, error = disk_smart._collect_from_snapshot()
    assert result is None
    assert "writable" in error
