"""
brain.py  |  Analysis layer for HorseRacingEngine

What this layer actually does, and an honest note on what it does not:

  1. It computes the market maths in Python, deterministically: the book
     percentage and overround, and Shin-de-vigged fair probabilities (margin
     cleared, correcting the favourite-longshot bias). The de-vig and book live
     in scraper.py and are imported here, so there is ONE source of truth and
     the numbers can never drift between files.
  2. It then asks Claude for a structured handicapping read on top of that
     anchor: a qualitative, form-based second opinion returned as strict JSON.

  Claude is NOT a calibrated probabilistic model. It will not systematically
  out-predict an efficient betting market from a few lines of form. Treat the
  Shin fair probabilities as the quantitative baseline and Claude's numbers as
  discussion, not as a staking signal. The 'value' flag marks where Claude's
  read diverges from the market; it is a prompt for human judgement only.
"""

from __future__ import annotations

import json
import logging

from anthropic import Anthropic

# Single source of truth for the market maths: the de-vig and book live in
# scraper.py, so brain and the orchestrator can never drift apart.
from scraper import compute_book, shin_devig

log = logging.getLogger("brain")

DEFAULT_MODEL = "claude-sonnet-5-5"   # valid Claude API model id (Claude Sonnet 5.5)
VALUE_MARGIN = 0.03                   # Claude prob must beat fair prob by 3pts to flag


# ----------------------------------------------------------------------------
# Deterministic market maths (no model involved)
# ----------------------------------------------------------------------------

def implied_prob(decimal_odds):
    if not decimal_odds or decimal_odds <= 1:
        return None
    return 1.0 / decimal_odds


def compute_market_frame(runners: list) -> dict:
    """
    Derive the book (percentage, overround, field_complete, Shin z) and the
    Shin-de-vigged fair probability per runner. 'implied_pct' is the raw 1/odds
    (with the vig still in); 'fair_pct' is Shin's margin-cleared estimate, which
    is the anchor the value flag compares against. Uses scraper's functions so
    the numbers match the rest of the pipeline exactly.
    """
    book = dict(compute_book(runners))
    fair_probs, z = shin_devig(runners)
    book["shin_z"] = round(z, 4)

    runners_frame = {}
    for r in runners:
        name = r["name"]
        d = r.get("decimal_odds")
        implied = implied_prob(d)
        fp = fair_probs.get(name)
        runners_frame[name] = {
            "decimal_odds": d,
            "implied_pct": round(implied * 100, 1) if implied else None,
            "fair_pct": round(fp * 100, 1) if fp else None,
        }
    return {"book": book, "runners": runners_frame}


# ----------------------------------------------------------------------------
# Claude call
# ----------------------------------------------------------------------------

def _build_prompt(region_name, race_data, market_frame):
    book = market_frame["book"]
    movement = race_data.get("movement") or {}
    movers = {n: m for n, m in movement.items()
              if m.get("label") in ("STEAMER", "DRIFTER")}
    velocity = json.dumps(movers, indent=2) if movers else "none recorded"

    return f"""You are an experienced handicapper reviewing one upcoming race from the {region_name} region.

RACE DATA (runner names, current/opening odds, form):
{json.dumps(race_data.get("runners", []), indent=2)}

MARKET MATHS (computed locally in Python, do NOT recompute):
- Book percentage: {book['book_percentage']}%  (overround / takeout: {book['overround']}%)
- The de-vigged fair probability per runner is in the market frame below. This
  is the market's own implied view with the margin removed (Shin's method, which
  corrects the favourite-longshot bias). Treat it as the quantitative anchor,
  not a number to beat for its own sake.
MARKET FRAME:
{json.dumps(market_frame['runners'], indent=2)}

MARKET VELOCITY (steamers shortened, drifters lengthened, open vs now):
{velocity}

STRATEGY PRIORS (a chosen stance, not a measured edge; apply as a lean, and
still justify each runner on its own merits):
- Favour progressive, lightly-raced or 'second-string' types that may be
  underestimated, especially any with a credible excuse for a recent poor run.
- Lean against favourites that look over-backed relative to their profile.
- Read a steamer as possible smart money arriving and a drifter as confidence
  leaving; weigh both, but do not follow a move blindly.

YOUR TASK:
1. Weigh the form, the odds, the de-vigged fair probabilities, the velocity
   signal, and the strategy priors together.
2. Give each runner your own win probability as a percentage. The whole field
   MUST sum to exactly 100.0. Where your number differs from the fair
   probability, that gap is your read, and it is a discussion point, not a
   guaranteed edge.
3. For each runner, write one plain sentence on the key reason for your number.

Respond with STRICT, valid JSON only. No prose before or after. Use this shape:
{{
  "track": "Track Name",
  "predictions": [
    {{
      "horse": "Horse Name",
      "win_probability_pct": 0.0,
      "handicap_note": "One sentence on the main reason (form, price, velocity, prior)."
    }}
  ]
}}"""


