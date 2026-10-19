from datetime import datetime, timezone

from conftest import kalshi_markets

from whalecopy.matcher import Matcher, index_row_from_market, numbers, sports_match


def matcher(**kw):
    rows = [r for r in (index_row_from_market(m) for m in kalshi_markets()) if r]
    return Matcher(rows, **kw)


GAME_END = datetime(2026, 10, 20, 2, 0, tzinfo=timezone.utc)
FED_END = datetime(2026, 10, 28, tzinfo=timezone.utc)


def test_combos_are_excluded():
    assert index_row_from_market({"ticker": "KXMVESPORTS-1", "market_type": "binary"}) is None


def test_numbers():
    hard, years = numbers("Will BTC be above $120k on Dec 31, 2026?")
    assert "120000" in hard and "31" in hard and years == {"2026"}


T = "Lakers vs. Celtics"


def test_sports_slug_maps_to_that_teams_yes():
    rows = matcher().rows
    r = sports_match(rows, "nba-lal-bos-2026-10-19", 1, T, "Celtics")
    assert r.ticker == "KXNBAGAME-26OCT19LALBOS-BOS" and r.side == "yes"
    assert sports_match(rows, "nba-lal-bos-2026-10-19", 0, T, "Lakers").ticker.endswith("-LAL")
    assert sports_match(rows, "nba-lal-bos-2026-10-20", 0, T, "Lakers")  # +-1 day for time zones
    assert sports_match(rows, "nba-lal-bos-2026-10-25", 0, T, "Lakers") is None
    assert sports_match(rows, "nba-lal-bos-2026-10-19-spread-home-5pt5", 0, T, "Lakers") is None


def test_sports_guards():
    rows = matcher().rows
    slug = "nba-lal-bos-2026-10-19"
    assert sports_match(rows, slug, 0, "Lakers vs. Celtics: O/U 220.5", "Over") is None
    assert sports_match(rows, slug, 0, "Will Lakers win?", "Yes") is None
    assert sports_match(rows, slug, 0, T, "Celtics") is None   # outcome order disagrees
    assert sports_match(rows, "nfl-lal-bos-2026-10-19", 0, T, "Lakers") is None  # wrong league


def test_direction_and_negation_never_auto_match():
    rows = [
        {"ticker": "A", "title": "Will Bitcoin be below $120,000 on Dec 31, 2026?",
         "yes_sub_title": "", "expiration_ts": int(datetime(2026, 12, 31, tzinfo=timezone.utc).timestamp())},
        {"ticker": "B", "title": "Will Trump not be impeached in 2026?", "yes_sub_title": "",
         "expiration_ts": int(datetime(2026, 12, 31, tzinfo=timezone.utc).timestamp())},
    ]
    m = Matcher(rows, auto_approve_score=60)
    end = datetime(2026, 12, 31, tzinfo=timezone.utc)
    assert not m.match("Will Bitcoin be above $120,000 on Dec 31, 2026?", "Yes", end).auto_ok
    assert not m.match("Will Trump be impeached in 2026?", "Yes", end).auto_ok


def test_number_edge_cases_never_auto_match():
    end = datetime(2026, 12, 31, tzinfo=timezone.utc)
    ts = int(end.timestamp())
    m = Matcher([{"ticker": "A", "title": "Will the Fed cut 50bps in December 2026?",
                  "yes_sub_title": "", "expiration_ts": ts},
                 {"ticker": "B", "title": "Will ETH be above $2,100 on Dec 31, 2026?",
                  "yes_sub_title": "", "expiration_ts": ts}], auto_approve_score=60)
    assert not m.match("Will the Fed cut 25bps in December 2026?", "Yes", end).auto_ok
    assert not m.match("Will ETH be above $2,000 on Dec 31, 2026?", "Yes", end).auto_ok


def test_team_nicknames_never_auto_match_by_text():
    # Kalshi names cities, Polymarket names nicknames: fuzzy text must not auto-approve this.
    r = matcher().match("Lakers vs. Celtics", "Celtics", GAME_END)
    assert not r.auto_ok


def test_yes_no_question_and_numbers_guard():
    m = matcher(auto_approve_score=80)
    r = m.match("Will the Fed cut rates by 50 bps at the October 2026 meeting?", "No", FED_END)
    assert r.ticker == "KXFEDDECISION-26OCT-C50" and r.side == "no"
    assert r.auto_ok
    # 25 vs 50 bps must never auto-match
    bad = [c for c in r.candidates if c.ticker.endswith("C25")][0]
    assert any("numbers differ" in n for n in bad.notes)


def test_date_mismatch_blocks_auto():
    m = matcher(auto_approve_score=60)
    far = datetime(2027, 3, 1, tzinfo=timezone.utc)
    r = m.match("Will the Fed cut rates by 50 bps at the October 2026 meeting?", "Yes", far)
    assert not r.auto_ok


def test_unknown_date_blocks_auto():
    m = matcher(auto_approve_score=60)
    r = m.match("Will the Fed cut rates by 50 bps at the October 2026 meeting?", "Yes", None)
    assert not r.auto_ok and "date unknown" in r.reason


def test_unrelated_question_finds_nothing():
    r = matcher().match("Will Taylor Swift release a new album in 2026?", "Yes", FED_END)
    assert r.ticker is None
