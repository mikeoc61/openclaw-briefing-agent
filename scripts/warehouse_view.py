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
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from dataclasses import asdict, dataclass

try:
    from market_warehouse import day_pace_retarget, latest
    from market_warehouse.aggregate import MIN_BLOCKS_FOR_PROJ, RETARGET_INTERVAL
    _AVAILABLE = True
except Exception:
    _AVAILABLE = False

STALE_AFTER_DAYS = 2


@dataclass(frozen=True)
class OnchainDayView:
    """Presentation-ready lines for the latest complete UTC day."""

    date: datetime.date
    day_line: str
    stale_line: str | None
    day_pace: float | None

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


def _format_day_line(row: dict, date: datetime.date) -> str | None:
    parts = []
    if row.get("blocks_day") is not None:
        parts.append(f"{row['blocks_day']} blks")
    if row.get("block_fullness") is not None:
        parts.append(f"{row['block_fullness']:.0f}% full")
    if row.get("p50_fee") is not None:
        parts.append(f"p50 {row['p50_fee']:.1f} sat/vB")
    if row.get("fee_subsidy") is not None:
        parts.append(f"fee/subsidy {row['fee_subsidy']:.2f}%")
    if row.get("miner_rev") is not None:
        parts.append(f"miner rev {row['miner_rev']:,.1f} BTC")
    if not parts:
        return None
    return f"Day (UTC {date}): " + " | ".join(parts)


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

    return OnchainDayView(date=date, day_line=day_line, stale_line=stale_line, day_pace=pace)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="warehouse_view.py",
        description="Print the warehouse-derived lines for the latest complete UTC day.",
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
        if view.stale_line:
            print(view.stale_line)
        if fragment:
            print(f"retarget fragment: {fragment}")
        if view.day_pace is not None:
            print(f"day-pace retarget: {view.day_pace:+.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