def _extract_json(text: str) -> dict:
    """
    Pull the first valid JSON object out of a model response, tolerating code
    fences or stray text around it.
    """
    if not text:
        raise ValueError("empty response")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1]
        if cleaned.lstrip().lower().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in response")
    return json.loads(cleaned[start : end + 1])


def _collect_text(response) -> str:
    out = ""
    for block in response.content:
        if getattr(block, "type", None) == "text" or hasattr(block, "text"):
            out += getattr(block, "text", "")
    return out


def _merge_and_flag(parsed: dict, market_frame: dict) -> dict:
    """
    Attach the deterministic fair probability to each prediction and flag value
    where Claude's probability beats the fair probability by the margin.
    Also renormalises Claude's numbers if they stray from 100.
    """
    preds = parsed.get("predictions", [])
    total = sum((p.get("win_probability_pct") or 0) for p in preds)
    fair_lookup = market_frame["runners"]

    for p in preds:
        # Renormalise so the field sums to 100 even if the model drifted.
        if total > 0:
            p["win_probability_pct"] = round(p["win_probability_pct"] * 100.0 / total, 1)

        frame = fair_lookup.get(p.get("horse"), {})
        fair_pct = frame.get("fair_pct")
        p["market_fair_pct"] = fair_pct
        p["decimal_odds"] = frame.get("decimal_odds")
        if fair_pct is not None:
            edge = p["win_probability_pct"] - fair_pct
            p["edge_pts"] = round(edge, 1)
            p["is_value"] = edge >= (VALUE_MARGIN * 100)
        else:
            p["edge_pts"] = None
            p["is_value"] = False

    parsed["book"] = market_frame["book"]
    return parsed


def analyze_race_with_claude(region_name, race_data, api_key,
                             model: str = DEFAULT_MODEL, max_retries: int = 1) -> dict:
    """
    Run the full analysis for one race. Returns a dict with the market frame
    and (if the model responds cleanly) Claude's predictions with value flags.
    Never raises: on any failure it returns the deterministic market frame plus
    an 'error' key so the orchestrator can carry on.
    """
    runners = race_data.get("runners", [])
    market_frame = compute_market_frame(runners)

    if not api_key:
        return {"error": "No API key supplied; returning market frame only.",
                "book": market_frame["book"], "market_frame": market_frame}

    client = Anthropic(api_key=api_key)
    prompt = _build_prompt(region_name, race_data, market_frame)

    attempt = 0
    last_err = None
    while attempt <= max_retries:
        try:
            response = client.messages.create(
                model=model,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            text = _collect_text(response)
            parsed = _extract_json(text)
            return _merge_and_flag(parsed, market_frame)
        except (ValueError, json.JSONDecodeError) as exc:
            # Model returned something unparseable: retry once asking for JSON only.
            last_err = exc
            log.warning("JSON parse failed (attempt %d): %s", attempt + 1, exc)
            prompt += "\n\nYour previous reply was not valid JSON. Reply with the JSON object ONLY."
        except Exception as exc:  # noqa: BLE001  network/API/model errors
            last_err = exc
            log.error("Claude call failed: %s", exc)
            break
        attempt += 1

    return {"error": f"Analysis failed: {last_err}",
            "book": market_frame["book"], "market_frame": market_frame}
