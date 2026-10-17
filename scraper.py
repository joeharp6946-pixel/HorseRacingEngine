"""
scraper.py  |  Manual text-intake data layer for HorseRacingEngine

No network calls. The race card is harvested for free from a Copilot web query,
pasted into the terminal, and parsed here. Python then computes the real
bookmaker overround, the Shin-de-vigged fair probabilities, flags
steamers/drifters, applies the rolling 3-hour time gate, and guards against
corrupt input. It emits the exact schema brain.py and main.py expect:

    { region: { track_key: {
        "race_info":  {...},       # includes post_time, bias, sentiment_score
        "runners":    [ {...}, ],  # each has decimal_odds and sentiment_index
        "book":       {...},       # real overround + Shin z, computed in Python
        "fair_probs": {name: p},   # Shin fair probabilities (margin cleared)
        "movement":   {...},       # steamers/drifters (open vs now, or across runs)
    }}}

Workflow:
  1. Ask Copilot for a card in the format below (see card_request_template()).
  2. Paste its reply when prompted (END for next race, ALL_DONE to finish).
  3. The engine parses it, does the local maths, and hands the result on.

The parser handles the clean multi-line format AND a single continuous line
(e.g. when Windows PowerShell strips the newlines). You may also separate
runners with '||' for the most robust parse.

Expected paste format (one block per race; header keys are case-insensitive):

    TRACK: Ascot
    OFF: 2026-10-15T20:45        (ISO datetime, or just HH:MM)
    REGION: UK
    GOING: Good to Firm
    DISTANCE: 6f
    TYPE: Handicap
    BIAS: Early Speed           (optional track-bias note)
    NOISE: chatter / quotes; name the horse so sentiment attaches  (optional)
    RUNNERS:
    Royal Sovereign | 2/1 | 5/2 | 1-2-1
    Celtic Twilight | 4/1 | 4/1 | 4-3-2
    London Fog | 12/1 | 10/1 | 7-5-9

Each runner line is:  NAME | CURRENT_ODDS | OPENING_ODDS | FORM
OPENING_ODDS and FORM are optional. Odds may be fractional (5/2) or decimal.
"""

from __future__ import annotations

import os
import re
import math
import json
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("scraper")

ODDS_HISTORY_FILE = "odds_history.json"
# Rolling time gate: keep only races jumping within this many minutes from now.
TIME_GATE_MINUTES = int(os.environ.get("TIME_GATE_MINUTES", "180"))
STEAM_THRESHOLD = 0.08  # 8% decimal-price move to flag a steamer/drifter

# Input guards (see validate_runners).
MAX_IMPLIED_PROB = 0.95   # a single runner over this is almost certainly a typo
MIN_FULL_BOOK = 0.99      # booksum under this on a FULL field is implausible


class CardValidationError(Exception):
    """Raised when a parsed card is corrupt enough that it must not be used."""

_HEADER_KEYS = {
    "track": "track",
    "off": "off_time",
    "off_time": "off_time",
    "post": "off_time",
    "post_time": "off_time",
    "region": "region",
    "going": "going",
    "distance": "distance",
    "type": "type",
    "bias": "bias",      # track bias note, e.g. "Early Speed", "Closers"
    "noise": "noise",    # free-text chatter / quotes for the sentiment scan
}

# Lightweight sentiment lexicon for the NOISE block. Phrases are matched before
# single words so multi-word cues score once. Crude by design: it is a rough
# tag, not a measured signal. Edit these lists freely. Longer phrases are
# matched first so "needs the run" isn't also counted as the word "needs".
_POSITIVE_PHRASES = [
    "working well", "well backed", "landed the gamble", "back in form",
    "catching the eye", "ran well last time",
]
_NEGATIVE_PHRASES = [
    "needs the run", "needs a run", "needs run", "hates soft", "hates the ground",
    "stamina doubts", "stamina doubt", "trip too far", "out of form",
    "one to avoid", "better for the run",
]
_POSITIVE_WORDS = {
    "flying", "aimed", "peaking", "headgear", "target", "fancied", "gamble",
    "improving", "progressive", "springer", "confident", "primed", "sharp",
}
_NEGATIVE_WORDS = {
    "unsuited", "doubtful", "weak", "drifting", "sore", "vulnerable",
    "overrated", "tired", "gone",
}


