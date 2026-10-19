"""Read-only Polymarket clients: Data API (leaderboard, wallet activity) and Gamma (market info).

Docs: https://docs.polymarket.com
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"


@dataclass
class WhaleTrade:
    """One (possibly aggregated) buy or sell by a followed wallet in one outcome."""
    wallet: str
    condition_id: str
    outcome_index: int
    outcome: str
    title: str
    slug: str
    event_slug: str
    side: str            # BUY | SELL
    price: float         # volume-weighted average price
    usdc: float
    shares: float
    timestamp: int       # latest fill time (epoch seconds)
    keys: tuple[str, ...]  # dedupe keys of the underlying fills

    @property
    def is_yes_no(self) -> bool:
        return self.outcome.strip().lower() in ("yes", "no")


def activity_key(row: dict) -> str:
    return "|".join(str(row.get(k, "")) for k in
                    ("transactionHash", "asset", "side", "size", "price", "timestamp"))


def aggregate_trades(wallet: str, rows: list[dict]) -> list[WhaleTrade]:
    """Whales often fill one order in many pieces; group by market+outcome+side."""
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        if r.get("type") != "TRADE" or r.get("isCombo"):
            continue
        if r.get("outcomeIndex") in (None, 999) or not r.get("conditionId"):
            continue
        k = (r["conditionId"], int(r["outcomeIndex"]), r.get("side"))
        groups.setdefault(k, []).append(r)
    out = []
    for (cid, idx, side), rs in groups.items():
        usdc = sum(float(r.get("usdcSize") or 0) for r in rs)
        shares = sum(float(r.get("size") or 0) for r in rs)
        if shares <= 0:
            continue
        first = rs[0]
        out.append(WhaleTrade(
            wallet=wallet, condition_id=cid, outcome_index=idx,
            outcome=first.get("outcome") or "", title=first.get("title") or "",
            slug=first.get("slug") or "", event_slug=first.get("eventSlug") or "",
            side=side, price=usdc / shares, usdc=usdc, shares=shares,
            timestamp=max(int(r.get("timestamp") or 0) for r in rs),
            keys=tuple(activity_key(r) for r in rs),
        ))
    return sorted(out, key=lambda t: t.timestamp)


class PolymarketClient:
    def __init__(self, session: requests.Session | None = None, timeout: float = 15.0):
        self.session = session or requests.Session()
        self.timeout = timeout

    def _get(self, url: str, params: dict, retries: int = 3):
        for attempt in range(retries + 1):
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.HTTPError(f"{r.status_code}")
                r.raise_for_status()
                return r.json()
            except requests.RequestException as e:
                if attempt == retries:
                    raise
                log.debug("retrying %s after %s", url, e)
                time.sleep(1.5 * (attempt + 1))

    def leaderboard(self, category: str = "OVERALL", time_period: str = "MONTH",
                    order_by: str = "PNL", limit: int = 50, offset: int = 0) -> list[dict]:
        return self._get(f"{DATA_API}/v1/leaderboard", {
            "category": category, "timePeriod": time_period, "orderBy": order_by,
            "limit": limit, "offset": offset,
        })

    def activity(self, wallet: str, start: int | None = None, limit: int = 500,
                 ascending: bool = True) -> list[dict]:
        params = {"user": wallet, "type": "TRADE", "limit": limit,
                  "sortDirection": "ASC" if ascending else "DESC"}
        if start:
            params["start"] = start
        return self._get(f"{DATA_API}/activity", params)

    def market_info(self, condition_id: str) -> dict | None:
        """Question, outcomes and end date for one market (None if not found)."""
        try:
            data = self._get(f"{GAMMA_API}/markets", {"condition_ids": condition_id})
        except requests.RequestException as e:
            log.warning("gamma lookup failed for %s: %s", condition_id, e)
            return None
        if not data:
            return None
        m = data[0]
        outcomes = m.get("outcomes")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except ValueError:
                outcomes = []
        return {
            "question": m.get("question") or "",
            "outcomes": outcomes or [],
            "end": parse_iso(m.get("endDate") or m.get("endDateIso")),
            "closed": bool(m.get("closed")),
        }


def parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
