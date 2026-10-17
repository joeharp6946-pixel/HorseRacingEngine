"""
main.py  |  Orchestrator for HorseRacingEngine

Links the data layer to the analysis layer: pulls today's cards (already
time-gated to the next few hours by scraper.py), records an odds snapshot
(so steamers/drifters build up over repeated runs), computes the local
market maths, and produces the analysis, then saves a timestamped JSON report.

Two analysis modes, chosen by whether an Anthropic API key is present:
  * With a key  -> brain.py asks Claude for a structured, captured JSON read,
                   shown as a value board and saved in the report.
  * Without a key -> the Copilot bridge: Python does all the local maths
                   (overround, price movement), assembles a handicapping
                   prompt with those real numbers already in it, puts it on
                   your clipboard, and opens Microsoft Copilot so you paste
                   and send. Copilot's reply is read in the browser; it is a
                   human-in-the-loop step and is NOT captured back into the
                   engine.

Every flagged selection is logged to results/performance_metrics.csv (win or
lose) so the history is unbiased; analyse.py reports strike rate, ROI and CLV.

Run it repeatedly through the day; the price-movement signal only appears once
at least two snapshots exist in odds_history.json.
"""

from __future__ import annotations

import os
import csv
import sys
import json
import logging
import subprocess
import webbrowser
from datetime import datetime, timezone

from scraper import fetch_live_cards, OddsHistoryTracker, fractional_to_decimal

from brain import analyze_race_with_claude

log = logging.getLogger("main")

RESULTS_DIR = "results"
COPILOT_URL = "https://copilot.microsoft.com/"
COPILOT_PROMPT_FILE = "copilot_prompt.txt"
PERFORMANCE_CSV = os.path.join(RESULTS_DIR, "performance_metrics.csv")

# ----------------------------------------------------------------------------
# Composite confidence score + aggressive trigger
#
# Weights (sum to 1.0): model edge 35%, steam velocity 25%, sentiment 20%,
# track-bias suitability 20%. Each sub-score is normalised to 0..1, so the
# composite is 0..10.
#
# HONEST LIMITS, read these before trusting the number:
#  * Per-runner MODEL EDGE only exists when brain.py runs (an API key is set).
#    In the no-key Copilot flow there is no independent model, so the model-edge
#    sub-score is 0 and 'model_edge_available' is False. The aggressive trigger
#    needs a real model edge, so it cannot fire without a key.
#  * BIAS suitability needs each runner's running style, which we do not parse,
#    so it defaults to neutral (0.5) for every runner. It only differentiates
#    runners once per-runner style data is added.
#  * SENTIMENT is per-runner where the chatter named the horse, else race-level.
# So in no-key mode the composite mostly separates runners by steam. Treat the
# score as a rough triage aid, never as a calibrated probability or a staking
# signal.
# ----------------------------------------------------------------------------

W_MODEL_EDGE = 0.35
W_STEAM = 0.25
W_SENTIMENT = 0.20
W_BIAS = 0.20

AGGR_EDGE_MIN = 1.5      # model edge in points
AGGR_STEAM_MIN = 10.0    # steam (shortening) in percent
AGGR_RANK_MIN = 2        # must sit beyond market rank 2 (an outsider)
OVERLAY_BANNER = "\U0001F525 HIGH-CONFIDENCE PLAYBOOK OVERLAY"  # explicit per spec


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _market_ranks(runners: list) -> dict:
    """Rank runners by decimal odds ascending (favourite = 1). Unpriced last."""
    priced = [(r["name"], r.get("decimal_odds")) for r in runners if r.get("decimal_odds")]
    priced.sort(key=lambda t: t[1])
    ranks = {name: i + 1 for i, (name, _) in enumerate(priced)}
    nxt = len(priced) + 1
    for r in runners:
        ranks.setdefault(r["name"], nxt)
    return ranks


