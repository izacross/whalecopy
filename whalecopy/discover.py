"""Find Polymarket wallets worth following.

Pulls the leaderboard in categories Kalshi also lists, keeps traders who are profitable both
this month and all-time, then looks at each one's recent trades to drop wallets that are hard
to copy (market makers, near-certainty grinders, tiny trades).
"""
from __future__ import annotations

import csv
import logging
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import Settings
from .polymarket import PolymarketClient

log = logging.getLogger(__name__)

DEFAULT_CATEGORIES = ["SPORTS", "POLITICS", "ECONOMICS", "FINANCE", "CRYPTO", "WEATHER", "CULTURE"]


@dataclass
class WalletScore:
    wallet: str
    name: str
    categories: str
    pnl_month: float
    pnl_all: float
    vol_month: float
    trades_sampled: int
    trades_per_day: float
    median_trade_usd: float
    copyable_signals_per_week: float
    extreme_price_share: float
    sell_share: float
    verdict: str


def score_wallet(rows: list[dict], s: Settings, now: float | None = None) -> dict:
    """Stats from a wallet's recent TRADE rows (newest first)."""
    now = now or time.time()
    trades = [r for r in rows if r.get("type") == "TRADE"]
    if not trades:
        return {"trades_sampled": 0, "trades_per_day": 0, "median_trade_usd": 0,
                "copyable_signals_per_week": 0, "extreme_price_share": 0, "sell_share": 0}
    ts = [int(r.get("timestamp") or 0) for r in trades]
    span_days = max((now - min(ts)) / 86400, 1)
    usd = [float(r.get("usdcSize") or 0) for r in trades]
    prices = [float(r.get("price") or 0) for r in trades]
    copyable = [r for r in trades if r.get("side") == "BUY"
                and float(r.get("usdcSize") or 0) >= s.min_whale_trade_usd
                and s.min_price <= float(r.get("price") or 0) <= s.max_price]
    return {
        "trades_sampled": len(trades),
        "trades_per_day": round(len(trades) / span_days, 1),
        "median_trade_usd": round(statistics.median(usd), 2),
        "copyable_signals_per_week": round(len(copyable) / span_days * 7, 1),
        "extreme_price_share": round(sum(p > 0.95 or p < 0.05 for p in prices) / len(prices), 2),
        "sell_share": round(sum(r.get("side") == "SELL" for r in trades) / len(trades), 2),
    }


def verdict(stats: dict) -> str:
    if stats["trades_sampled"] == 0:
        return "skip: no recent trades"
    if stats["trades_per_day"] > 80:
        return "skip: looks like a bot/market maker"
    if stats["extreme_price_share"] > 0.5:
        return "skip: mostly near-certain bets"
    if stats["copyable_signals_per_week"] < 1:
        return "skip: too few copyable trades"
    if stats["copyable_signals_per_week"] > 60:
        return "caution: very frequent trader"
    return "good candidate"


def discover(poly: PolymarketClient, s: Settings, categories=None, top: int = 50,
             out_dir: str | None = None) -> list[WalletScore]:
    categories = categories or DEFAULT_CATEGORIES
    pool: dict[str, dict] = {}
    for cat in categories:
        try:
            month = poly.leaderboard(cat, "MONTH", "PNL", limit=top)
            alltime = poly.leaderboard(cat, "ALL", "PNL", limit=top)
        except Exception as e:
            log.warning("leaderboard %s failed: %s", cat, e)
            continue
        all_by_wallet = {r["proxyWallet"].lower(): r for r in alltime}
        for r in month:
            w = r["proxyWallet"].lower()
            a = all_by_wallet.get(w)
            if not a or float(r.get("pnl") or 0) <= 0 or float(a.get("pnl") or 0) <= 0:
                continue  # want traders who win this month AND all-time in this category
            e = pool.setdefault(w, {"name": r.get("userName") or "", "cats": [],
                                    "pnl_month": 0.0, "pnl_all": 0.0, "vol_month": 0.0})
            e["cats"].append(cat)
            e["pnl_month"] += float(r.get("pnl") or 0)
            e["pnl_all"] += float(a.get("pnl") or 0)
            e["vol_month"] += float(r.get("vol") or 0)
        time.sleep(0.2)

    results = []
    for w, e in pool.items():
        try:
            rows = poly.activity(w, limit=500, ascending=False)
        except Exception as err:
            log.warning("activity for %s failed: %s", w, err)
            continue
        stats = score_wallet(rows, s)
        results.append(WalletScore(
            wallet=w, name=e["name"], categories=",".join(e["cats"]),
            pnl_month=round(e["pnl_month"], 2), pnl_all=round(e["pnl_all"], 2),
            vol_month=round(e["vol_month"], 2), verdict=verdict(stats), **stats))
        time.sleep(0.2)

    results.sort(key=lambda r: (r.verdict != "good candidate", -r.pnl_month))
    if out_dir:
        path = Path(out_dir) / "candidates.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=list(asdict(results[0]).keys()) if results else ["wallet"])
            wr.writeheader()
            for r in results:
                wr.writerow(asdict(r))
        log.info("wrote %s", path)
    return results
