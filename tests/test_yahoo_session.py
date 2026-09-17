"""Session arithmetic behind the Yahoo-backed snapshots (scripts/yahoo_session.py).

The daily change is (regularMarketPrice - prior session close). Yahoo's
interval=1d slice includes a bar for the session regularMarketPrice belongs
to, but for single-name equities and ^FTSE that bar's close stays null for the
whole session and for hours after it -- so a fixed clean_closes[-2] index
silently measured against TWO sessions back at every hour the briefing could
run. These tests pin the session-date anchoring that replaced it.
"""
import math, pathlib, sys
from datetime import datetime, timedelta, timezone

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
from yahoo_session import prior_close, session_volumes, session_date  # noqa: E402

# Scripts that must keep getting this logic from the shared module.
SNAPSHOTS = ["portfolio_snapshot.sh", "strc_snapshot.sh", "market_snapshot.sh"]

EDT, EST, HST = -4 * 3600, -5 * 3600, -10 * 3600
BST, JST = 1 * 3600, 9 * 3600  # London in September, Tokyo year-round


def stamp(y, m, d, hh, mm, offset):
    """Epoch seconds for a wall-clock moment at the given UTC offset."""
    return int(datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp()) - offset


def hst_6am(y, m, d, et_offset):
    """The 6:00 HST run time, expressed as epoch seconds.

    HST has no DST, so 6:00 HST is 16:00 UTC year-round: noon ET in summer,
    11:00 ET in winter. Either way the US session is open and unfinished.
    """
    return stamp(y, m, d, 6, 0, HST)


def bars(offset, *sessions):
    """(timestamps, closes) for 09:30-stamped daily bars."""
    ts = [stamp(y, m, d, 9, 30, offset) for (y, m, d), _ in sessions]
    return ts, [c for _, c in sessions]


# Real STRC daily closes. For a Wed Sep 16 session the correct baseline is
# Tue Sep 15 (97.14); the old clean[-2] reached Mon Sep 14 (98.91).
WEEK = [((2026, 9, 10), 97.97), ((2026, 9, 11), 98.64),
        ((2026, 9, 14), 98.91), ((2026, 9, 15), 97.14)]
PRIOR, TWO_BACK = 97.14, 98.91
SEP16 = ((2026, 9, 16), None)


def meta(market_time, offset=EDT):
    return {"regularMarketTime": market_time, "gmtoffset": offset}


# ── the run time that matters: 6:00 HST, mid-session ──────────────────────
def test_scheduled_run_time_summer():
    """6:00 HST = noon EDT, four hours before the close, today's bar null."""
    ts, closes = bars(EDT, *WEEK, SEP16)
    assert prior_close(meta(hst_6am(2026, 9, 16, EDT)), ts, closes) == PRIOR


def test_scheduled_run_time_winter():
    """6:00 HST = 11:00 EST once the mainland leaves DST; HST never shifts."""
    week = [((2027, 1, 12), 90.10), ((2027, 1, 13), 91.20)]
    ts, closes = bars(EST, *week, ((2027, 1, 14), None))
    assert prior_close(meta(hst_6am(2027, 1, 14, EST), EST), ts, closes) == 91.20


# ── the two states asked about explicitly ─────────────────────────────────
@pytest.mark.parametrize("hh,mm", [
    (9, 31),    # just after the open
    (12, 0),    # midday -- the scheduled hour
    (15, 59),   # a minute before the close
])
def test_market_open_today_bar_still_null(hh, mm):
    """Mid-session, regularMarketTime tracks the last trade; today's bar is
    null the whole time, which is where clean[-2] went wrong."""
    ts, closes = bars(EDT, *WEEK, SEP16)
    assert prior_close(meta(stamp(2026, 9, 16, hh, mm, EDT)), ts, closes) == PRIOR


def test_after_close_today_close_not_yet_updated():
    """The state observed live: 16:00 close, bar for that session still null.

    Once the session ends, regularMarketTime pins to 16:00 and stops moving,
    and prior_close() reads no wall clock -- so a run at 16:05, at 20:51, or
    at 00:30 the next morning is the same input, and one assertion covers all
    of them. test_run_hour_never_reaches_two_sessions_back sweeps the hour
    that *is* an input.
    """
    ts, closes = bars(EDT, *WEEK, SEP16)
    assert prior_close(meta(stamp(2026, 9, 16, 16, 0, EDT)), ts, closes) == PRIOR


