"""SQLite state: wallet cursors, seen fills, Kalshi market index, mappings, positions, event log."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, ts INTEGER);
CREATE TABLE IF NOT EXISTS kalshi_markets (
    ticker TEXT PRIMARY KEY, event_ticker TEXT, title TEXT, yes_sub_title TEXT,
    no_sub_title TEXT, expiration_ts INTEGER, status TEXT, refreshed_at INTEGER);
CREATE TABLE IF NOT EXISTS mappings (
    condition_id TEXT, outcome_index INTEGER, kalshi_ticker TEXT, kalshi_side TEXT,
    score REAL, status TEXT, poly_text TEXT, kalshi_text TEXT, candidates TEXT,
    created_at INTEGER, PRIMARY KEY (condition_id, outcome_index));
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT, kalshi_ticker TEXT, side TEXT,
    count INTEGER, cost_usd REAL, opened_at INTEGER, closed_at INTEGER,
    close_value_usd REAL, pnl_usd REAL, status TEXT, source_wallet TEXT,
    condition_id TEXT, outcome_index INTEGER);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, wallet TEXT,
    condition_id TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_pos_status ON positions(status, mode);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


def day_start(now: float | None = None) -> int:
    """Start of the current UTC day, as epoch seconds (daily limits reset here)."""
    now = time.time() if now is None else now
    return int(now // 86400 * 86400)


class Store:
    def __init__(self, path: str | Path, clock=time.time, readonly: bool = False):
        self.clock = clock
        path = Path(path)
        if readonly:  # the dashboard: can't write, even by mistake
            self.db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True,
                                      timeout=10, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            return
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # -- kv ------------------------------------------------------------------
    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))
        self.db.commit()

    # -- seen fills -------------------------------------------------------------
    def is_seen(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM seen WHERE key=?", (key,)).fetchone() is not None

    def mark_seen(self, keys) -> None:
        now = int(self.clock())
        self.db.executemany("INSERT OR IGNORE INTO seen VALUES (?,?)", [(k, now) for k in keys])
        self.db.commit()

    def prune_seen(self, older_than_sec: int = 7 * 86400) -> None:
        self.db.execute("DELETE FROM seen WHERE ts < ?", (int(self.clock()) - older_than_sec,))
        self.db.commit()

    # -- kalshi index -------------------------------------------------------------
    def replace_kalshi_index(self, rows: list[dict]) -> None:
        now = int(self.clock())
        with self.db:
            self.db.execute("DELETE FROM kalshi_markets")
            self.db.executemany(
                "INSERT OR REPLACE INTO kalshi_markets VALUES (?,?,?,?,?,?,?,?)",
                [(r["ticker"], r.get("event_ticker"), r.get("title"), r.get("yes_sub_title"),
                  r.get("no_sub_title"), r.get("expiration_ts"), r.get("status"), now)
                 for r in rows])

    def kalshi_index(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM kalshi_markets")]

    def kalshi_market(self, ticker: str) -> dict | None:
        r = self.db.execute("SELECT * FROM kalshi_markets WHERE ticker=?", (ticker,)).fetchone()
        return dict(r) if r else None

    # -- mappings ------------------------------------------------------------------
    def get_mapping(self, condition_id: str, outcome_index: int) -> dict | None:
        r = self.db.execute("SELECT * FROM mappings WHERE condition_id=? AND outcome_index=?",
                            (condition_id, outcome_index)).fetchone()
        return dict(r) if r else None

    def save_mapping(self, condition_id, outcome_index, ticker, side, score, status,
                     poly_text="", kalshi_text="", candidates=None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO mappings VALUES (?,?,?,?,?,?,?,?,?,?)",
            (condition_id, outcome_index, ticker, side, score, status, poly_text, kalshi_text,
             json.dumps(candidates or []), int(self.clock())))
        self.db.commit()

    def set_mapping_status(self, condition_id, outcome_index, status, ticker=None, side=None):
        m = self.get_mapping(condition_id, outcome_index)
        if not m:
            raise KeyError("no such mapping")
        self.save_mapping(condition_id, outcome_index, ticker or m["kalshi_ticker"],
                          side or m["kalshi_side"], m["score"], status, m["poly_text"],
                          m["kalshi_text"], json.loads(m["candidates"] or "[]"))

    def mappings(self, status: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM mappings", ()
        if status:
            q, args = q + " WHERE status=?", (status,)
        return [dict(r) for r in self.db.execute(q + " ORDER BY created_at DESC", args)]

    # -- positions --------------------------------------------------------------------
    def open_position(self, mode, ticker, side, count, cost, wallet, condition_id,
                      outcome_index) -> int:
        cur = self.db.execute(
            "INSERT INTO positions (mode,kalshi_ticker,side,count,cost_usd,opened_at,status,"
            "source_wallet,condition_id,outcome_index) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (mode, ticker, side, count, cost, int(self.clock()), "open", wallet, condition_id,
             outcome_index))
        self.db.commit()
        return cur.lastrowid

    def close_position(self, pos_id: int, value_usd: float, status: str = "closed") -> None:
        row = self.db.execute("SELECT cost_usd FROM positions WHERE id=?", (pos_id,)).fetchone()
        self.db.execute(
            "UPDATE positions SET status=?, closed_at=?, close_value_usd=?, pnl_usd=? WHERE id=?",
            (status, int(self.clock()), value_usd, value_usd - row["cost_usd"], pos_id))
        self.db.commit()

    def reduce_position(self, pos_id: int, sold: int, value_usd: float) -> None:
        """Partial sell: split the row into a closed part and a smaller open part."""
        p = dict(self.db.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone())
        frac = sold / p["count"]
        sold_cost = p["cost_usd"] * frac
        with self.db:
            self.db.execute("UPDATE positions SET count=?, cost_usd=? WHERE id=?",
                            (p["count"] - sold, p["cost_usd"] - sold_cost, pos_id))
            self.db.execute(
                "INSERT INTO positions (mode,kalshi_ticker,side,count,cost_usd,opened_at,"
                "closed_at,close_value_usd,pnl_usd,status,source_wallet,condition_id,"
                "outcome_index) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["mode"], p["kalshi_ticker"], p["side"], sold, sold_cost, p["opened_at"],
                 int(self.clock()), value_usd, value_usd - sold_cost, "closed",
                 p["source_wallet"], p["condition_id"], p["outcome_index"]))

    def open_positions(self, mode: str, ticker: str | None = None) -> list[dict]:
        q = "SELECT * FROM positions WHERE status='open' AND mode=?"
        args: tuple = (mode,)
        if ticker:
            q, args = q + " AND kalshi_ticker=?", (mode, ticker)
        return [dict(r) for r in self.db.execute(q, args)]

    def open_exposure(self, mode: str, ticker: str | None = None) -> float:
        return sum(p["cost_usd"] for p in self.open_positions(mode, ticker))

    def spent_since(self, mode: str, since: int) -> float:
        r = self.db.execute(
            "SELECT COALESCE(SUM(cost_usd),0) s FROM positions WHERE mode=? AND opened_at>=?",
            (mode, since)).fetchone()
        return float(r["s"])

    def copies_since(self, mode: str, since: int) -> int:
        r = self.db.execute("SELECT COUNT(*) c FROM events WHERE kind='copied' AND ts>=? "
                            "AND json_extract(detail,'$.mode')=?", (since, mode)).fetchone()
        return int(r["c"])

    def last_copy_ts(self, wallet: str, condition_id: str, mode: str) -> int | None:
        r = self.db.execute("SELECT MAX(ts) t FROM events WHERE kind='copied' AND wallet=? "
                            "AND condition_id=? AND json_extract(detail,'$.mode')=?",
                            (wallet, condition_id, mode)).fetchone()
        return r["t"]

    def all_positions(self, mode: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM positions WHERE mode=? ORDER BY opened_at", (mode,))]

    # -- event log -----------------------------------------------------------------------
    def log_event(self, kind: str, wallet: str = "", condition_id: str = "", **detail) -> None:
        self.db.execute("INSERT INTO events (ts,kind,wallet,condition_id,detail) VALUES (?,?,?,?,?)",
                        (int(self.clock()), kind, wallet, condition_id, json.dumps(detail)))
        self.db.commit()

    def copy_titles(self, mode: str) -> dict[tuple[str, str], str]:
        """(condition_id, ticker) -> "whale market title / outcome", from copy events."""
        out = {}
        for r in self.db.execute(
                "SELECT condition_id, json_extract(detail,'$.ticker') t, "
                "json_extract(detail,'$.title') title, json_extract(detail,'$.outcome') o "
                "FROM events WHERE kind='copied' AND json_extract(detail,'$.mode')=? ORDER BY ts",
                (mode,)):
            if r["title"]:
                out[(r["condition_id"], r["t"])] = f"{r['title']} / {r['o'] or ''}".rstrip(" /")
        return out

    def events_since(self, since: int) -> list[dict]:
        return [dict(r) | {"detail": json.loads(r["detail"] or "{}")} for r in self.db.execute(
            "SELECT * FROM events WHERE ts>=? ORDER BY ts", (since,))]
