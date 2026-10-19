"""Find the Kalshi market (ticker + side) that means the same thing as a Polymarket outcome.

This is the riskiest part of the bot: a wrong match turns a copied bet into a different
bet. So the matcher is deliberately conservative. Only high-confidence, unambiguous matches
with agreeing dates and numbers are auto-approved; everything else goes to a review queue
(`whalecopy review`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from rapidfuzz import fuzz, process

from .polymarket import parse_iso

_NUM = re.compile(
    r"(\$)?(\d[\d,]*(?:\.\d+)?)\s*(k|m|bn|b|%|bps|bp|pts|pt|c)?(?![a-z0-9])", re.I)
_POLARITY = [
    ("UP", r"\b(above|over|more than|higher|greater|exceeds?|at least|or more|rises?|up)\b"),
    ("DOWN", r"\b(below|under|less than|lower|fewer|at most|or less|falls?|drops?|down)\b"),
    ("BEFORE", r"\bbefore\b"),
    ("AFTER", r"\bafter\b"),
    ("NEG", r"\b(not|no|never|without)\b|n't\b"),
]
_STOP = {"will", "the", "a", "an", "be", "of", "on", "in", "by", "to", "at", "for", "?"}


def normalize(text: str) -> str:
    t = (text or "").lower()
    t = t.replace("&", " and ").replace("vs.", "vs").replace(" v. ", " vs ")
    t = re.sub(r"[^a-z0-9.%$ ]+", " ", t)
    words = [w.strip(".") for w in t.split()]
    return " ".join(w for w in words if w and w not in _STOP)


def numbers(text: str) -> tuple[set[str], set[str]]:
    """Returns (hard_numbers, years). Years may be omitted on one side; other numbers may not."""
    hard, years = set(), set()
    for m in _NUM.finditer(text or ""):
        dollar, raw, suffix = m.group(1), m.group(2), (m.group(3) or "").lower()
        try:
            v = float(raw.replace(",", ""))
        except ValueError:
            continue
        if suffix == "k":
            v *= 1_000
        elif suffix == "m":
            v *= 1_000_000
        elif suffix in ("b", "bn"):
            v *= 1_000_000_000
        if (not dollar and not suffix and "," not in raw and v.is_integer()
                and 1990 <= v <= 2100):
            years.add(str(int(v)))
            continue
        hard.add(f"{v:g}")
    return hard, years


def polarity(text: str) -> set[str]:
    """Direction/negation words. Two questions that differ here mean different things."""
    t = (text or "").lower()
    return {name for name, pat in _POLARITY if re.search(pat, t)}


def kalshi_text(row: dict) -> str:
    parts = [row.get("title") or ""]
    sub = row.get("yes_sub_title") or ""
    if sub and sub.lower() not in parts[0].lower():
        parts.append(sub)
    return " ".join(parts)


def index_row_from_market(m: dict) -> dict | None:
    """Turn a Kalshi /markets row into a compact index row (None = skip this market)."""
    ticker = m.get("ticker") or ""
    if not ticker or ticker.startswith("KXMVE") or m.get("mve_collection_ticker"):
        return None  # multi-leg combos never map to a single Polymarket outcome
    if m.get("market_type") not in (None, "binary"):
        return None
    exp = parse_iso(m.get("expected_expiration_time") or m.get("close_time"))
    return {
        "ticker": ticker, "event_ticker": m.get("event_ticker"), "title": m.get("title") or "",
        "yes_sub_title": m.get("yes_sub_title") or "", "no_sub_title": m.get("no_sub_title") or "",
        "expiration_ts": int(exp.timestamp()) if exp else None, "status": m.get("status"),
    }


@dataclass
class Candidate:
    ticker: str
    text: str
    score: float
    notes: list[str] = field(default_factory=list)


@dataclass
class MatchResult:
    ticker: str | None
    side: str | None
    score: float
    auto_ok: bool
    poly_text: str
    candidates: list[Candidate]
    reason: str = ""


class Matcher:
    def __init__(self, index_rows: list[dict], auto_approve_score: float = 92.0,
                 review_min_score: float = 70.0, date_tolerance_days: float = 3.0):
        self.rows = index_rows
        self.texts = [kalshi_text(r) for r in index_rows]
        self.norm = [normalize(t) for t in self.texts]
        self.auto_score = auto_approve_score
        self.review_min = review_min_score
        self.tol_sec = date_tolerance_days * 86400

    def match(self, question: str, outcome: str, end: datetime | None = None) -> MatchResult:
        is_yes_no = outcome.strip().lower() in ("yes", "no")
        side = ("yes" if outcome.strip().lower() == "yes" else "no") if is_yes_no else "yes"
        poly_text = question if is_yes_no else f"{question} {outcome}"
        if not self.rows:
            return MatchResult(None, None, 0, False, poly_text, [], "kalshi index is empty")

        query = normalize(poly_text)
        hits = process.extract(query, self.norm, scorer=fuzz.token_set_ratio, limit=25)
        p_hard, p_years = numbers(poly_text)
        cands: list[tuple[Candidate, bool]] = []
        for _, _, i in hits:
            row, text = self.rows[i], self.texts[i]
            score = 0.5 * fuzz.token_set_ratio(query, self.norm[i]) + \
                0.5 * fuzz.token_sort_ratio(query, self.norm[i])
            notes, hard_ok = [], True

            k_hard, k_years = numbers(text)
            if p_hard != k_hard:
                diff = sorted(p_hard ^ k_hard)
                if p_hard - k_hard:   # a number in the question that Kalshi lacks: real mismatch
                    score -= 30
                hard_ok = False
                notes.append(f"numbers differ {diff}")
            if polarity(poly_text) != polarity(text):
                score -= 30
                hard_ok = False
                notes.append("direction/negation words differ")
            if p_years and k_years and not (p_years & k_years):
                score -= 30
                hard_ok = False
                notes.append("years differ")

            exp = row.get("expiration_ts")
            if end is not None and exp:
                gap = abs(end.timestamp() - exp)
                if gap > self.tol_sec:
                    score -= 25
                    hard_ok = False
                    notes.append(f"dates {gap / 86400:.0f}d apart")
            else:
                hard_ok = False
                notes.append("date unknown")

            if not is_yes_no:
                sub = row.get("yes_sub_title") or row.get("title") or ""
                if fuzz.partial_ratio(normalize(outcome), normalize(sub)) < 85:
                    score -= 20
                    hard_ok = False
                    notes.append(f"outcome '{outcome}' not the YES side")
            cands.append((Candidate(row["ticker"], text, round(score, 1), notes), hard_ok))

        cands.sort(key=lambda c: c[0].score, reverse=True)
        best, best_ok = cands[0]
        runner_up = cands[1][0].score if len(cands) > 1 else 0
        top = [c for c, _ in cands[:5]]
        if best.score < self.review_min:
            return MatchResult(None, None, best.score, False, poly_text, top, "no close match")
        auto_ok = best_ok and best.score >= self.auto_score and best.score - runner_up >= 4
        reason = "" if auto_ok else (
            "; ".join(best.notes) or
            ("ambiguous" if best.score - runner_up < 4 else "score below auto threshold"))
        return MatchResult(best.ticker, side, best.score, auto_ok, poly_text, top, reason)


_SPORTS_SLUG = re.compile(r"^([a-z0-9]+)-([a-z]{2,4})-([a-z]{2,4})-(\d{4})-(\d{2})-(\d{2})$")
_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


_GENERIC_OUTCOMES = {"yes", "no", "over", "under", "draw", "tie", ""}


def sports_match(rows: list[dict], slug: str, outcome_index: int, title: str = "",
                 outcome: str = "") -> MatchResult | None:
    """Moneyline games: Polymarket slug 'nba-lal-bos-2026-10-19' -> Kalshi 'KX...GAME-26OCT19LALBOS-LAL'.

    Assumes Polymarket's outcome order follows the team order in the slug (team 1 = outcome 0).
    That assumption is why these matches are only used live if you opt in (sports_auto_approve)
    after checking them in paper mode with `whalecopy map list`.
    """
    m = _SPORTS_SLUG.match(slug or "")
    if not m or outcome_index not in (0, 1) or outcome.strip().lower() in _GENERIC_OUTCOMES:
        return None
    # The outcome must be the team named in that position of "A vs. B" in the title.
    parts = re.split(r"\s+vs\.?\s+", title or "", flags=re.I)
    if len(parts) != 2 or fuzz.partial_ratio(outcome.lower(), parts[outcome_index].lower()) < 80:
        return None
    league, a, b, year, month, day = m.groups()
    team = (a, b)[outcome_index].upper()
    base = datetime(int(year), int(month), int(day))
    keys = set()
    for delta in (-1, 0, 1):
        d = datetime.fromordinal(base.toordinal() + delta)
        stamp = f"{d.year % 100:02d}{_MONTHS[d.month - 1]}{d.day:02d}"
        keys |= {f"{stamp}{a.upper()}{b.upper()}", f"{stamp}{b.upper()}{a.upper()}"}
    hits = []
    for r in rows:
        series, _, ev = (r.get("event_ticker") or "").partition("-")
        if (series.endswith("GAME") and league.upper() in series and ev in keys
                and r["ticker"] == f"{r['event_ticker']}-{team}"):
            hits.append(r)
    if len(hits) != 1:
        return None
    row = hits[0]
    cand = Candidate(row["ticker"], kalshi_text(row), 100.0, ["sports slug match"])
    return MatchResult(row["ticker"], "yes", 100.0, True, slug, [cand], "sports slug match")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