def test_after_close_once_the_bar_fills():
    """When Yahoo does backfill the close, the baseline must not move."""
    ts, closes = bars(EDT, *WEEK, ((2026, 9, 16), 97.07))
    assert prior_close(meta(stamp(2026, 9, 16, 16, 0, EDT)), ts, closes) == PRIOR


# ── general-case gaps ─────────────────────────────────────────────────────
def test_today_bar_absent_entirely():
    ts, closes = bars(EDT, *WEEK)
    assert prior_close(meta(hst_6am(2026, 9, 16, EDT)), ts, closes) == PRIOR


def test_monday_reaches_back_over_the_weekend():
    """Monday's baseline is Friday's close, not Thursday's."""
    ts, closes = bars(EDT, ((2026, 9, 10), 97.97), ((2026, 9, 11), 98.64),
                      ((2026, 9, 14), None))
    assert prior_close(meta(hst_6am(2026, 9, 14, EDT)), ts, closes) == 98.64


def test_session_after_a_holiday_gap():
    """Tue Sep 8 follows Labor Day; the baseline is Fri Sep 4."""
    ts, closes = bars(EDT, ((2026, 9, 2), 174.96), ((2026, 9, 3), 192.70),
                      ((2026, 9, 4), 184.64), ((2026, 9, 8), None))
    assert prior_close(meta(hst_6am(2026, 9, 8, EDT)), ts, closes) == 184.64


def test_weekend_price_is_fridays_close():
    """On a weekend regularMarketPrice is already Friday's close, so the
    baseline is Thursday -- the long-weekend artifact the old comment cited."""
    ts, closes = bars(EDT, *WEEK)
    friday = stamp(2026, 9, 15, 16, 0, EDT)
    assert prior_close(meta(friday), ts, closes) == 98.91


def test_duplicate_bars_for_one_session():
    """Yahoo sometimes emits two bars for the current date (seen on GC=F)."""
    ts, closes = bars(EDT, *WEEK, SEP16, ((2026, 9, 16), 97.07))
    assert prior_close(meta(stamp(2026, 9, 16, 12, 0, EDT)), ts, closes) == PRIOR


def test_interior_null_close_is_skipped():
    ts, closes = bars(EDT, ((2026, 9, 10), 97.97), ((2026, 9, 11), None),
                      ((2026, 9, 14), 98.91), ((2026, 9, 15), 97.14), SEP16)
    assert prior_close(meta(hst_6am(2026, 9, 16, EDT)), ts, closes) == PRIOR


def test_run_hour_never_reaches_two_sessions_back():
    """Regression guard: sweep every regularMarketTime hour on the session.

    Covers the whole day in one pass -- pre-open, open, and post-close -- with
    today's bar null, which is the shape that produced the wrong percentages.
    """
    ts, closes = bars(EDT, *WEEK, SEP16)
    for hh in range(24):
        got = prior_close(meta(stamp(2026, 9, 16, hh, 0, EDT)), ts, closes)
        assert got == PRIOR, f"{hh:02d}:00 ET gave {got}, expected {PRIOR}"
        assert got != TWO_BACK


# ── degraded input: fail closed rather than print a wrong number ──────────
def test_missing_market_time_returns_none():
    ts, closes = bars(EDT, *WEEK, SEP16)
    assert prior_close({"gmtoffset": EDT}, ts, closes) is None


def test_no_timestamps_returns_none():
    assert prior_close(meta(hst_6am(2026, 9, 16, EDT)), [], []) is None


def test_no_prior_session_in_window_returns_none():
    ts, closes = bars(EDT, SEP16)
    assert prior_close(meta(hst_6am(2026, 9, 16, EDT)), ts, closes) is None


def test_missing_gmtoffset_does_not_crash():
    ts, closes = bars(EDT, *WEEK, SEP16)
    assert prior_close({"regularMarketTime": hst_6am(2026, 9, 16, EDT)}, ts, closes) == PRIOR


@pytest.mark.parametrize("script", SNAPSHOTS)
def test_script_imports_the_shared_module(script):
    """No script may reintroduce a local copy of this logic."""
    src = (SCRIPTS / script).read_text(encoding="utf-8")
    assert "from yahoo_session import" in src, f"{script} does not import yahoo_session"
    assert "def prior_close" not in src, f"{script} has its own prior_close again"
    assert "def session_volumes" not in src, f"{script} has its own session_volumes again"


