"""The copy loop: watch wallets -> map to Kalshi -> check price and risk -> trade (paper or live)."""
from __future__ import annotations

import hashlib
import logging
import math
import time
from dataclasses import replace

from .config import Settings
from .kalshi import (KalshiClient, KalshiError, estimate_fee, fees_usd, fill_cost_usd,
                     filled_count)
from .matcher import Matcher, index_row_from_market, sports_match
from .notify import Notifier
from .polymarket import PolymarketClient, WhaleTrade, activity_key, aggregate_trades
from .store import Store, day_start

log = logging.getLogger(__name__)

SETTLED_STATUSES = {"settled", "determined", "finalized"}
CURSOR_OVERLAP_SEC = 300   # re-read the last 5 minutes each poll; the seen-set dedupes
MAX_PAGES = 10


def size_for(usdc: float, s: Settings) -> float:
    """Scale between min and max trade size by how big the whale's buy was."""
    if usdc >= s.big_whale_trade_usd:
        return s.max_trade_usd
    span = max(s.big_whale_trade_usd - s.min_whale_trade_usd, 1e-9)
    frac = max(0.0, (usdc - s.min_whale_trade_usd) / span)
    return round(s.min_trade_usd + frac * (s.max_trade_usd - s.min_trade_usd), 2)


