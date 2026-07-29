#!/usr/bin/env python3
"""Read the market_warehouse DuckDB and format the brief's on-chain day lines.

The warehouse is written by a dedicated ingester (data_stores/market_warehouse);
this module only ever reads it, read-only and fail-soft — every failure path
returns None so the brief degrades to its live-snapshot render.

Import from compose_briefing.py:

    import warehouse_view
    view = warehouse_view.onchain_day_view()

Or run standalone to inspect what the brief would show:

    ./warehouse_view.py
    ./warehouse_view.py --json
    ./warehouse_view.py --retarget-proj -53.69 --blocks-left 2010

Scope: this covers the WAREHOUSE half of the brief's BITCOIN section — the
`Day (UTC …)` and `Signal:` lines. The `Live:` line above them is assembled by
compose_briefing.py from bitcoin_snapshot.sh output and is not reproduced here;
this tool deliberately touches nothing but the database, so it works without a
node. Run `./bitcoin_snapshot.sh` to see the live half.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from dataclasses import asdict, dataclass

try:
    from market_warehouse import (
        apathy_streak,
        day_pace_retarget,
        drawdown_from_high,
        latest,
        percentile_rank,
    )
    from market_warehouse.aggregate import MIN_BLOCKS_FOR_PROJ, RETARGET_INTERVAL
    _AVAILABLE = True
except Exception:
    _AVAILABLE = False

STALE_AFTER_DAYS = 2

# Volume is reported only when notable. A percentile threshold fires (100-N)% of
# days by construction, so 95 means ~18 days/year rather than ~36 at 90. FTX
# (2022-11-09) reached 97.5 and clears it; every other backtested regime date sat
# between 5.9 and 72.3, including all three euphoria peaks.
VOL_PCTILE_MIN = 95.0


@dataclass(frozen=True)
class OnchainDayView:
    """Presentation-ready lines for the latest complete UTC day."""

    date: datetime.date
    day_line: str
    stale_line: str | None
    day_pace: float | None
    signal_line: str | None
    fee_pctile: float | None = None
    apathy_days: int | None = None
    hashrate_dd: float | None = None
    vol_pctile: float | None = None

    def context_lines(self) -> list[str]:
        """Warehouse-derived facts for the LLM analyst's context block.

        The analyst otherwise sees only this morning's snapshot, so it has no way
        to know whether today's numbers are ordinary or extreme. These lines give
        it the historical position instead of a hardcoded threshold.
        """
        out: list[str] = []
        if self.fee_pctile is not None:
            out.append(
                f"BTC fee/subsidy vs history: {self.fee_pctile:.0f}th percentile of 2y "
                f"(7d avg, weekend-corrected; low = blockspace demand apathy)"
            )
        if self.apathy_days:
            out.append(
                f"BTC apathy streak: {self.apathy_days} consecutive days under 1.0% "
                f"fee/subsidy (regime duration, not a new low)"
            )
        if self.hashrate_dd is not None and self.hashrate_dd < -1:
            out.append(
                f"BTC hashrate: {self.hashrate_dd:.1f}% off its 90d high "
                f"(miner stress; historic washouts ran -33% to -50%)"
            )
        if self.vol_pctile is not None and self.vol_pctile >= VOL_PCTILE_MIN:
            out.append(
                f"BTC exchange volume: {self.vol_pctile:.0f}th percentile of 2y "
                f"(Kraken, single venue, weekday-adjusted). Volume marks EVENTS, not "
                f"direction — pair it with the price move above. Backtested, it fires "
                f"only on acute single-day capitulations (2015-01-14: 100th; "
                f"2022-11-09 FTX: 97th), which no other measure here sees. It is quiet "
                f"at slow bear bottoms (2018: 71st), at supply shocks (2021 China ban: "
                f"6th) and at every euphoria peak — so it flags the panic itself, not "
                f"the low, which typically follows days later"
            )
        if out:
            out.append(f"On-chain day above is UTC {self.date:%Y-%m-%d %a}, a complete day")
        return out

    def retarget_fragment(
        self, cumulative: str | float | None, blocks_left: str | int | None
    ) -> str:
        """Live retarget text: the cumulative projection once the difficulty
        period is established, else the warehouse day-pace fallback.

        Below MIN_BLOCKS_FOR_PROJ blocks the cumulative figure is single-block
        noise (one fast block has produced +1718%). The threshold is imported
        from the warehouse so the two can never disagree.
        """
        elapsed = None
        if blocks_left not in (None, ""):
            try:
                elapsed = RETARGET_INTERVAL - int(blocks_left)
            except (TypeError, ValueError):
                elapsed = None
        if cumulative not in (None, "") and (
            elapsed is None or elapsed >= MIN_BLOCKS_FOR_PROJ
        ):
            return f"retarget proj {cumulative}%"
        if self.day_pace is not None:
            return f"retarget {self.day_pace:+.2f}% (day-pace)"
        return ""


def _ordinal(p: float) -> str:
    n = max(1, round(p))
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _signal_values(db_path) -> dict:
    """Trailing-window position of today's data. Each helper is independently
    fail-soft — one failing leaves its key None rather than losing the rest."""
    # 7d smoothing: fee_subsidy runs ~27% lower at weekends, so the raw daily
    # percentile substantially reports the day of the week (73% of its bottom
    # decile is Sat/Sun vs a 29% baseline). A 7-day mean cancels that exactly.
    try:
        fee_pct = percentile_rank(
            "fee_subsidy", window_days=730, smooth_days=7, db_path=db_path
        )
    except Exception:
        fee_pct = None

    # Absolute threshold, deliberately: a percentile threshold recalibrates to the
    # regime it measures, so it reports "new lows vs recent history", never the
    # duration of a sustained regime. Duration is what belongs in the brief.
    try:
        streak = apathy_streak(db_path=db_path)
    except Exception:
        streak = None

    try:
        hr_dd = drawdown_from_high("hash_rate_ehs", window_days=90, db_path=db_path)
    except Exception:
        hr_dd = None

    # detrend_dow, not smooth_days: exchange volume carries the same weekly cycle
    # as fee_subsidy, but a volume event lasts 3-4 days and a 7-day mean dilutes
    # it away (FTX measured 97.5 detrended vs 9.2 smoothed 12 days later).
    try:
        vol_pct = percentile_rank(
            "kraken_vol", window_days=730, table="btc",
            detrend_dow=True, db_path=db_path,
        )
    except Exception:
        vol_pct = None

    return {
        "fee_pctile": fee_pct,
        "apathy_days": streak,
        "hashrate_dd": hr_dd,
        "vol_pctile": vol_pct,
    }


def _build_signal_line(values: dict) -> str | None:
    bits = []
    if values["fee_pctile"] is not None:
        bits.append(f"fee/subsidy {_ordinal(values['fee_pctile'])} pctile 2y (7d)")
    if values["apathy_days"]:
        bits.append(f"apathy {values['apathy_days']}d")
    hr_dd = values["hashrate_dd"]
    if hr_dd is not None and hr_dd < -1:
        bits.append(f"hashrate {hr_dd:.1f}% off 90d high")
    vol = values.get("vol_pctile")
    if vol is not None and vol >= VOL_PCTILE_MIN:
        bits.append(f"volume {_ordinal(vol)} pctile 2y")
    return "Signal: " + " | ".join(bits) if bits else None


def _format_day_line(row: dict, date: datetime.date) -> str | None:
    parts = []
    if row.get("blocks_day") is not None:
        parts.append(f"{row['blocks_day']} blks")
    if row.get("block_fullness") is not None:
        parts.append(f"{row['block_fullness']:.0f}% full")
    if row.get("p50_fee") is not None:
        # getblockstats reports feerate_percentiles as integer sat/vB, so 0 is a
        # floor meaning "under 1", not an absence of fees. Rendering it as 0.0
        # reads like missing data. It bottoms out on Sundays, the weekly demand
        # trough (DECISIONS #17).
        p50 = row["p50_fee"]
        parts.append("p50 <1 sat/vB" if p50 < 1 else f"p50 {p50:.1f} sat/vB")
    if row.get("fee_subsidy") is not None:
        parts.append(f"fee/subsidy {row['fee_subsidy']:.2f}%")
    if row.get("miner_rev") is not None:
        parts.append(f"miner rev {row['miner_rev']:,.1f} BTC")
    if not parts:
        return None
    # Name the weekday: fee_subsidy runs ~27% lower at weekends, and 2 of 7
    # briefs (Sun/Mon HST) report a weekend UTC day, so an unlabelled dip reads
    # as deterioration. The Signal line is seasonality-corrected; this line is
    # deliberately one day's raw facts, so the reader needs the cue.
    return f"Day (UTC {date} {date:%a}): " + " | ".join(parts)


def onchain_day_view(db_path=None, today: datetime.date | None = None):
    """Latest complete-day view, or None if the warehouse is unusable."""
    if not _AVAILABLE:
        return None
    try:
        row = latest("onchain", db_path=db_path)
    except Exception:
        return None
    if not row or row.get("date") is None:
        return None
    date = row["date"]
    day_line = _format_day_line(row, date)
    if day_line is None:
        return None

    stale_line = None
    try:
        today = today or datetime.datetime.now(datetime.timezone.utc).date()
        behind = (today - date).days
        if behind > STALE_AFTER_DAYS:
            stale_line = f"⚠ warehouse {behind}d behind (latest complete day {date})"
    except Exception:
        pass

    try:
        pace = day_pace_retarget(db_path=db_path)
    except Exception:
        pace = None

    values = _signal_values(db_path)

    return OnchainDayView(
        date=date,
        day_line=day_line,
        stale_line=stale_line,
        day_pace=pace,
        signal_line=_build_signal_line(values),
        fee_pctile=values["fee_pctile"],
        apathy_days=values["apathy_days"],
        hashrate_dd=values["hashrate_dd"],
        vol_pctile=values["vol_pctile"],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="warehouse_view.py",
        description=(
            "Print the warehouse-derived lines (Day / Signal) for the latest "
            "complete UTC day. The brief's 'Live:' line comes from "
            "bitcoin_snapshot.sh, not from here — this reads only the database."
        ),
    )
    parser.add_argument("--db", default=None, help="warehouse path (default: MARKET_WAREHOUSE_DB or ~/data/market.duckdb)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--retarget-proj", default=None, help="live cumulative projection, to preview the retarget fragment")
    parser.add_argument("--blocks-left", default=None, help="live blocks remaining in the period")
    args = parser.parse_args(argv)

    view = onchain_day_view(db_path=args.db)
    if view is None:
        if args.json:
            print(json.dumps({"available": False}))
        else:
            reason = "market_warehouse not importable" if not _AVAILABLE else "no DB, no table, or no usable row"
            print(f"warehouse unavailable ({reason})", file=sys.stderr)
        return 1

    fragment = view.retarget_fragment(args.retarget_proj, args.blocks_left)
    if args.json:
        payload = asdict(view)
        payload["date"] = view.date.isoformat()
        payload["available"] = True
        payload["retarget_fragment"] = fragment
        print(json.dumps(payload, indent=2))
    else:
        print(view.day_line)
        if view.signal_line:
            print(view.signal_line)
        if view.stale_line:
            print(view.stale_line)
        if fragment:
            print(f"retarget fragment: {fragment}")
        # Only worth a second line when it differs: with no --retarget-proj the
        # fragment already falls back to the day-pace figure.
        if view.day_pace is not None and "(day-pace)" not in fragment:
            print(f"day-pace retarget: {view.day_pace:+.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
