from __future__ import annotations

import datetime
import json
import pathlib

import duckdb
import pytest

from market_warehouse import write_snapshot, write_snapshots

import warehouse_view
from warehouse_view import main, onchain_day_view

DAY = datetime.date(2026, 7, 24)

# The real row the Pi produced for 2026-07-24.
PI_ROW = {
    "hash_rate_ehs": 915.6224206722721,
    "difficulty_t": 127.1705004290352,
    "blocks_day": 154,
    "block_fullness": 98.30543181818182,
    "p50_fee": 1.0,
    "miner_rev": 484.42393013,
    "fee_subsidy": 0.6595179490909091,
    "tx_rate": 8.521840277777779,
    "retarget_proj": -0.7926730540439686,
}


@pytest.fixture
def db(tmp_path) -> pathlib.Path:
    return tmp_path / "market.duckdb"


@pytest.fixture
def seeded(db) -> pathlib.Path:
    write_snapshot(DAY.isoformat(), {"onchain": PI_ROW}, db_path=db)
    return db


def test_day_line_matches_pi_render(seeded):
    view = onchain_day_view(db_path=seeded, today=datetime.date(2026, 7, 25))
    assert view.day_line == (
        "Day (UTC 2026-07-24): 154 blks | 98% full | p50 1.0 sat/vB "
        "| fee/subsidy 0.66% | miner rev 484.4 BTC"
    )


def test_not_stale_at_one_day_behind(seeded):
    assert onchain_day_view(db_path=seeded, today=datetime.date(2026, 7, 25)).stale_line is None


def test_not_stale_at_exactly_two_days_behind(seeded):
    assert onchain_day_view(db_path=seeded, today=datetime.date(2026, 7, 26)).stale_line is None


def test_stale_beyond_threshold(seeded):
    view = onchain_day_view(db_path=seeded, today=datetime.date(2026, 7, 29))
    assert view.stale_line == "⚠ warehouse 5d behind (latest complete day 2026-07-24)"


def test_none_when_db_missing(db):
    assert onchain_day_view(db_path=db, today=DAY) is None


def test_none_when_table_empty(seeded):
    con = duckdb.connect(str(seeded))
    con.execute("DELETE FROM onchain")
    con.close()
    assert onchain_day_view(db_path=seeded, today=DAY) is None


def test_none_when_market_warehouse_unavailable(seeded, monkeypatch):
    monkeypatch.setattr(warehouse_view, "_AVAILABLE", False)
    assert onchain_day_view(db_path=seeded, today=DAY) is None


def test_partial_row_renders_available_metrics(db):
    write_snapshot("2026-07-24", {"onchain": {"blocks_day": 144}}, db_path=db)
    view = onchain_day_view(db_path=db, today=datetime.date(2026, 7, 25))
    assert view.day_line == "Day (UTC 2026-07-24): 144 blks"


def test_all_null_metrics_yields_no_view(db):
    write_snapshot("2026-07-24", {"onchain": {"hash_rate_ehs": 900.0}}, db_path=db)
    assert onchain_day_view(db_path=db, today=datetime.date(2026, 7, 25)) is None


def test_day_pace_is_read_from_blocks_day(seeded):
    view = onchain_day_view(db_path=seeded, today=datetime.date(2026, 7, 25))
    assert view.day_pace == pytest.approx((154 / 144.0 - 1) * 100)


def test_retarget_fragment_uses_cumulative_when_period_established(seeded):
    view = onchain_day_view(db_path=seeded, today=datetime.date(2026, 7, 25))
    assert view.retarget_fragment("-0.79", "1800") == "retarget proj -0.79%"


def test_retarget_fragment_falls_back_below_min_blocks(seeded):
    view = onchain_day_view(db_path=seeded, today=datetime.date(2026, 7, 25))
    # 2010 left -> elapsed 6 -> single-block noise -> day-pace instead
    assert view.retarget_fragment("-53.69", "2010") == "retarget +6.94% (day-pace)"


def test_retarget_fragment_keeps_cumulative_for_large_early_sample(seeded):
    view = onchain_day_view(db_path=seeded, today=datetime.date(2026, 7, 25))
    # 1246 left -> elapsed 770 (China-ban shape) -> real signal, not noise
    assert view.retarget_fragment("-28.0", "1246") == "retarget proj -28.0%"


def test_retarget_fragment_defaults_to_cumulative_when_blocks_left_unparsable(seeded):
    view = onchain_day_view(db_path=seeded, today=datetime.date(2026, 7, 25))
    assert view.retarget_fragment("-0.79", "") == "retarget proj -0.79%"


def test_retarget_fragment_empty_when_nothing_available(db):
    write_snapshot("2026-07-24", {"onchain": {"p50_fee": 1.0}}, db_path=db)
    view = onchain_day_view(db_path=db, today=datetime.date(2026, 7, 25))
    assert view.day_pace is None
    assert view.retarget_fragment(None, "2010") == ""