# ----------------------------------------------------------------------------
# Odds / maths helpers
# ----------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fractional_to_decimal(odds):
    """
    Convert a price to decimal form. Accepts fractional ('2/1' -> 3.0,
    '9/2' -> 5.5, 'evens' -> 2.0) or already-decimal ('3.5' -> 3.5).
    Returns None if it cannot be parsed.
    """
    if odds is None:
        return None
    if isinstance(odds, (int, float)):
        return float(odds) if odds > 0 else None
    s = str(odds).strip().lower()
    if not s:
        return None
    if s in ("evens", "evs", "even", "1/1"):
        return 2.0
    if "/" in s:
        try:
            num, den = s.split("/")
            num, den = float(num), float(den)
            return round(num / den + 1.0, 4) if den else None
        except (ValueError, ZeroDivisionError):
            return None
    try:
        val = float(s)
        return val if val > 1 else None
    except ValueError:
        return None


def compute_book(runners: list) -> dict:
    """
    Compute the real bookmaker book locally from decimal odds. No constants.
      book_percentage = sum of implied probabilities (1/decimal) * 100
      overround       = book_percentage - 100 (the margin / takeout)
    'overround' is only a true takeout once the whole field is priced, so
    field_complete flags whether the sum has reached 100%.
    """
    total_implied = 0.0
    priced = 0
    for r in runners:
        d = r.get("decimal_odds")
        if d and d > 1:
            total_implied += 1.0 / d
            priced += 1
    return {
        "book_percentage": round(total_implied * 100, 2),
        "overround": round((total_implied - 1.0) * 100, 2),
        "runners_priced": priced,
        "field_complete": total_implied >= 1.0,
    }


def shin_devig(runners: list) -> tuple:
    """
    Shin's de-vig. Returns (fair_probs {name: prob}, z) where z is the estimated
    proportion of informed ('sharp') money the layer prices against.

    Shin models the bookmaker facing a fraction z of insiders, which is what
    produces the favourite-longshot bias (longshots carry more margin). Starting
    from the inverse odds r_i = 1/odds_i and booksum B = sum(r_i), the fair
    probability of each runner is

        p_i = ( sqrt(z^2 + 4(1-z) r_i^2 / B) - z ) / ( 2(1-z) )

    with z chosen so the p_i sum to 1. sum(p_i) falls monotonically in z, so we
    bisection-solve for z. This lifts the margin off longshots less harshly than
    flat proportional normalisation, which matters because the playbook targets
    5/1 to 14/1 runners.

    Only valid on an over-round book (B > 1, i.e. a reasonably full field). On a
    partial paste (B <= 1) Shin does not apply, so we fall back to proportional
    normalisation and return z = 0.
    """
    priced = [(r["name"], r["decimal_odds"]) for r in runners
              if r.get("decimal_odds") and r["decimal_odds"] > 1]
    if not priced:
        return {}, 0.0

    names = [n for n, _ in priced]
    r = [1.0 / o for _, o in priced]
    B = sum(r)

    # Partial / underround book: Shin undefined, use proportional normalisation.
    if B <= 1.0 + 1e-9:
        return {n: ri / B for n, ri in zip(names, r)}, 0.0

    def sum_p(z: float) -> float:
        total = 0.0
        for ri in r:
            total += (math.sqrt(z * z + 4 * (1 - z) * ri * ri / B) - z) / (2 * (1 - z))
        return total

    # sum_p(0) = sqrt(B) > 1; sum_p decreases in z. Bisection for sum_p(z) = 1.
    lo, hi = 0.0, 0.9999
    if sum_p(lo) < 1.0:                      # defensive; should not happen for B>1
        return {n: ri / B for n, ri in zip(names, r)}, 0.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if sum_p(mid) > 1.0:
            lo = mid
        else:
            hi = mid
    z = (lo + hi) / 2

    denom = 2 * (1 - z)
    probs = {n: (math.sqrt(z * z + 4 * (1 - z) * ri * ri / B) - z) / denom
             for n, ri in zip(names, r)}
    # Clear any tiny residual so the field sums to exactly 1.
    tot = sum(probs.values())
    if tot > 0:
        probs = {n: p / tot for n, p in probs.items()}
    return probs, z