@pytest.mark.parametrize("script", SNAPSHOTS)
def test_script_puts_the_module_on_the_path(script):
    """The heredocs run `python3 -` with no __file__, so each script must add
    its own directory to PYTHONPATH or the import fails at runtime."""
    src = (SCRIPTS / script).read_text(encoding="utf-8")
    assert 'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"' in src
    assert 'PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" python3' in src


def test_session_date_reads_exchange_local_time():
    """20:49 ET is the next day in UTC; the session must stay Sep 16."""
    market_time = stamp(2026, 9, 16, 20, 49, EDT)
    assert session_date(meta(market_time))[0].isoformat() == "2026-09-16"
    assert session_date({"regularMarketTime": market_time})[0].isoformat() == "2026-09-17"


def test_session_date_without_market_time():
    assert session_date({"gmtoffset": EDT})[0] is None


# ── other venues in market_snapshot.sh: bar stamps and offsets differ ──────
def test_ftse_after_the_london_close():
    """^FTSE bars are stamped 08:00 London and its close sits null after the
    16:35 finish -- the state that had FTSE reading -0.09% instead of +0.28%.
    At the 6:00 HST run (17:00 London) the session is already over."""
    week = [((2026, 9, 11), 10650.40), ((2026, 9, 14), 10697.60),
            ((2026, 9, 15), 10658.10)]
    ts = [stamp(y, m, d, 8, 0, BST) for (y, m, d), _ in week] + [stamp(2026, 9, 16, 8, 0, BST)]
    closes = [c for _, c in week] + [None]
    market_time = stamp(2026, 9, 16, 16, 35, BST)
    assert prior_close(meta(market_time, BST), ts, closes) == 10658.10


def test_nikkei_tokyo_session():
    """^N225 runs a day ahead of ET; the anchor is Tokyo-local either way."""
    week = [((2026, 9, 15), 63200.0), ((2026, 9, 16), 63484.10)]
    ts = [stamp(y, m, d, 9, 0, JST) for (y, m, d), _ in week] + [stamp(2026, 9, 17, 9, 0, JST)]
    closes = [c for _, c in week] + [64003.66]
    market_time = stamp(2026, 9, 17, 9, 43, JST)
    assert prior_close(meta(market_time, JST), ts, closes) == 63484.10


def test_futures_live_bar_stamped_at_market_time():
    """GC=F/SI=F/HG=F and DX-Y stamp session bars 00:00 and append a live bar
    at regularMarketTime, so the current session appears twice -- once with a
    null close, once live. Neither may become the baseline."""
    ts = [stamp(2026, 9, 11, 0, 0, EDT), stamp(2026, 9, 14, 0, 0, EDT),
          stamp(2026, 9, 15, 0, 0, EDT), stamp(2026, 9, 16, 0, 0, EDT),
          stamp(2026, 9, 16, 20, 49, EDT)]
    closes = [4408.90, 4351.90, 4332.80, None, 4323.10]
    market_time = stamp(2026, 9, 16, 20, 49, EDT)
    assert prior_close(meta(market_time), ts, closes) == 4332.80


def test_evening_futures_session_uses_exchange_local_dates():
    """The case that makes gmtoffset load-bearing.

    Futures trade into the evening, so regularMarketTime can be 20:49 ET --
    which is already the NEXT day in UTC. Once the day's bar has settled and
    carries a close, judging the session in UTC would put that bar in the past
    and quote the change against today's own settle (~0%). Exchange-local
    dates keep the baseline on the prior session.
    """
    ts = [stamp(2026, 9, 14, 0, 0, EDT), stamp(2026, 9, 15, 0, 0, EDT),
          stamp(2026, 9, 16, 0, 0, EDT), stamp(2026, 9, 16, 20, 49, EDT)]
    closes = [4351.90, 4332.80, 4326.70, 4323.10]   # Sep 16 has settled
    market_time = stamp(2026, 9, 16, 20, 49, EDT)   # = Sep 17 00:49 UTC
    assert prior_close(meta(market_time), ts, closes) == 4332.80


def test_dxy_null_day_inside_the_window():
    """DX-Y publishes a Sunday bar with a null close mid-window."""
    ts = [stamp(2026, 9, 11, 0, 0, EDT), stamp(2026, 9, 13, 0, 0, EDT),
          stamp(2026, 9, 14, 0, 0, EDT), stamp(2026, 9, 15, 0, 0, EDT),
          stamp(2026, 9, 16, 0, 0, EDT), stamp(2026, 9, 16, 20, 49, EDT)]
    closes = [99.12, None, 99.46, 99.65, None, 100.30]
    assert prior_close(meta(stamp(2026, 9, 16, 20, 49, EDT)), ts, closes) == 99.65