def order_id_for(t: WhaleTrade, action: str, ticker: str) -> str:
    """Deterministic id, so the same signal can never become two Kalshi orders."""
    h = hashlib.sha256("|".join((action, ticker) + t.keys).encode()).hexdigest()[:24]
    return f"wc-{h}"


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

    @property
    def halted(self) -> str | None:
        return self.store.get(f"halted:{self.mode}")

    def halt(self, reason: str) -> None:
        self.store.set(f"halted:{self.mode}", reason)
        self.store.log_event("halted", reason=reason, mode=self.mode)
        self.notify.send(f"HALTED ({self.mode}): {reason}. Check your Kalshi account, then "
                         "run `whalecopy resume`.")

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
        start = max(int(cursor) - CURSOR_OVERLAP_SEC, 1)
        rows: list[dict] = []
        for page in range(MAX_PAGES):
            batch = self.poly.activity(wallet, start=start, offset=page * 500)
            rows.extend(batch)
            if len(batch) < 500:
                break
        fresh = [r for r in rows if not self.store.is_seen(activity_key(r))]
        if not fresh:
            return []
        self.store.mark_seen(activity_key(r) for r in fresh)
        self.store.set(key, max([int(cursor)] + [int(r.get("timestamp") or 0) for r in fresh]))
        return aggregate_trades(wallet, fresh)

    # -- mapping -------------------------------------------------------------------------
    def resolve(self, t: WhaleTrade) -> tuple[str | None, str | None, str]:
        m = self.store.get_mapping(t.condition_id, t.outcome_index)
        index_time = self.store.get("index_refreshed_at", 0)
        if m and not (m["status"] == "nomatch" and m["created_at"] < index_time):
            return self._usable(m["kalshi_ticker"], m["kalshi_side"], m["status"])

        sm = sports_match(self.matcher.rows, t.slug, t.outcome_index, t.title, t.outcome)
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
        if status in ("auto", "approved") and ticker and side:
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
        try:
            self.refresh_index()
        except Exception as e:
            log.warning("Kalshi index refresh failed (will retry): %s", e)
        for wallet in self.s.wallets:
            try:
                trades = self.poll_wallet(wallet)
            except Exception as e:  # one bad wallet shouldn't stop the others
                log.warning("polling %s failed: %s", wallet, e)
                continue
            for t in trades:
                try:
                    self.handle(t)
                except Exception as e:  # one bad trade shouldn't drop the rest
                    log.exception("error handling %s", t.title)
                    self.store.log_event("error", t.wallet, t.condition_id, error=repr(e),
                                         title=t.title, mode=self.mode)
        if self.clock() - self.store.get("settled_at", 0) > 3600:
            try:
                self.settle()
            except Exception:
                log.exception("settlement pass failed")
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
        if t.side == "BUY":
            if self.clock() - t.timestamp > self.s.max_signal_age_sec:
                return self.skip(t, "signal too old")
            return self.handle_buy(t)
        if t.side == "SELL" and self.s.mirror_sells:
            return self.handle_sell(t)  # exits are mirrored even if late

    def build_up(self, t: WhaleTrade) -> WhaleTrade:
        """Whales often build a position in small buys spread over several polls.

        Keep a running total per wallet+outcome; once the buys inside the window add up
        to the minimum, return them as one combined trade (and start the total over).
        """
        key = f"buildup:{t.wallet}:{t.condition_id}:{t.outcome_index}"
        b = self.store.get(key)
        if not b or t.timestamp - b["first_ts"] > self.s.buildup_window_sec:
            b = {"first_ts": t.timestamp, "usdc": 0.0, "shares": 0.0, "keys": []}
        b["usdc"] += t.usdc
        b["shares"] += t.shares
        b["keys"] += list(t.keys)
        if b["usdc"] < self.s.min_whale_trade_usd:
            self.store.set(key, b)
            return replace(t, usdc=b["usdc"])
        self.store.delete(key)
        return replace(t, usdc=b["usdc"], shares=b["shares"], price=b["usdc"] / b["shares"],
                       keys=tuple(b["keys"]))

    def _send_order(self, t: WhaleTrade, **kw) -> dict | None:
        """Live order with a deterministic id. If the outcome is unknown, halt everything."""
        try:
            return self.kalshi.create_order(
                client_order_id=order_id_for(t, kw["action"], kw["ticker"]),
                time_in_force=self.s.time_in_force, **kw)
        except KalshiError as e:
            if " -> 4" in str(e):     # Kalshi rejected it (4xx): nothing was placed
                self.skip(t, "Kalshi rejected order", ticker=kw["ticker"], error=str(e)[:200])
                return None
            self.halt(f"order outcome unknown for {kw['ticker']} ({e})")
            return None

    def handle_buy(self, t: WhaleTrade) -> None:
        s = self.s
        if s.paused:
            return self.skip(t, "paused")
        if self.halted:
            return self.skip(t, "halted")
        if t.usdc < s.min_whale_trade_usd:
            combined = self.build_up(t)
            if combined.usdc < s.min_whale_trade_usd:
                return self.skip(t, "whale trade too small", built_up=round(combined.usdc, 2))
            t = combined
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
        event = (self.store.kalshi_market(ticker) or {}).get("event_ticker")
        for p in self.store.open_positions(self.mode):
            p_event = (self.store.kalshi_market(p["kalshi_ticker"]) or {}).get("event_ticker")
            same_event = p["kalshi_ticker"] == ticker or (event and p_event == event)
            if same_event and (p["kalshi_ticker"], p["side"]) != (ticker, side):
                return self.skip(t, "already holding a conflicting position in this event",
                                 ticker=ticker, held=p["kalshi_ticker"])

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
        # leave room for fees inside the budget
        count = math.floor(budget / (limit + s.paper_fee_rate * limit * (1 - limit) + 0.01))
        if count < 1:
            return self.skip(t, "budget below one contract", ticker=ticker)

        if s.is_live:
            order = self._send_order(t, ticker=ticker, side=side, action="buy", count=count,
                                     limit_price=limit)
            if order is None:
                return
            filled = filled_count(order)
            if filled < 1:
                return self.skip(t, "order did not fill", ticker=ticker,
                                 order_status=order.get("status"))
            cost = round(fill_cost_usd(order, filled, limit) + fees_usd(order), 2)
        else:  # paper: assume a full fill at the ask, plus an estimated fee
            filled = count
            cost = round(count * ask + estimate_fee(count, ask, s.paper_fee_rate), 2)

        self.store.open_position(self.mode, ticker, side, filled, cost, t.wallet,
                                 t.condition_id, t.outcome_index)
        self.store.log_event("copied", t.wallet, t.condition_id, title=t.title,
                             outcome=t.outcome, whale_usdc=t.usdc, whale_price=t.price,
                             ticker=ticker, side=side, count=filled, cost=cost, mode=self.mode)
        self.notify.send(
            f"{'LIVE' if s.is_live else 'Paper'} copy: {filled}x {ticker} {side.upper()} "
            f"for ${cost:.2f} (whale paid {t.price:.2f}, ${t.usdc:,.0f}) - {t.title} / {t.outcome}")

    def handle_sell(self, t: WhaleTrade) -> None:
        self.store.delete(f"buildup:{t.wallet}:{t.condition_id}:{t.outcome_index}")
        if t.usdc < self.s.min_whale_trade_usd * 0.5:
            return  # small trims don't trigger an exit
        mine = [p for p in self.store.open_positions(self.mode)
                if p["condition_id"] == t.condition_id and p["outcome_index"] == t.outcome_index
                and p["source_wallet"] == t.wallet]
        if not mine:
            return
        if self.halted:
            return self.skip(t, "halted (exit not mirrored)")
        for p in mine:
            q = self.kalshi.get_quote(p["kalshi_ticker"])
            bid = q.bid(p["side"])
            floor = round(max(t.price - self.s.max_slippage, 0.01), 2)
            if bid is None or bid < floor:
                self.skip(t, "exit skipped: Kalshi bid too low", ticker=p["kalshi_ticker"],
                          bid=bid, floor=floor)
                continue
            if self.s.is_live:
                order = self._send_order(t, ticker=p["kalshi_ticker"], side=p["side"],
                                         action="sell", count=p["count"], limit_price=floor)
                if order is None:
                    continue
                sold = filled_count(order)
                if sold < 1:
                    self.skip(t, "exit order did not fill", ticker=p["kalshi_ticker"])
                    continue
                # IOC sell at `floor` fills at the bid or better; book the floor (conservative)
                value = round(sold * floor - fees_usd(order), 2)
            else:
                sold = p["count"]
                value = round(sold * bid - estimate_fee(sold, bid, self.s.paper_fee_rate), 2)
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
            if m.get("status") not in SETTLED_STATUSES:
                continue
            result = (m.get("result") or "").lower()
            if result in ("yes", "no"):
                value, status = (float(p["count"]) if result == p["side"] else 0.0), "settled"
            else:  # voided/cancelled market: Kalshi refunds at cost
                value, status = float(p["cost_usd"]), "voided"
            self.store.close_position(p["id"], value, status=status)
            self.store.log_event("settled", p["source_wallet"], p["condition_id"],
                                 ticker=p["kalshi_ticker"], side=p["side"], result=result or "void",
                                 pnl=round(value - p["cost_usd"], 2), mode=self.mode)

    # -- reporting ---------------------------------------------------------------------------
    def report(self, days: int = 7) -> str:
        since = int(self.clock()) - days * 86400
        pos = self.store.all_positions(self.mode)
        closed = [p for p in pos if p["status"] in ("closed", "settled", "voided")]
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
            f"  realized P&L:     ${realized:,.2f}  (after fees)",
            f"  last {days}d copies: {copied}",
            f"  pending reviews:  {len(self.store.mappings('pending'))}",
        ]
        if self.halted:
            lines.insert(1, f"  HALTED: {self.halted}  (run `whalecopy resume` after checking)")
        if skipped:
            lines.append(f"  last {days}d skips by reason:")
            for r, n in sorted(skipped.items(), key=lambda x: -x[1]):
                lines.append(f"    {n:5d}  {r}")
        return "\n".join(lines)