def _edge_for(name: str, analysis: dict):
    """Per-runner model edge in points from brain.py's predictions, or None."""
    for p in (analysis or {}).get("predictions", []) or []:
        if p.get("horse") == name:
            return p.get("edge_pts")
    return None


def compute_composite(card: dict, analysis: dict) -> dict:
    """
    Build the per-runner composite score and aggressive-trigger flag for one
    race. Returns {runner_name: {composite, trigger, model_edge, steam_pct,
    market_rank, model_edge_available}}.
    """
    runners = card.get("runners", [])
    info = card.get("race_info", {})
    movement = card.get("movement") or {}
    ranks = _market_ranks(runners)

    race_sentiment = info.get("sentiment_score") or 0
    bias_sc = 0.5                                     # neutral; see module note

    out = {}
    for r in runners:
        name = r["name"]

        # Prefer this runner's own sentiment (from chatter that named it);
        # fall back to the race-level reading when the horse wasn't named.
        sentiment = r.get("sentiment_index")
        if sentiment is None:
            sentiment = race_sentiment
        sentiment_sc = _clamp01((sentiment + 3) / 6.0)   # -3..+3  ->  0..1

        edge = _edge_for(name, analysis)
        edge_available = edge is not None
        edge_sc = _clamp01(edge / 5.0) if edge_available else 0.0  # 5pts -> full

        mv = movement.get(name) or {}
        steam_pct = -mv["pct_change"] if mv.get("label") == "STEAMER" else 0.0
        steam_sc = _clamp01(steam_pct / 20.0)         # 20% shortening -> full

        composite = round(10.0 * (
            W_MODEL_EDGE * edge_sc
            + W_STEAM * steam_sc
            + W_SENTIMENT * sentiment_sc
            + W_BIAS * bias_sc
        ), 1)

        rank = ranks.get(name, 99)
        trigger = bool(edge_available and edge > AGGR_EDGE_MIN
                       and steam_pct > AGGR_STEAM_MIN and rank > AGGR_RANK_MIN)

        out[name] = {
            "composite": composite,
            "trigger": trigger,
            "model_edge": edge,
            "model_edge_available": edge_available,
            "steam_pct": round(steam_pct, 1),
            "market_rank": rank,
        }
    return out

# The handicapping playbook prepended to every Copilot clipboard prompt. These
# are universal daily rules (not meeting-specific). Edit freely; the per-race
# data is appended underneath by build_copilot_prompt().
# NOTE: rule 1 is a deliberate directional bias, so the output is the case
# against the favourite, not a neutral read.
HANDICAPPING_PLAYBOOK = """You are an expert horse racing handicapper with live web access.
Apply this playbook to EVERY race listed below. The market maths (book
overround and the steamer/drifter price deltas) has already been computed
locally and is given to you; do NOT recompute it. Before selecting, use the
live web to check late scratches or non-runners, going and weather at the
track, and any fresh market move.

PLAYBOOK
1. Value / anti-favourite bias. The public market over-anchors to obvious
   favourites, so hunt actively for progressive 'second-string' runners and
   unexposed horses, especially any with a credible excuse for a recent poor
   run (trouble in running, an unsuitable trip or ground, a stable back in form
   since). State the favourite's claim briefly, then push for the overlay
   against it.
2. Track / pacing alignment. Weight course form heavily. Favour strong
   finishers and closers suited by a likely strong early pace (a pace collapse)
   or a long galloping run-in.
3. Market movement. The steamers and drifters shown per race are real local
   price deltas: read a steamer (shortening) as money arriving and a drifter
   (lengthening) as confidence leaving, and weigh both.

OUTPUT FORMAT (strict). For EACH race, reply using exactly these four labels:
  WIN SELECTION: <horse>, one short reason
  EACH-WAY SELECTION: <horse>, one short reason
  DARK HORSE: <horse>, the value or longshot angle
  AVOID: <horse>, why its price looks wrong or overbet
Keep every line short."""