# ── STRC volume: same session-date split, applied to the volume series ─────
# Real STRC volumes, Sep 10-15 2026, with Sep 16 as the current session.
VOL_WEEK = [((2026, 9, 10), 1_233_000), ((2026, 9, 11), 1_388_800),
            ((2026, 9, 14), 2_042_000), ((2026, 9, 15), 2_071_000)]
PRIOR_VOLS = [v for _, v in VOL_WEEK]


def vol_bars(*sessions):
    ts = [stamp(y, m, d, 9, 30, EDT) for (y, m, d), _ in sessions]
    return ts, [v for _, v in sessions]


def test_volume_splits_current_session_from_the_average():
    """The observed state: close null but volume filled, so today counts."""
    ts, vols = vol_bars(*VOL_WEEK, ((2026, 9, 16), 1_194_368))
    today, prior = session_volumes(meta(stamp(2026, 9, 16, 16, 0, EDT)), ts, vols)
    assert today == 1_194_368
    assert prior == PRIOR_VOLS


def test_volume_average_excludes_the_current_session():
    """The current session must never land in its own baseline."""
    ts, vols = vol_bars(*VOL_WEEK, ((2026, 9, 16), 1_194_368))
    _, prior = session_volumes(meta(stamp(2026, 9, 16, 12, 0, EDT)), ts, vols)
    assert 1_194_368 not in prior
    assert sum(prior) / len(prior) == sum(PRIOR_VOLS) / len(PRIOR_VOLS)


def test_volume_before_the_first_print_is_not_yesterdays():
    """The old bug: with today's bar empty, volumes[-1] was yesterday's volume
    reported as today's, and yesterday then dropped out of the average."""
    ts, vols = vol_bars(*VOL_WEEK, ((2026, 9, 16), None))
    today, prior = session_volumes(meta(hst_6am(2026, 9, 16, EDT)), ts, vols)
    assert today == 0, "must not borrow the prior session's volume"
    assert prior == PRIOR_VOLS, "the prior session stays in the average"


def test_volume_zero_bar_is_not_counted():
    ts, vols = vol_bars(*VOL_WEEK, ((2026, 9, 16), 0))
    today, prior = session_volumes(meta(hst_6am(2026, 9, 16, EDT)), ts, vols)
    assert today == 0
    assert prior == PRIOR_VOLS


def test_volume_today_bar_absent():
    ts, vols = vol_bars(*VOL_WEEK)
    today, prior = session_volumes(meta(hst_6am(2026, 9, 16, EDT)), ts, vols)
    assert today == 0
    assert prior == PRIOR_VOLS


@pytest.mark.parametrize("dup", [
    (1_194_368, 0),      # real reading first, empty companion second
    (0, 1_194_368),      # empty first -- the order Yahoo actually emits
    (1_194_368, None),
])
def test_volume_empty_companion_bar_does_not_blank_the_session(dup):
    """One session, two bars: an empty companion must not erase the reading,
    and neither bar may leak into the prior-session average."""
    ts, vols = vol_bars(*VOL_WEEK, ((2026, 9, 16), dup[0]), ((2026, 9, 16), dup[1]))
    today, prior = session_volumes(meta(stamp(2026, 9, 16, 16, 0, EDT)), ts, vols)
    assert today == 1_194_368
    assert prior == PRIOR_VOLS


def test_volume_shorter_volume_series_does_not_crash():
    """Yahoo can return fewer volume entries than timestamps."""
    ts, vols = vol_bars(*VOL_WEEK, ((2026, 9, 16), 1_194_368))
    today, prior = session_volumes(meta(stamp(2026, 9, 16, 16, 0, EDT)), ts, vols[:2])
    assert today == 0
    assert prior == PRIOR_VOLS[:2]


def test_volume_missing_market_time_yields_nothing():
    ts, vols = vol_bars(*VOL_WEEK, ((2026, 9, 16), 1_194_368))
    assert session_volumes({"gmtoffset": EDT}, ts, vols) == (0, [])


def test_volume_monday_average_spans_the_weekend():
    ts, vols = vol_bars(((2026, 9, 10), 1_233_000), ((2026, 9, 11), 1_388_800),
                        ((2026, 9, 14), None))
    today, prior = session_volumes(meta(hst_6am(2026, 9, 14, EDT)), ts, vols)
    assert today == 0
    assert prior == [1_233_000, 1_388_800]