def validate_runners(runners: list) -> tuple:
    """
    Upstream input guards. Returns (hard_problems, soft_problems) as two lists of
    human-readable strings.

    HARD (corruption; the card must not be used or logged):
      * any priced runner implying > MAX_IMPLIED_PROB, which on a 5/1-to-14/1
        card is almost always a mistyped or mis-transcribed price.
    SOFT (worth flagging, not fatal):
      * booksum below MIN_FULL_BOOK. Expected for a partial paste (a subset of
        the field), so this is a warning, not a halt.
    """
    hard, soft = [], []
    for r in runners:
        d = r.get("decimal_odds")
        if d and d > 1 and (1.0 / d) > MAX_IMPLIED_PROB:
            hard.append(
                f"{r['name']}: price {r.get('current_odds') or d} implies "
                f"{100.0 / d:.1f}% (> {MAX_IMPLIED_PROB * 100:.0f}%); likely a "
                f"transcription error"
            )
    booksum = sum(1.0 / r["decimal_odds"] for r in runners
                  if r.get("decimal_odds") and r["decimal_odds"] > 1)
    if booksum and booksum < MIN_FULL_BOOK:
        soft.append(
            f"book sums to {booksum * 100:.1f}% (< {MIN_FULL_BOOK * 100:.0f}%); "
            f"fine for a partial paste, but on a full field it is implausible"
        )
    return hard, soft