def test_threshold_is_shared_with_the_warehouse():
    from market_warehouse.aggregate import MIN_BLOCKS_FOR_PROJ, RETARGET_INTERVAL

    assert warehouse_view.MIN_BLOCKS_FOR_PROJ is MIN_BLOCKS_FOR_PROJ
    assert warehouse_view.RETARGET_INTERVAL is RETARGET_INTERVAL


def test_cli_prints_day_line(seeded, capsys):
    assert main(["--db", str(seeded)]) == 0
    out = capsys.readouterr().out
    assert "Day (UTC 2026-07-24): 154 blks" in out
    assert "day-pace retarget: +6.94%" in out


def test_cli_json_is_machine_readable(seeded, capsys):
    assert main(["--db", str(seeded), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["available"] is True
    assert payload["date"] == "2026-07-24"
    assert payload["day_line"].startswith("Day (UTC 2026-07-24)")


def test_cli_previews_retarget_fragment(seeded, capsys):
    main(["--db", str(seeded), "--retarget-proj", "-53.69", "--blocks-left", "2010"])
    assert "retarget fragment: retarget +6.94% (day-pace)" in capsys.readouterr().out


def test_cli_returns_nonzero_when_unavailable(db, capsys):
    assert main(["--db", str(db)]) == 1
    assert "unavailable" in capsys.readouterr().err


def test_cli_json_when_unavailable(db, capsys):
    assert main(["--db", str(db), "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {"available": False}


def _seed_series(db: pathlib.Path, values: list[dict], start: str = "2026-04-01") -> None:
    base = datetime.date.fromisoformat(start)
    rows = [
        ((base + datetime.timedelta(days=i)).isoformat(), {"onchain": v})
        for i, v in enumerate(values)
    ]
    write_snapshots(rows, db_path=db)


def _washout(db: pathlib.Path) -> None:
    """90 normal days, then a 5-day fee washout with hashrate rolling over."""
    vals = [
        {"fee_subsidy": 5.0, "hash_rate_ehs": 1000.0, "blocks_day": 144, "p50_fee": 4.0,
         "miner_rev": 460.0, "block_fullness": 90.0}
        for _ in range(90)
    ]
    vals += [
        {"fee_subsidy": 0.1, "hash_rate_ehs": 900.0, "blocks_day": 138, "p50_fee": 1.0,
         "miner_rev": 433.5, "block_fullness": 97.0}
        for _ in range(5)
    ]
    _seed_series(db, vals)


def test_signal_line_present_on_washout(db):
    _washout(db)
    view = onchain_day_view(db_path=db, today=datetime.date(2026, 7, 6))
    assert view.signal_line is not None
    assert view.signal_line.startswith("Signal: ")
    assert "pctile 2y" in view.signal_line
    assert "apathy 5d" in view.signal_line
    assert "hashrate -10.0% off 90d high" in view.signal_line


def test_signal_fragments_needing_history_are_suppressed_when_short(db):
    # percentile_rank and drawdown_from_high require MIN_WINDOW_ROWS and drop
    # their fragments. apathy_streak has no history requirement, so it honestly
    # reports the 3 sub-1% days that exist.
    _seed_series(db, [{"fee_subsidy": 0.5, "blocks_day": 144, "hash_rate_ehs": 900.0}] * 3)
    view = onchain_day_view(db_path=db, today=datetime.date(2026, 4, 5))
    assert "pctile" not in view.signal_line
    assert "off 90d high" not in view.signal_line
    assert view.signal_line == "Signal: apathy 3d"
    assert view.day_line.startswith("Day (UTC ")


def test_signal_line_none_when_nothing_is_computable(db):
    _seed_series(db, [{"blocks_day": 144}] * 3)
    view = onchain_day_view(db_path=db, today=datetime.date(2026, 4, 5))
    assert view.signal_line is None
    assert view.day_line.startswith("Day (UTC ")


def test_signal_line_omits_quiet_hashrate_fragment(db):
    # Hashrate flat at its 90d high -> drawdown ~0 -> fragment suppressed.
    _seed_series(db, [{"fee_subsidy": 5.0, "hash_rate_ehs": 1000.0, "blocks_day": 144}] * 60)
    view = onchain_day_view(db_path=db, today=datetime.date(2026, 6, 1))
    assert view.signal_line is None or "off 90d high" not in view.signal_line


def test_signal_fragment_failure_does_not_break_view(db, monkeypatch):
    _washout(db)

    def boom(*a, **k):
        raise RuntimeError("query exploded")

    monkeypatch.setattr(warehouse_view, "percentile_rank", boom)
    view = onchain_day_view(db_path=db, today=datetime.date(2026, 7, 6))
    assert view is not None
    assert view.day_line.startswith("Day (UTC ")
    assert "pctile" not in (view.signal_line or "")
    assert "apathy 5d" in view.signal_line


def test_cli_prints_signal_line(db, capsys):
    _washout(db)
    assert main(["--db", str(db)]) == 0
    assert "Signal: " in capsys.readouterr().out


def test_cli_json_includes_signal_line(db, capsys):
    _washout(db)
    main(["--db", str(db), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert "signal_line" in payload and payload["signal_line"].startswith("Signal: ")