def _get_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key.strip()
    try:
        return input("Enter your Anthropic API Key (or press Enter to run market-maths only): ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def _fmt_pct(v):
    return f"{v:>5.1f}%" if isinstance(v, (int, float)) else "   -  "


def _render_race(track_key: str, card: dict, analysis: dict) -> None:
    info = card.get("race_info", {})
    print(f"\n  {track_key}  |  {info.get('going', '?')}  |  "
          f"{info.get('distance', '?')}  |  {info.get('type', '?')}")

    book = analysis.get("book", {})
    if book:
        if book.get("field_complete"):
            print(f"  Book: {book.get('book_percentage')}%   "
                  f"Overround (takeout): {book.get('overround')}%"
                  + (f"   Shin z: {book.get('shin_z')}" if book.get('shin_z') else ""))
        else:
            print(f"  Book: {book.get('book_percentage')}%   "
                  f"(partial field: {book.get('runners_priced')} priced; "
                  f"overround n/a until full field)")

    if "error" in analysis:
        print(f"  [analysis note] {analysis['error']}")

    preds = analysis.get("predictions")
    if preds:
        print(f"  {'Runner':<20}{'Odds':>7}{'Fair':>8}{'Claude':>8}{'Edge':>7}  Value")
        print("  " + "-" * 62)
        for p in sorted(preds, key=lambda x: x.get("win_probability_pct", 0), reverse=True):
            odds = p.get("decimal_odds")
            odds_s = f"{odds:>6.2f}" if isinstance(odds, (int, float)) else "   -  "
            flag = "  <== VALUE" if p.get("is_value") else ""
            edge = p.get("edge_pts")
            edge_s = f"{edge:>+5.1f}" if isinstance(edge, (int, float)) else "   - "
            print(f"  {p.get('horse', '?'):<20}{odds_s}{_fmt_pct(p.get('market_fair_pct'))}"
                  f"{_fmt_pct(p.get('win_probability_pct'))}{edge_s}{flag}")

    movement = card.get("movement") or {}
    movers = {k: v for k, v in movement.items() if v.get("label") in ("STEAMER", "DRIFTER")}
    if movers:
        print("  Market movers:")
        for name, m in movers.items():
            print(f"    {name:<20} {m['label']:<8} {m['open']} -> {m['now']} "
                  f"({m['pct_change']:+.1f}%)")

    # Composite confidence score (0-10) and aggressive-trigger banner.
    composite = card.get("composite") or {}
    if composite:
        info = card.get("race_info", {})
        extra = []
        if info.get("bias"):
            extra.append(f"bias: {info['bias']}")
        if info.get("sentiment_score"):
            extra.append(f"sentiment: {info['sentiment_score']:+d}")
        suffix = ("   (" + ", ".join(extra) + ")") if extra else ""
        print(f"  Composite confidence (0-10){suffix}:")
        for name, c in sorted(composite.items(),
                              key=lambda kv: kv[1]["composite"], reverse=True):
            edge = c["model_edge"]
            edge_s = f"{edge:+.1f}" if isinstance(edge, (int, float)) else "n/a"
            banner = f"   {OVERLAY_BANNER}" if c["trigger"] else ""
            print(f"    {name:<20} {c['composite']:>4.1f}/10  "
                  f"(edge {edge_s}, steam {c['steam_pct']:.0f}%, rank {c['market_rank']}){banner}")
        if not any(c["model_edge_available"] for c in composite.values()):
            print("    [note] no API key: model-edge weight is 0 and the")
            print("           overlay trigger is inactive (needs a model edge).")


def _save_report(payload: dict) -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(RESULTS_DIR, f"run_{stamp}.json")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except OSError as exc:
        log.error("Could not save report: %s", exc)
        return ""
    return path


# ----------------------------------------------------------------------------
# Copilot bridge (no-API-key mode)
# ----------------------------------------------------------------------------

def build_copilot_prompt(all_regions: dict) -> str:
    """
    Assemble a pre-formatted handicapping prompt from the real, locally-computed
    numbers. Copilot is asked to ADD live web context (scratches, going, weather,
    late market moves) on top, not to re-derive the maths we already did.
    """
    lines = [HANDICAPPING_PLAYBOOK, "", "RACES (next few hours):", ""]
    for region, cards in all_regions.items():
        lines.append(f"## {region}")
        for track_key, card in cards.items():
            info = card.get("race_info", {})
            book = card.get("book", {})
            lines.append(
                f"- {track_key} | going {info.get('going', '?')} | "
                f"{info.get('distance', '?')} | {info.get('type', '?')}"
            )
            if book.get("field_complete"):
                lines.append(
                    f"  book {book.get('book_percentage')}% "
                    f"(overround {book.get('overround')}%)"
                )
            elif book.get("runners_priced"):
                lines.append(
                    f"  partial book {book.get('book_percentage')}% "
                    f"({book.get('runners_priced')} priced)"
                )
            def _runner_label(r):
                price = r.get("current_odds") or r.get("decimal_odds") or "NP"
                si = r.get("sentiment_index")
                tag = f" [sentiment {si:+d}]" if si else ""
                return f"{r['name']} {price}{tag}"
            runners = ", ".join(_runner_label(r) for r in card.get("runners", []))
            lines.append(f"  runners: {runners}")
            if info.get("bias"):
                lines.append(f"  track bias: {info['bias']}")
            if info.get("sentiment_score"):
                lines.append(f"  race-level noise sentiment: {info['sentiment_score']:+d}")
            composite = card.get("composite") or {}
            triggered = [n for n, c in composite.items() if c.get("trigger")]
            if triggered:
                lines.append(f"  {OVERLAY_BANNER}: " + ", ".join(triggered))
            movers = {
                n: m for n, m in (card.get("movement") or {}).items()
                if m.get("label") in ("STEAMER", "DRIFTER")
            }
            if movers:
                mv = ", ".join(
                    f"{n} {m['label']} {m['open']}->{m['now']}" for n, m in movers.items()
                )
                lines.append(f"  market moves: {mv}")
        lines.append("")

    # How to read the per-horse sentiment index.
    lines.append("SENTIMENT INDEX")
    lines.append("Each horse may carry [sentiment +/-N], a -3..+3 keyword tag from")
    lines.append("my pasted chatter (not a measured signal). Cross-reference it with")
    lines.append("the local overround: flag a CONTRARIAN play where a horse carries")
    lines.append("positive sentiment yet sits at a long price in a loaded book, and")
    lines.append("treat positive sentiment on a short favourite as already priced in.")
    lines.append("Verify every sentiment cue against the live web before trusting it.")
    lines.append("")

    # Cross-card synthesis across every submitted race.
    lines.append("CROSS-CARD VALUE RANKING")
    lines.append("After the per-race selections above, treat all the races as one")
    lines.append("combined betting landscape. Synthesise your live web checks across")
    lines.append("the whole spread and rank your best value plays across all races,")
    lines.append("strongest first, each with a one-line reason. End with your single")
    lines.append("BEST BET and BEST EACH-WAY across the card.")
    lines.append("")
    return "\n".join(lines)


def _copy_to_clipboard(text: str) -> bool:
    """Best-effort clipboard copy using the OS native tool. No extra deps."""
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["clip"], input=text.encode("utf-16"), check=True)
        elif sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        else:
            subprocess.run(["xclip", "-selection", "clipboard"],
                           input=text.encode("utf-8"), check=True)
        return True
    except Exception:  # noqa: BLE001  clipboard is a convenience, never fatal
        return False


