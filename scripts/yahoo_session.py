"""Shared session arithmetic for the Yahoo Finance chart snapshots.

Yahoo's interval=1d slice cannot be safely indexed from the end. Two habits of
the feed break a fixed offset like clean_closes[-2]:

  * The bar for the session regularMarketPrice belongs to is present, but its
    close stays null until Yahoo fills it -- for single-name equities that is
    the whole session and hours past it, and ^FTSE behaves the same way after
    the London close. Filtering nulls out before indexing then silently slides
    the baseline back a session, so the reported daily change is measured
    against two sessions ago.
  * For futures (GC=F, SI=F, HG=F) and DX-Y a live bar stamped at
    regularMarketTime is appended beside that session's own bar, so one
    session appears twice.

Both are handled by anchoring on regularMarketTime -- the timestamp
regularMarketPrice actually belongs to -- and comparing session dates rather
than counting back from the end of the array.

Dates must be compared in exchange-local time, never UTC: bar stamps differ by
venue (00:00 for futures and DX-Y, 08:00 for ^FTSE, 09:30 for US equities,
09:00 for ^N225), and an evening futures quote at 20:49 ET is already the next
day in UTC, which would put the current session's own settle in the past.
"""
import math
from datetime import datetime, timedelta, timezone

__all__ = ['session_date', 'prior_close', 'session_volumes']


def _local_date(epoch, offset):
    return (datetime.fromtimestamp(epoch, timezone.utc) + offset).date()


def session_date(meta):
    """Exchange-local date of the session regularMarketPrice belongs to.

    Returns (date, offset), or (None, offset) when the feed omits
    regularMarketTime -- callers treat that as "no baseline" and fall back or
    report the figure as unavailable rather than printing a wrong number.
    """
    offset = timedelta(seconds=meta.get('gmtoffset') or 0)
    market_time = meta.get('regularMarketTime')
    if market_time is None:
        return None, offset
    return _local_date(market_time, offset), offset


def prior_close(meta, timestamps, closes):
    """Close of the session before the one regularMarketPrice belongs to.

    This is the correct baseline for a daily change. Over a weekend it also
    does the right thing: regularMarketPrice is already Friday's close, so the
    baseline becomes Thursday.
    """
    session, offset = session_date(meta)
    if session is None or not timestamps:
        return None

    prev = None
    for i, ts in enumerate(timestamps):
        close = closes[i] if i < len(closes) else None
        if not isinstance(close, (int, float)) or not math.isfinite(close):
            continue
        if _local_date(ts, offset) >= session:
            continue
        prev = close
    return prev


def session_volumes(meta, timestamps, volumes):
    """(current session's volume, prior completed sessions' volumes).

    The same split, applied to the volume series. volumes[-1] is only "today"
    once Yahoo has filled that bar; before the first print it is the previous
    session's volume, which a naive index reports as today's while also
    dropping that session from the average -- both halves of a volume ratio
    wrong at once. Where one session yields two bars, the larger reading wins
    so an empty companion cannot blank it out.
    """
    session, offset = session_date(meta)
    if session is None or not timestamps:
        return 0, []

    today, prior = 0, []
    for i, ts in enumerate(timestamps):
        vol = volumes[i] if i < len(volumes) else None
        if not isinstance(vol, (int, float)) or not math.isfinite(vol) or vol <= 0:
            continue
        if _local_date(ts, offset) >= session:
            today = max(today, vol)
        else:
            prior.append(vol)
    return today, prior