def _parse_post_time(race_info: dict):
    """
    Return a tz-aware UTC datetime for a card's post time, or None. Accepts a
    full ISO datetime (post_time / off_time), or a bare 'HH:MM' combined with
    today's UTC date.

    NOTE: a bare 'HH:MM' is assumed to be in UTC. For cross-zone accuracy, have
    Copilot give a full ISO datetime with an offset.
    """
    raw = race_info.get("post_time") or race_info.get("off_time")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    try:
        hh, mm = str(raw).strip().split(":")[:2]
        today = datetime.now(timezone.utc).date()
        return datetime(today.year, today.month, today.day,
                        int(hh), int(mm), tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _blank_runner(name=None):
    """A runner record with the fields the analysis layer may look for."""
    return {
        "name": name,
        "current_odds": None,
        "decimal_odds": None,
        "open_odds": None,
        "open_decimal": None,
        "recent_form": None,
    }


def compute_movement_from_open(runners: list, threshold: float = STEAM_THRESHOLD) -> dict:
    """
    Immediate price velocity from the opening vs current price supplied in the
    same paste. Returns {name: {open, now, pct_change, label}} where a negative
    pct_change means the price shortened (a steamer).
    """
    out = {}
    for r in runners:
        opened = r.get("open_decimal")
        now = r.get("decimal_odds")
        if not opened or not now:
            continue
        pct = (now - opened) / opened
        if pct <= -threshold:
            label = "STEAMER"
        elif pct >= threshold:
            label = "DRIFTER"
        else:
            label = "stable"
        out[r["name"]] = {
            "open": round(opened, 2),
            "now": round(now, 2),
            "pct_change": round(pct * 100, 1),
            "label": label,
        }
    return out


# ----------------------------------------------------------------------------
# Text parsing
# ----------------------------------------------------------------------------

def card_request_template() -> str:
    """The text to paste into Copilot (STEP 1) so its reply parses cleanly."""
    return (
        "List the full race card for the race I name, using the live web. Reply "
        "ONLY in this exact plain-text format, no commentary:\n\n"
        "TRACK: <course>\n"
        "OFF: <ISO datetime or HH:MM, 24h>\n"
        "REGION: <UK|IE|USA|AUS>\n"
        "GOING: <going>\n"
        "DISTANCE: <distance>\n"
        "TYPE: <race type>\n"
        "BIAS: <track bias, optional>\n"
        "NOISE: <chatter / quotes, optional; name the horse so sentiment attaches>\n"
        "RUNNERS:\n"
        "<Horse Name> | <current odds> | <opening odds> | <recent form>\n"
        "... one line per runner ...\n"
    )


# Keys whose "KEY:" label marks a header value or the runners section. Used in
# lookahead so a value stops at the next label, whether on a new line or inline.
_LOOKAHEAD_KEYS = ("TRACK|OFF_TIME|OFF|POST_TIME|POST|REGION|GOING|DISTANCE|"
                   "TYPE|BIAS|NOISE|RUNNERS")


def _score_text(text: str) -> int:
    """
    Raw (unclamped) sentiment of a piece of text: +1 per positive phrase/word,
    -1 per negative. Phrases are consumed first (longest first) so a multi-word
    cue is not also counted as its component words.
    """
    if not text:
        return 0
    t = " " + text.lower() + " "
    score = 0
    for phrase in sorted(_POSITIVE_PHRASES, key=len, reverse=True):
        while phrase in t:
            score += 1
            t = t.replace(phrase, " ", 1)
    for phrase in sorted(_NEGATIVE_PHRASES, key=len, reverse=True):
        while phrase in t:
            score -= 1
            t = t.replace(phrase, " ", 1)
    for tok in re.findall(r"[a-z']+", t):
        if tok in _POSITIVE_WORDS:
            score += 1
        elif tok in _NEGATIVE_WORDS:
            score -= 1
    return score


def compute_sentiment_score(noise: str) -> int:
    """
    Race-level sentiment of the whole NOISE block, clamped to -3..+3. A rough
    keyword/phrase tag, not a calibrated signal.
    """
    return max(-3, min(3, _score_text(noise or "")))


def compute_runner_sentiment(noise: str, runner_names: list) -> dict:
    """
    Per-runner sentiment index (-3..+3). The NOISE text is split into clauses;
    each clause is scored and attributed to whichever runner(s) it names. A
    clause that names no runner (e.g. 'the favourite looks vulnerable') is not
    attributed to anyone. Returns {name: index} only for runners that were named
    in the chatter.
    """
    out = {}
    if not noise:
        return out
    clauses = re.split(r"[.;,\n]", noise)
    for clause in clauses:
        cl = clause.strip()
        if not cl:
            continue
        low = cl.lower()
        named = [n for n in runner_names if n and n.lower() in low]
        if not named:
            continue
        sc = _score_text(cl)
        if sc == 0:
            continue
        for n in named:
            out[n] = max(-3, min(3, out.get(n, 0) + sc))
    return out


def _is_odds(s: str) -> bool:
    """True if a segment looks like a price (fraction, decimal, or 'evens')."""
    t = s.strip().lower()
    if t in ("evens", "evs", "even"):
        return True
    if "/" in t:
        return bool(re.fullmatch(r"\d+(?:\.\d+)?/\d+(?:\.\d+)?", t))
    return bool(re.fullmatch(r"\d+(?:\.\d+)?", t))


def _split_form_and_name(seg: str):
    """
    On a single pasted line one runner's FORM is glued to the next runner's NAME
    inside the same pipe segment (e.g. '1-2-1 Celtic Twilight'). Split it: a
    leading form-like token (digits, dashes, letters like P/F/U, not a price) is
    the form, the rest is the next name. If the lead token is not form-like,
    there was no form and the whole segment is the next name.
    """
    seg = seg.strip()
    m = re.match(r"^([0-9A-Za-z/\-]+)\s+(.+)$", seg)
    if m and re.search(r"[\d/\-]", m.group(1)) and not _is_odds(m.group(1)):
        return m.group(1), m.group(2).strip()
    return None, seg


def _runner_from_fields(name, cur, opn, form):
    """Build a runner record from its four raw fields (any may be None)."""
    runner = _blank_runner(name.strip() if name else name)
    if cur:
        runner["current_odds"] = cur.strip()
        runner["decimal_odds"] = fractional_to_decimal(cur)
    if opn:
        runner["open_odds"] = opn.strip()
        runner["open_decimal"] = fractional_to_decimal(opn)
    if form:
        runner["recent_form"] = form.strip()
    return runner


def _parse_runners_blob(blob: str) -> list:
    """
    Parse the text after RUNNERS: into runner records. Handles three shapes:
      1. '||' between runners (most robust; fields split on single '|').
      2. one runner per line (newlines survived).
      3. a single continuous line: single '|' between a runner's fields and
         whitespace between runners, reconstructed by detecting the two odds
         that every runner carries.
    """
    blob = blob.strip()
    if not blob:
        return []

    # 1) Explicit runner separator '||'.
    if "||" in blob:
        out = []
        for chunk in blob.split("||"):
            parts = [p.strip() for p in chunk.split("|")]
            if parts and parts[0]:
                out.append(_runner_from_fields(
                    parts[0],
                    parts[1] if len(parts) > 1 else None,
                    parts[2] if len(parts) > 2 else None,
                    parts[3] if len(parts) > 3 else None,
                ))
        return out

    # 2) One runner per line (or a single line holding just one runner).
    line_runners = [ln for ln in blob.splitlines() if "|" in ln]
    if len(line_runners) > 1 or (len(line_runners) == 1 and blob.count("|") <= 3):
        out = []
        for ln in line_runners:
            parts = [p.strip() for p in ln.split("|")]
            if parts and parts[0]:
                out.append(_runner_from_fields(
                    parts[0],
                    parts[1] if len(parts) > 1 else None,
                    parts[2] if len(parts) > 2 else None,
                    parts[3] if len(parts) > 3 else None,
                ))
        return out

    # 3) Single continuous line: walk the pipe segments, pairing the two odds.
    segs = [s.strip() for s in blob.split("|") if s.strip()]
    out = []
    name = segs[0] if segs else None
    idx = 1
    while name is not None and idx <= len(segs):
        cur = opn = form = None
        if idx < len(segs) and _is_odds(segs[idx]):
            cur = segs[idx]; idx += 1
        if idx < len(segs) and _is_odds(segs[idx]):
            opn = segs[idx]; idx += 1
        next_name = None
        if idx < len(segs):
            if idx == len(segs) - 1:
                form = segs[idx]; idx += 1          # lone trailing form
            else:
                form, next_name = _split_form_and_name(segs[idx]); idx += 1
        out.append(_runner_from_fields(name, cur, opn, form))
        name = next_name
    return out


def _extract_headers(block: str) -> dict:
    """Pull header values from a block whether they are on their own lines or inline."""
    info = {}
    for key in ("TRACK", "OFF_TIME", "OFF", "POST_TIME", "POST",
                "REGION", "GOING", "DISTANCE", "TYPE", "BIAS", "NOISE"):
        pat = rf"{key}\s*:\s*(.*?)(?=\s*(?:{_LOOKAHEAD_KEYS})\s*:|$)"
        m = re.search(pat, block, flags=re.IGNORECASE | re.DOTALL)
        if m:
            val = m.group(1).strip()
            dest = _HEADER_KEYS.get(key.lower())
            if val and dest and dest not in info:
                info[dest] = val
    return info


def parse_card_text(raw: str) -> list:
    """
    Parse pasted text into a list of (region, track_key, card) tuples. Works
    with the clean multi-line format and with PowerShell-squished single-line
    pastes alike: blocks split at each 'TRACK:', headers found by their labels,
    runners rebuilt from the pipe structure. Blocks with no runners are skipped.
    """
    results = []
    # Split into race blocks at each TRACK: label (inline or on its own line).
    blocks = re.split(r"(?=\bTRACK\s*:)", raw, flags=re.IGNORECASE)
    for block in blocks:
        if "|" not in block:
            continue
        info = _extract_headers(block)
        rm = re.search(r"RUNNERS\s*:\s*(.*)$", block, flags=re.IGNORECASE | re.DOTALL)
        runners_blob = rm.group(1) if rm else block  # no label -> whole tail
        runners = _parse_runners_blob(runners_blob)
        if not runners:
            continue

        track = info.get("track", "Unknown")
        raw_off = info.get("off_time", "")
        region = (info.get("region") or "UNKNOWN").upper()
        # Keep the full value for gating in post_time; show a clean HH:MM label.
        dt = _parse_post_time({"post_time": raw_off})
        display_off = dt.strftime("%H:%M") if dt else raw_off
        track_key = f"{track} {display_off}".strip()
        noise = info.get("noise", "")
        race_info = {
            "track": track,
            "going": info.get("going"),
            "distance": info.get("distance"),
            "type": info.get("type"),
            "region": region,
            "off_time": display_off,
            "post_time": raw_off or None,
            "race_id": f"{track}-{raw_off}" if raw_off else track,
            "bias": info.get("bias"),                       # race/track bias note
            "noise": noise or None,                         # raw chatter kept verbatim
            "sentiment_score": compute_sentiment_score(noise),  # -3..+3 (race-level)
        }
        # Attach a per-runner sentiment_index where the chatter named the horse.
        per_runner = compute_runner_sentiment(noise, [r["name"] for r in runners])
        for r in runners:
            r["sentiment_index"] = per_runner.get(r["name"], 0)
        results.append((region, track_key, {"race_info": race_info, "runners": runners}))
    return results


# ----------------------------------------------------------------------------
# Odds history tracker (unchanged behaviour: reads/records across runs)
# ----------------------------------------------------------------------------

class OddsHistoryTracker:
    """
    Persists timestamped odds snapshots to odds_history.json and computes
    market shortening (steamers) and lengthening (drifters) per runner across
    runs. Used alongside the immediate open-vs-now velocity from each paste.
    """

    def __init__(self, path: str = ODDS_HISTORY_FILE, steam_threshold: float = STEAM_THRESHOLD):
        self.path = path
        self.steam_threshold = steam_threshold
        self.history = self._load()

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read %s (%s); starting fresh.", self.path, exc)
            return {}

    def save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.history, fh, indent=2)
        except OSError as exc:
            log.error("Could not write %s: %s", self.path, exc)

    def record(self, track_key: str, runners: list) -> None:
        """Append a snapshot of current decimal prices for this track, then save."""
        snapshot = {
            "ts": _now_iso(),
            "prices": {
                r["name"]: r.get("decimal_odds")
                for r in runners
                if r.get("name") and r.get("decimal_odds")
            },
        }
        if not snapshot["prices"]:
            return
        self.history.setdefault(track_key, []).append(snapshot)
        self.save()

    def movement(self, track_key: str) -> dict:
        """Compare the earliest and latest recorded snapshot for this track."""
        snaps = self.history.get(track_key, [])
        if len(snaps) < 2:
            return {}
        first, last = snaps[0]["prices"], snaps[-1]["prices"]
        result = {}
        for name, now in last.items():
            opened = first.get(name)
            if not opened or not now:
                continue
            pct = (now - opened) / opened
            if pct <= -self.steam_threshold:
                label = "STEAMER"
            elif pct >= self.steam_threshold:
                label = "DRIFTER"
            else:
                label = "stable"
            result[name] = {
                "open": round(opened, 2),
                "now": round(now, 2),
                "pct_change": round(pct * 100, 1),
                "label": label,
            }
        return result