def launch_copilot_bridge(all_regions: dict) -> None:
    """
    Write the handicapping prompt to a file, copy it to the clipboard, and open
    Microsoft Copilot in the default browser. The reply is read in the browser;
    it is not captured back into the engine.
    """
    prompt = build_copilot_prompt(all_regions)

    try:
        with open(COPILOT_PROMPT_FILE, "w", encoding="utf-8") as fh:
            fh.write(prompt)
    except OSError as exc:
        log.error("Could not write %s: %s", COPILOT_PROMPT_FILE, exc)

    copied = _copy_to_clipboard(prompt)

    print("\n" + "=" * 56)
    print("  COPILOT BRIDGE")
    print("=" * 56)
    if copied:
        print("  The handicapping prompt is on your clipboard. In Copilot,")
        print("  paste it (Ctrl+V) and send.")
    else:
        print(f"  Clipboard unavailable. Open {COPILOT_PROMPT_FILE} and paste")
        print("  its contents into Copilot.")
    print("  Copilot's reply appears in the browser for you to read; it is")
    print("  NOT fed back into the engine. The numbers above are the engine's")
    print("  own output.")

    try:
        webbrowser.open(COPILOT_URL)
        print(f"  Opening {COPILOT_URL} ...")
    except Exception as exc:  # noqa: BLE001
        print(f"  Could not open a browser automatically ({exc}).")
        print(f"  Go to {COPILOT_URL} manually.")


