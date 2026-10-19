"""The copy loop: watch wallets -> map to Kalshi -> check price and risk -> trade (paper or live)."""
from __future__ import annotations

import logging
import math
import time
import uuid

from .config import Settings
from .kalshi import KalshiClient, KalshiError, fill_cost_usd, filled_count
from .matcher import Matcher, index_row_from_market, sports_match
from .notify import Notifier
from .polymarket import PolymarketClient, WhaleTrade, activity_key, aggregate_trades
from .store import Store, day_start

log = logging.getLogger(__name__)

SETTLED_STATUSES = {"settled", "determined", "finalized"}


def size_for(usdc: float, s: Settings) -> float:
    """Scale between min and max trade size by how big the whale's buy was."""
    if usdc >= s.big_whale_trade_usd:
        return s.max_trade_usd
    span = max(s.big_whale_trade_usd - s.min_whale_trade_usd, 1e-9)
    frac = max(0.0, (usdc - s.min_whale_trade_usd) / span)
    return round(s.min_trade_usd + frac * (s.max_trade_usd - s.min_trade_usd), 2)


class CopyEngine:
    def __init__(self, settings: Settings, store: Store, poly: PolymarketClient,
                 kalshi: KalshiClient, notifier: Notifier | None = None, clock=time.time):
        self.s = settings
        self.store = store
        self.poly = poly
        self.kalshi = kalshi
        self.notify = notifier or Notifier()
        self.clock = clock
        self._matcher: Matcher | None = None

    @property
    def mode(self) -> str:
        return self.s.mode

    # -- Kalshi market index -------------------------------------------------------
    def refresh_index(self, force: bool = False) -> int:
        last = self.store.get("index_refreshed_at", 0)
        if not force and self.clock() - last < self.s.index_refresh_hours * 3600:
            return 0
        log.info("refreshing Kalshi market index...")
        rows = [r for r in (index_row_from_market(m) for m in self.kalshi.iter_open_markets()) if r]
        if not rows:
            log.warning("Kalshi returned no open markets; keeping the old index")
            return 0
        self.store.replace_kalshi_index(rows)
        self.store.set("index_refreshed_at", self.clock())
        self._matcher = None
        log.info("indexed %d Kalshi markets", len(rows))
        return len(rows)

    @property
    def matcher(self) -> Matcher:
        if self._matcher is None:
            self._matcher = Matcher(self.store.kalshi_index(), self.s.auto_approve_score,
                                    self.s.review_min_score, self.s.date_tolerance_days)
        return self._matcher

    # -- watching wallets ------------------------------------------------------------
    def poll_wallet(self, wallet: str) -> list[WhaleTrade]:
        key = f"cursor:{wallet}"
        cursor = self.store.get(key)
        if cursor is None:
            # First time we see this wallet: start from now, never copy old history.
            self.store.set(key, int(self.clock()))
            log.info("now watching %s", wallet)
            return []
        rows = self.poly.activity(wallet, start=cursor)
        fresh = [r for r in rows if not self.store.is_seen(activity_key(r))]
        if not fresh:
            return []
        self.store.mark_seen(activity_key(r) for r in fresh)
        self.store.set(key, max(int(r.get("timestamp") or cursor) for r in fresh))
        return aggregate_trades(wallet, fresh)

    # -- mapping -------------------------------------------------------------------------
    def resolve(self, t: WhaleTrade) -> tuple[str | None, str | None, str]:
        m = self.store.get_mapping(t.condition_id, t.outcome_index)
        index_time = self.store.get("index_refreshed_at", 0)
        if m and not (m["status"] == "nomatch" and m["created_at"] < index_time):
            return self._usable(m["kalshi_ticker"], m["kalshi_side"], m["status"])

        for slug in (t.slug, t.event_slug):
            sm = sports_match(self.matcher.rows, slug, t.outcome_index)
            if sm:
                self.store.save_mapping(t.condition_id, t.outcome_index, sm.ticker, sm.side,
                                        sm.score, "sports", f"{t.title} / {t.outcome}",
                                        sm.candidates[0].text, [c.__dict__ for c in sm.candidates])
                return self._usable(sm.ticker, sm.side, "sports")

        info = self.poly.market_info(t.condition_id) or {}
        question = info.get("question") or t.title
        res = self.matcher.match(question, t.outcome, info.get("end"))
        cands = [c.__dict__ for c in res.candidates]
        if res.ticker is None:
            self.store.save_mapping(t.condition_id, t.outcome_index, None, None, res.score,
                                    "nomatch", res.poly_text, "", cands)
            return None, None, "no matching Kalshi market"
        status = "auto" if res.auto_ok else "pending"
        self.store.save_mapping(t.condition_id, t.outcome_index, res.ticker, res.side, res.score,
                                status, res.poly_text, res.candidates[0].text, cands)
        if status == "pending":
            self.notify.send(
                f"Review needed: '{res.poly_text}' -> {res.ticker} ({res.side.upper()}), "
                f"score {res.score:.0f}: {res.reason}. Run `whalecopy review`.")
            return None, None, "match awaiting your review"
        return res.ticker, res.side, ""

    def _usable(self, ticker, side, status) -> tuple[str | None, str | None, str]:
        if status in ("auto", "approved"):
            return ticker, side, ""
        if status == "sports":
            if not self.s.is_live or self.s.sports_auto_approve:
                return ticker, side, ""
            return None, None, "sports match not enabled for live (sports_auto_approve)"
        return None, None, {"pending": "match awaiting your review",
                            "rejected": "match rejected",
                            "nomatch": "no matching Kalshi market"}.get(status, status)

    # -- main loop body ---------------------------------------------------------------------
    def tick(self) -> None:
        self.refresh_index()
        for wallet in self.s.wallets:
            try:
                trades = self.poll_wallet(wallet)
            except Exception as e:  # one bad wallet shouldn't stop the others
                log.warning("polling %s failed: %s", wallet, e)
                continue
            for t in trades:
                try:
                    self.handle(t)
                except KalshiError as e:
                    log.error("Kalshi error on %s: %s", t.title, e)
                    self.store.log_event("error", t.wallet, t.condition_id, error=str(e),
                                         title=t.title, mode=self.mode)
        if self.clock() - self.store.get("settled_at", 0) > 3600:
            self.settle()
            self.store.set("settled_at", self.clock())
        if self.clock() - self.store.get("pruned_at", 0) > 86400:
            self.store.prune_seen()
            self.store.set("pruned_at", self.clock())

    def skip(self, t: WhaleTrade, reason: str, **extra) -> None:
        log.info("skip [%s] %s %s @%.2f ($%.0f): %s", t.wallet[:8], t.side, t.title,
                 t.price, t.usdc, reason)
        self.store.log_event("skipped", t.wallet, t.condition_id, reason=reason, title=t.title,
                             outcome=t.outcome, side=t.side, usdc=t.usdc, price=t.price,
                             mode=self.mode, **extra)

    def handle(self, t: WhaleTrade) -> None:
        if self.clock() - t.timestamp > self.s.max_signal_age_sec:
            return self.skip(t, "signal too old")
        if t.side == "BUY":
            return self.handle_buy(t)
        if t.side == "SELL" and self.s.mirror_sells:
            return self.handle_sell(t)

    def handle_buy(self, t: WhaleTrade) -> None:
        s = self.s
        if s.paused:
            return self.skip(t, "paused")
        if t.usdc < s.min_whale_trade_usd:
            return self.skip(t, "whale trade too small")
        if not (s.min_price <= t.price <= s.max_price):
            return self.skip(t, "price outside range")
        last = self.store.last_copy_ts(t.wallet, t.condition_id, self.mode)
        if last and self.clock() - last < s.cooldown_minutes * 60:
            return self.skip(t, "cooldown")

        ticker, side, why = self.resolve(t)
        if not ticker:
            return self.skip(t, why)

        today = day_start(self.clock())
        if self.store.copies_since(self.mode, today) >= s.max_trades_per_day:
            return self.skip(t, "daily trade count reached")
        opposite = "no" if side == "yes" else "yes"
        if any(p["side"] == opposite for p in self.store.open_positions(self.mode, ticker)):
            return self.skip(t, "already holding the other side")

        budget = min(
            size_for(t.usdc, s),
            s.daily_max_usd - self.store.spent_since(self.mode, today),
            s.max_open_usd - self.store.open_exposure(self.mode),
            s.per_market_max_usd - self.store.open_exposure(self.mode, ticker),
        )
        if budget < s.min_trade_usd * 0.5:
            return self.skip(t, "risk limit reached", budget=round(budget, 2))

        q = self.kalshi.get_quote(ticker)
        if not q.tradable:
            return self.skip(t, f"Kalshi market not open ({q.status})", ticker=ticker)
        ask = q.ask(side)
        if ask is None:
            return self.skip(t, "no sellers on Kalshi", ticker=ticker)
        limit = round(min(t.price + s.max_slippage, 0.99), 2)
        if ask > limit:
            return self.skip(t, "Kalshi price already moved", ticker=ticker, ask=ask, limit=limit)
        count = math.floor(budget / limit)
        if count < 1:
            return self.skip(t, "budget below one contract", ticker=ticker)

        if s.is_live:
            order = self.kalshi.create_order(
                ticker=ticker, side=side, action="buy", count=count, limit_price=limit,
                client_order_id=f"wc-{uuid.uuid4()}", time_in_force=s.time_in_force)
            filled = filled_count(order)
            if filled < 1:
                return self.skip(t, "order did not fill", ticker=ticker,
                                 order_status=order.get("status"))
            cost = fill_cost_usd(order, filled, limit)
        else:
            filled, cost = count, round(count * ask, 2)  # paper: assume fill at the ask

        self.store.open_position(self.mode, ticker, side, filled, cost, t.wallet,
                                 t.condition_id, t.outcome_index)
        self.store.log_event("copied", t.wallet, t.condition_id, title=t.title,
                             outcome=t.outcome, whale_usdc=t.usdc, whale_price=t.price,
                             ticker=ticker, side=side, count=filled, cost=cost, mode=self.mode)
        self.notify.send(
            f"{'LIVE' if s.is_live else 'Paper'} copy: {filled}x {ticker} {side.upper()} "
            f"for ${cost:.2f} (whale paid {t.price:.2f}, ${t.usdc:,.0f}) - {t.title} / {t.outcome}")

    def handle_sell(self, t: WhaleTrade) -> None:
        if t.usdc < self.s.min_whale_trade_usd * 0.5:
            return  # small trims don't trigger an exit
        mine = [p for p in self.store.open_positions(self.mode)
                if p["condition_id"] == t.condition_id and p["outcome_index"] == t.outcome_index
                and p["source_wallet"] == t.wallet]
        if not mine:
            return
        for p in mine:
            q = self.kalshi.get_quote(p["kalshi_ticker"])
            bid = q.bid(p["side"])
            floor = round(max(t.price - self.s.max_slippage, 0.01), 2)
            if bid is None or bid < floor:
                self.skip(t, "exit skipped: Kalshi bid too low", ticker=p["kalshi_ticker"],
                          bid=bid, floor=floor)
                continue
            if self.s.is_live:
                order = self.kalshi.create_order(
                    ticker=p["kalshi_ticker"], side=p["side"], action="sell", count=p["count"],
                    limit_price=floor, client_order_id=f"wc-{uuid.uuid4()}",
                    time_in_force=self.s.time_in_force)
                sold = filled_count(order)
                if sold < 1:
                    self.skip(t, "exit order did not fill", ticker=p["kalshi_ticker"])
                    continue
                value = fill_cost_usd(order, sold, floor)
            else:
                sold, value = p["count"], round(p["count"] * bid, 2)
            if sold >= p["count"]:
                self.store.close_position(p["id"], value)
            else:
                self.store.reduce_position(p["id"], sold, value)
            self.store.log_event("exited", t.wallet, t.condition_id, ticker=p["kalshi_ticker"],
                                 side=p["side"], count=sold, value=value, mode=self.mode)
            self.notify.send(f"Exit (whale sold): {sold}x {p['kalshi_ticker']} "
                             f"{p['side'].upper()} for ${value:.2f}")

    # -- settlement ------------------------------------------------------------------------
    def settle(self) -> None:
        for p in self.store.open_positions(self.mode):
            try:
                m = self.kalshi.get_market(p["kalshi_ticker"])
            except KalshiError as e:
                log.warning("settle lookup failed for %s: %s", p["kalshi_ticker"], e)
                continue
            result = (m.get("result") or "").lower()
            if m.get("status") in SETTLED_STATUSES and result in ("yes", "no"):
                value = float(p["count"]) if result == p["side"] else 0.0
                self.store.close_position(p["id"], value, status="settled")
                self.store.log_event("settled", p["source_wallet"], p["condition_id"],
                                     ticker=p["kalshi_ticker"], side=p["side"], result=result,
                                     pnl=round(value - p["cost_usd"], 2), mode=self.mode)

    # -- reporting ---------------------------------------------------------------------------
    def report(self, days: int = 7) -> str:
        since = int(self.clock()) - days * 86400
        pos = self.store.all_positions(self.mode)
        closed = [p for p in pos if p["status"] in ("closed", "settled")]
        open_ = [p for p in pos if p["status"] == "open"]
        realized = sum(p["pnl_usd"] or 0 for p in closed)
        wins = sum(1 for p in closed if (p["pnl_usd"] or 0) > 0)
        events = self.store.events_since(since)
        skipped: dict[str, int] = {}
        for e in events:
            if e["kind"] == "skipped" and e["detail"].get("mode", self.mode) == self.mode:
                r = e["detail"].get("reason", "?")
                skipped[r] = skipped.get(r, 0) + 1
        copied = sum(1 for e in events if e["kind"] == "copied"
                     and e["detail"].get("mode") == self.mode)
        lines = [
            f"whalecopy report ({self.mode} mode)",
            f"  open positions:   {len(open_)}  (${sum(p['cost_usd'] for p in open_):,.2f} at cost)",
            f"  closed/settled:   {len(closed)}  wins {wins}/{len(closed) or 0}",
            f"  realized P&L:     ${realized:,.2f}",
            f"  last {days}d copies: {copied}",
            f"  pending reviews:  {len(self.store.mappings('pending'))}",
        ]
        if skipped:
            lines.append(f"  last {days}d skips by reason:")
            for r, n in sorted(skipped.items(), key=lambda x: -x[1]):
                lines.append(f"    {n:5d}  {r}")
        return "\n".join(lines)