# ----------------------------------------------------------------------------
# Console intake + public entry point
# ----------------------------------------------------------------------------

def read_pasted_text() -> str:
    """
    Collect one or more pasted race cards from the console.
      END      finishes the current race and prompts for the next.
      ALL_DONE saves the current race too, then ends the whole batch.
      EOF (Ctrl-Z then Enter on Windows / Ctrl-D on Mac/Linux) also ends it.
    Returns every collected block joined together for parse_card_text to split.
    """
    print("\nSTEP 1 - ask Copilot for each card using this request:\n")
    print(card_request_template())
    print("STEP 2 - paste each card below.")
    print("         END      finishes the current race and starts the next.")
    print("         ALL_DONE finishes the whole batch.")
    print("         (or Ctrl-Z then Enter on Windows / Ctrl-D on Mac/Linux)\n")

    blocks = []
    race_no = 1
    while True:
        print(f"-- Race {race_no}: paste now, then END or ALL_DONE --")
        lines = []
        stop = False
        try:
            while True:
                line = input()
                token = line.strip().upper()
                if token == "END":
                    break
                if token == "ALL_DONE":
                    stop = True
                    break
                lines.append(line)
        except EOFError:
            stop = True

        if any(s.strip() for s in lines):
            blocks.append("\n".join(lines))
            print(f"   saved race {race_no}.")
            race_no += 1
        if stop:
            break

    return "\n\n".join(blocks)


