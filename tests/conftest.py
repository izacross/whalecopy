import time

import pytest

from whalecopy.config import Settings
from whalecopy.kalshi import quote_from_market
from whalecopy.store import Store

NOW = 1_792_000_000  # fixed clock (Oct 2026)
WALLET = "0x" + "a" * 40
CID_GAME = "0x" + "1" * 64
CID_FED = "0x" + "2" * 64


def kalshi_markets():
    exp_game = "2026-10-20T03:00:00Z"
    exp_fed = "2026-10-28T18:00:00Z"
    return [
        {"ticker": "KXNBAGAME-26OCT19LALBOS-LAL", "event_ticker": "KXNBAGAME-26OCT19LALBOS",
         "title": "Los Angeles L vs Boston Winner?", "yes_sub_title": "Los Angeles L",
         "market_type": "binary", "status": "active", "expected_expiration_time": exp_game},
        {"ticker": "KXNBAGAME-26OCT19LALBOS-BOS", "event_ticker": "KXNBAGAME-26OCT19LALBOS",
         "title": "Los Angeles L vs Boston Winner?", "yes_sub_title": "Boston",
         "market_type": "binary", "status": "active", "expected_expiration_time": exp_game},
        {"ticker": "KXFEDDECISION-26OCT-C25", "event_ticker": "KXFEDDECISION-26OCT",
         "title": "Will the Fed cut rates by 25 bps at the October 2026 meeting?",
         "yes_sub_title": "Cut 25bps", "market_type": "binary", "status": "active",
         "expected_expiration_time": exp_fed},
        {"ticker": "KXFEDDECISION-26OCT-C50", "event_ticker": "KXFEDDECISION-26OCT",
         "title": "Will the Fed cut rates by 50 bps at the October 2026 meeting?",
         "yes_sub_title": "Cut 50bps", "market_type": "binary", "status": "active",
         "expected_expiration_time": exp_fed},
        {"ticker": "KXMVESPORTS-ABC", "title": "combo", "market_type": "binary",
         "status": "active", "mve_collection_ticker": "X"},
    ]


class FakePoly:
    def __init__(self):
        self.rows: list[dict] = []
        self.info: dict[str, dict] = {}
        self.fail = None

    def activity(self, wallet, start=None, limit=500, ascending=True, offset=0):
        if self.fail:
            raise self.fail
        rows = [r for r in self.rows if start is None or r["timestamp"] >= start]
        return rows[offset:offset + limit]

    def market_info(self, cid):
        return self.info.get(cid)

    def leaderboard(self, *a, **k):
        return []


class FakeKalshi:
    def __init__(self):
        self.markets = {m["ticker"]: dict(m) for m in kalshi_markets()}
        self.orders: list[dict] = []
        self.fill_ratio = 1.0
        self.order_error = None

    def set_price(self, ticker, yes_ask=None, no_ask=None, yes_bid=None, no_bid=None):
        m = self.markets[ticker]
        for k, v in (("yes_ask", yes_ask), ("no_ask", no_ask), ("yes_bid", yes_bid),
                     ("no_bid", no_bid)):
            if v is not None:
                m[f"{k}_dollars"] = f"{v:.4f}"

    def iter_open_markets(self):
        return iter(self.markets.values())

    def get_market(self, ticker):
        return self.markets[ticker]

    def get_quote(self, ticker):
        return quote_from_market(self.markets[ticker])

    def create_order(self, **kw):
        self.orders.append(kw)
        if self.order_error:
            raise self.order_error
        filled = int(kw["count"] * self.fill_ratio)
        return {"order_id": "o1", "status": "executed" if filled else "canceled",
                "fill_count": filled, "remaining_count": kw["count"] - filled,
                "initial_count": kw["count"],
                "taker_fill_cost_dollars": f"{filled * kw['limit_price']:.4f}",
                "maker_fill_cost_dollars": "0.0000"}


def trade_row(cid=CID_GAME, outcome="Los Angeles Lakers", idx=0, side="BUY", price=0.55,
              usdc=2000.0, ts=NOW - 30, title="Lakers vs. Celtics", tx="0xt1"):
    return {"proxyWallet": WALLET, "timestamp": ts, "conditionId": cid, "type": "TRADE",
            "size": usdc / price, "usdcSize": usdc, "transactionHash": tx, "price": price,
            "asset": f"{cid}-{idx}", "side": side, "outcomeIndex": idx, "title": title,
            "slug": "s", "eventSlug": "e", "outcome": outcome}


@pytest.fixture
def settings():
    return Settings(wallets=[WALLET], data_dir=":memory:", index_refresh_hours=1e9)


@pytest.fixture
def store():
    return Store(":memory:", clock=lambda: NOW)
