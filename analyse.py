#!/usr/bin/env python3
"""
analyse.py  |  Forensic analytics for HorseRacingEngine

Read-only. Reads results/performance_metrics.csv (written by main.py or logged
by hand) and reports, for the SETTLED bets only:
  * number of settled bets (and how many are still PENDING)
  * strike rate (outright wins / settled)
  * ROI on actual money staked (P/L divided by total stake)
  * average Closing Line Value (CLV) delta, over rows that have a taken price
    AND a final SP
and breaks it down by odds band so you can see where any edge lives.

P/L is read straight from the 'pl' column when present, which handles each-way
returns cleanly (no need to reconstruct place terms here). If 'pl' is blank it
falls back to a win-only estimate from stake, result and taken odds.

Odds bands (on the taken price, else the SP):
  Shorts     < 4.0
  Mid-price  4.0 to 15.0 inclusive
  Longshots  > 15.0
NOTE: the brief specified Mid as 5.0-15.0, which leaves a 4.0-5.0 gap that would
silently drop bets. The mid band's lower bound is 4.0 here so every bet lands in
exactly one band. Change BANDS below if you want otherwise.

Honest reading: with a small sample these numbers are noise. Strike rate and
ROI need a few hundred settled bets before they mean much; CLV stabilises
sooner and is the better early read on whether you are beating the market.

Usage:  python analyse.py            (reads results/performance_metrics.csv)
        python analyse.py <path>     (reads a specific CSV)
"""

from __future__ import annotations

import os
import sys
import csv

try:
    # Reuse the engine's odds parser so fractions and decimals both work.
    from scraper import fractional_to_decimal
except Exception:  # noqa: BLE001  keep analyse.py runnable even if import fails
    def fractional_to_decimal(odds):
        if odds is None:
            return None
        s = str(odds).strip().lower()
        if not s:
            return None
        if s in ("evens", "evs", "even", "1/1"):
            return 2.0
        try:
            if "/" in s:
                n, d = s.split("/")
                return round(float(n) / float(d) + 1.0, 4) if float(d) else None
            v = float(s)
            return v if v > 1 else None
        except (ValueError, ZeroDivisionError):
            return None


DEFAULT_CSV = os.path.join("results", "performance_metrics.csv")

BANDS = [
    ("Shorts (<4.0)", lambda d: d < 4.0),
    ("Mid-price (4.0-15.0)", lambda d: 4.0 <= d <= 15.0),
    ("Longshots (>15.0)", lambda d: d > 15.0),
]


def _load(path: str) -> list:
    if not os.path.exists(path):
        print(f"No results file at {path}. Run the engine and log some selections first.")
        return []
    with open(path, "r", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _num(x):
    """Parse a money/number field (strip £ etc.), or None."""
    if x is None:
        return None
    s = str(x).strip().lstrip("£$").replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# A bet is settled once it has a known result (not blank / PENDING).
_SETTLED_RESULTS = ("WON", "PLACED", "UNPLACED", "WIN", "LOSE", "LOST")


def _enrich(rows: list) -> list:
    """Attach parsed decimals, stake, P/L and a normalised result to each row."""
    out = []
    for r in rows:
        row = dict(r)
        row["_taken_dec"] = fractional_to_decimal(r.get("taken_odds"))
        row["_sp_dec"] = fractional_to_decimal(r.get("final_sp"))
        row["_result"] = (r.get("result") or "").strip().upper()
        row["_stake"] = _num(r.get("stake"))
        row["_pl"] = _num(r.get("pl"))
        out.append(row)
    return out


def _row_pl(r):
    """
    P/L for a row. Prefer the recorded P/L (handles each-way cleanly). Otherwise
    fall back to a win-only estimate from stake, result and taken odds.
    """
    if r["_pl"] is not None:
        return r["_pl"]
    stake = r["_stake"] or 1.0
    if r["_result"] == "WON" and r["_taken_dec"]:
        return stake * (r["_taken_dec"] - 1.0)
    if r["_result"] in ("WIN",) and r["_taken_dec"]:  # legacy label
        return stake * (r["_taken_dec"] - 1.0)
    return -stake  # PLACED/UNPLACED/LOSE on a win bet


def _metrics(rows: list) -> dict:
    """
    Metrics over enriched rows. Settled = any known result. ROI is on actual
    money staked (P/L divided by total stake); strike rate counts outright wins.
    """
    settled = [r for r in rows if r["_result"] in _SETTLED_RESULTS]
    wins = [r for r in settled if r["_result"] in ("WON", "WIN")]
    placed = [r for r in settled if r["_result"] == "PLACED"]
    n = len(settled)

    staked = sum((r["_stake"] or 0.0) for r in settled)
    profit = sum(_row_pl(r) for r in settled)

    clv_rows = [r for r in rows if r["_taken_dec"] and r["_sp_dec"]]
    clv_vals = [(r["_taken_dec"] / r["_sp_dec"] - 1.0) * 100 for r in clv_rows]

    return {
        "settled": n,
        "wins": len(wins),
        "placed": len(placed),
        "strike_pct": (100.0 * len(wins) / n) if n else None,
        "staked": staked,
        "profit": profit,
        "roi_pct": (100.0 * profit / staked) if staked else None,
        "avg_clv_pct": (sum(clv_vals) / len(clv_vals)) if clv_vals else None,
        "clv_n": len(clv_vals),
    }


def _fmt(v, suffix="%"):
    return f"{v:+.2f}{suffix}" if isinstance(v, (int, float)) else "   -  "


def _print_block(title: str, m: dict) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    print(f"  settled bets : {m['settled']}  (wins {m['wins']}, placed {m['placed']})")
    sr = f"{m['strike_pct']:.1f}%" if m["strike_pct"] is not None else "  -  "
    print(f"  strike rate  : {sr}  (outright wins only)")
    print(f"  staked       : {m['staked']:.2f}   P/L: {m['profit']:+.2f}")
    print(f"  ROI on stake : {_fmt(m['roi_pct'])}")
    clv = _fmt(m["avg_clv_pct"])
    print(f"  avg CLV      : {clv}   (over {m['clv_n']} priced row(s))")


def main(path: str = DEFAULT_CSV) -> None:
    rows = _enrich(_load(path))
    if not rows:
        return

    pending = sum(1 for r in rows if r["_result"] == "PENDING")
    print("=" * 56)
    print("     HORSE RACING ENGINE  |  FORENSIC ANALYTICS")
    print("=" * 56)
    print(f"  rows: {len(rows)}   pending (unsettled): {pending}")

    overall = _metrics(rows)
    _print_block("OVERALL (settled only)", overall)

    for label, test in BANDS:
        # Band on the taken price where we have it, else the SP.
        band_rows = [r for r in rows
                     if (r["_taken_dec"] or r["_sp_dec"])
                     and test(r["_taken_dec"] or r["_sp_dec"])]
        _print_block(label, _metrics(band_rows))

    print("\nReminder: small samples are noise. Strike rate and ROI need a few")
    print("hundred settled bets to mean much; CLV is the better early signal.\n")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV)