def fetch_live_cards(region: str = "ALL",
                     tracker: "OddsHistoryTracker | None" = None,
                     raw_text: "str | None" = None) -> dict:
    """
    Parse one or more pasted race cards (prompting for them if raw_text is None),
    guard against corrupt input, compute each race's overround and Shin fair
    probabilities, flag steamers/drifters, keep only races inside the rolling
    time gate, and return one combined payload:

        {region: {track_key: {'race_info','runners','book','fair_probs','movement'}}}

    The 'region' argument is kept for signature compatibility; each pasted card
    carries its own region.
    """
    tracker = tracker or OddsHistoryTracker()
    raw = raw_text if raw_text is not None else read_pasted_text()
    if not raw.strip():
        log.warning("No text pasted; nothing to analyse.")
        return {}

    parsed = parse_card_text(raw)
    if not parsed:
        log.warning("Could not parse any runners from the pasted text.")
        return {}

    now = datetime.now(timezone.utc)
    three_hours_hence = now + timedelta(minutes=TIME_GATE_MINUTES)
    log.info("Time gate: keeping races from %s to %s UTC (next %d min).",
             now.strftime("%H:%M"), three_hours_hence.strftime("%H:%M"), TIME_GATE_MINUTES)

    output = {}
    for reg, track_key, card in parsed:
        # --- UPSTREAM INPUT GUARDS ---  protect the history logs from bad data.
        hard, soft = validate_runners(card["runners"])
        if hard:
            print("\n" + "!" * 60)
            print(f"  DATA INTEGRITY HALT  |  {track_key}")
            for p in hard:
                print(f"   - {p}")
            print("  This card is NOT processed or logged. Fix the paste and re-run.")
            print("!" * 60)
            log.error("Dropping corrupt card %s: %s", track_key, "; ".join(hard))
            continue
        for p in soft:
            print(f"  [note] {track_key}: {p}")

        # --- THE ROLLING TIME GATE ---  now <= race_time <= three_hours_hence
        race_time = _parse_post_time(card["race_info"])
        if race_time is not None and not (now <= race_time <= three_hours_hence):
            log.info("Skipping %s: outside the %d-minute window.", track_key, TIME_GATE_MINUTES)
            continue
        if race_time is None:
            log.warning("Keeping %s: no readable post time to gate on.", track_key)

        # Real, locally-computed overround, and Shin-de-vigged fair probabilities.
        card["book"] = compute_book(card["runners"])
        fair_probs, shin_z = shin_devig(card["runners"])
        card["book"]["shin_z"] = round(shin_z, 4)
        card["fair_probs"] = fair_probs

        # Record for cross-run history, then derive movement. Prefer the
        # immediate open-vs-now velocity from this paste; fall back to the
        # tracker's run-to-run comparison when no opening price was supplied.
        track_id = card["race_info"].get("race_id") or track_key
        tracker.record(track_id, card["runners"])
        movement = compute_movement_from_open(card["runners"])
        if not movement:
            movement = tracker.movement(track_id)
        card["movement"] = movement

        output.setdefault(reg, {})[track_key] = card

    return output


if __name__ == "__main__":
    import pprint
    pprint.pprint(fetch_live_cards("ALL"))