# A runner is logged as a selection if the aggressive trigger fired, it is
# steaming, OR its composite clears this triage threshold. Logging happens at
# selection time for EVERY such runner, win or lose, to avoid survivorship bias.
TRIAGE_MIN = 5.0
# Ledger columns. stake/bet_type/returns/pl are filled from the bets you
# actually place (the LLM proposes them); result/finish/final_sp come at
# settle time. P/L is stored directly so each-way maths never has to be
# reconstructed. CLV is taken_odds vs final_sp when both are present.
PERF_FIELDS = ["date", "track", "distance", "horse", "bet_type", "stake",
               "taken_odds", "final_sp", "result", "finish", "returns",
               "pl", "clv_pct"]


def _read_perf_rows() -> list:
    if not os.path.exists(PERFORMANCE_CSV):
        return []
    try:
        with open(PERFORMANCE_CSV, "r", newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    except OSError as exc:
        log.error("Could not read %s: %s", PERFORMANCE_CSV, exc)
        return []


def _write_perf_rows(rows: list) -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(PERFORMANCE_CSV, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=PERF_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in PERF_FIELDS})


def log_selections(all_regions: dict) -> None:
    """
    Log EVERY runner the engine flagged (aggressive trigger, steamer, or
    composite at/above TRIAGE_MIN) as one row per selection, result PENDING and
    the SP blank. Logging all picks, win or lose, is what makes the history
    usable for strike rate and ROI; recording only winners would be survivorship
    bias. Deduplicates on (date, track, horse) so re-running does not double-log.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing = _read_perf_rows()
    seen = {(r.get("date"), r.get("track"), r.get("horse")) for r in existing}

    new_rows = []
    for region, cards in all_regions.items():
        for track_key, card in cards.items():
            info = card.get("race_info", {})
            comp = card.get("composite") or {}
            movement = card.get("movement") or {}
            for r in card.get("runners", []):
                name = r["name"]
                c = comp.get(name, {})
                # Trigger (with-key), steamer (the no-key signal), or triage.
                is_steamer = (movement.get(name) or {}).get("label") == "STEAMER"
                flagged = (c.get("trigger") or is_steamer
                           or c.get("composite", 0) >= TRIAGE_MIN)
                if not flagged:
                    continue
                key = (today, info.get("track", track_key), name)
                if key in seen:
                    continue
                seen.add(key)
                new_rows.append({
                    "date": today,
                    "track": info.get("track", track_key),
                    "distance": info.get("distance", ""),
                    "horse": name,
                    "bet_type": "",          # WIN / EW, from the bet you place
                    "stake": "",             # from the bet you place
                    "taken_odds": r.get("current_odds") or r.get("decimal_odds") or "",
                    "final_sp": "",          # filled later by settle_pending
                    "result": "PENDING",
                    "finish": "",
                    "returns": "",
                    "pl": "",
                    "clv_pct": "",
                })

    if not new_rows:
        print("\nNo runners met the selection threshold; nothing logged.")
        return
    try:
        _write_perf_rows(existing + new_rows)
        print(f"\nLogged {len(new_rows)} selection(s) as PENDING to {PERFORMANCE_CSV}.")
        print("Settle them (fill SP + result) once the races are run.")
    except OSError as exc:
        log.error("Could not write %s: %s", PERFORMANCE_CSV, exc)


def settle_pending() -> None:
    """
    Optional: walk the PENDING rows and fill in result (W/L) and final SP. CLV is
    computed from taken odds vs SP. Safe to skip; never raises on no input.
    """
    rows = _read_perf_rows()
    pending = [r for r in rows if (r.get("result") or "").upper() == "PENDING"]
    if not pending:
        return
    try:
        ans = input(f"\nSettle {len(pending)} pending selection(s) now? (Y/N): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if ans not in ("y", "yes"):
        return

    print("For each: result (w / l / Enter to skip), then final SP.")
    for r in pending:
        try:
            res = input(f"  {r['horse']} @ {r['taken_odds']} ({r['track']}) result [w/l/skip]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if res not in ("w", "win", "l", "lose", "lost"):
            continue
        r["result"] = "WIN" if res in ("w", "win") else "LOSE"
        try:
            sp_raw = input("    final SP: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        r["final_sp"] = sp_raw
        taken = fractional_to_decimal(r.get("taken_odds"))
        sp = fractional_to_decimal(sp_raw)
        if taken and sp:
            r["clv_pct"] = round((taken / sp - 1.0) * 100, 2)  # +ve = beat the SP

    try:
        _write_perf_rows(rows)
        print(f"  Updated {PERFORMANCE_CSV}. Run analyse.py for strike rate / ROI / CLV.")
    except OSError as exc:
        log.error("Could not write %s: %s", PERFORMANCE_CSV, exc)


def main():
    print("=" * 56)
    print("        GLOBAL HORSE RACING ANALYTICS ENGINE")
    print("=" * 56)

    api_key = _get_api_key()
    if not api_key:
        print("\nNo API key: running market maths and movement only (no Claude read).\n")

    tracker = OddsHistoryTracker()

    try:
        all_regions = fetch_live_cards("ALL", tracker=tracker)
    except Exception as exc:  # noqa: BLE001  never let a data failure crash the whole run
        log.error("Fatal data-layer error: %s", exc)
        print("Could not fetch any cards. Check network and credentials.")
        return

    if not all_regions:
        print("No cards returned for any region today.")
        return

    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "regions": {}}

    for region, cards in all_regions.items():
        print(f"\n{'#' * 56}\n# {region} CARDS\n{'#' * 56}")
        report["regions"][region] = {}

        for track_key, card in cards.items():
            try:
                analysis = analyze_race_with_claude(region, card, api_key)
            except Exception as exc:  # noqa: BLE001  isolate per-race failures
                log.error("Analysis crashed for %s: %s", track_key, exc)
                analysis = {"error": str(exc)}

            card["composite"] = compute_composite(card, analysis)
            _render_race(track_key, card, analysis)
            report["regions"][region][track_key] = {
                "race_info": card.get("race_info"),
                "movement": card.get("movement"),
                "composite": card.get("composite"),
                "analysis": analysis,
            }

    path = _save_report(report)
    if path:
        print(f"\nFull report saved to {path}")

    if api_key:
        print("\nReminder: 'VALUE' marks where Claude's read diverges from the")
        print("de-vigged market. It is a discussion point, not a staking signal.\n")
    else:
        # No key: hand the local numbers to Copilot for live context.
        launch_copilot_bridge(all_regions)
        print("\nReminder: the overrounds, movers and composite scores above are")
        print("computed locally. Copilot adds live context but is not a calibrated")
        print("model; treat all of it as a discussion point, not a staking signal.\n")

    # Log every flagged selection (win or lose) so the history is unbiased,
    # then optionally settle any pending results. analyse.py reads this file.
    log_selections(all_regions)
    settle_pending()


if __name__ == "__main__":
    main()
