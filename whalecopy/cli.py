"""Command line entry point: `whalecopy <command>` (or `python -m whalecopy <command>`)."""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time

from .config import load_settings
from .discover import discover
from .engine import CopyEngine
from .kalshi import KalshiClient
from .notify import Notifier
from .polymarket import PolymarketClient, parse_iso
from .store import Store

log = logging.getLogger("whalecopy")


def build(args):
    s = load_settings(args.config)
    logging.basicConfig(level=s.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = Store(s.db_path)
    kalshi = KalshiClient(s.kalshi_base_url, s.kalshi_api_key_id, s.private_key_bytes())
    engine = CopyEngine(s, store, PolymarketClient(), kalshi, Notifier(s.notify_webhook_url))
    return s, store, kalshi, engine


def cmd_run(args):
    s, store, kalshi, engine = build(args)
    if not s.wallets:
        sys.exit("No wallets configured. Run `whalecopy discover`, then add wallets to config.yaml.")
    banner = (f"whalecopy starting in {s.mode.upper()} mode on Kalshi {s.kalshi_env}; "
              f"following {len(s.wallets)} wallets; ${s.min_trade_usd:.0f}-${s.max_trade_usd:.0f}"
              f"/trade, ${s.daily_max_usd:.0f}/day cap")
    if s.is_live:
        banner += f"; Kalshi balance ${kalshi.get_balance():,.2f}"
    engine.notify.send(banner)

    stop = {"flag": False}
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.update(flag=True))
    while not stop["flag"]:
        started = time.time()
        try:
            engine.tick()
        except Exception:
            log.exception("tick failed; continuing")
        if time.time() - store.get("summary_at", 0) > 86400:
            engine.notify.send(engine.report(days=1))
            store.set("summary_at", time.time())
        while not stop["flag"] and time.time() - started < s.poll_seconds:
            time.sleep(1)
    log.info("stopped")


def cmd_once(args):
    _, _, _, engine = build(args)
    engine.tick()
    print(engine.report(days=1))


def cmd_check(args):
    s, store, kalshi, engine = build(args)
    print(f"mode:            {s.mode} (Kalshi {s.kalshi_env})")
    print(f"wallets:         {len(s.wallets)}")
    print(f"trade size:      ${s.min_trade_usd:.0f}-${s.max_trade_usd:.0f}, "
          f"daily cap ${s.daily_max_usd:.0f}, open cap ${s.max_open_usd:.0f}")
    print(f"database:        {s.db_path}")
    print(f"kalshi index:    {len(store.kalshi_index())} markets")
    if s.kalshi_api_key_id and s.private_key_bytes():
        print(f"kalshi balance:  ${kalshi.get_balance():,.2f}  (API key works)")
    else:
        print("kalshi key:      not set (fine for paper mode)")


def cmd_index(args):
    _, _, _, engine = build(args)
    print(f"indexed {engine.refresh_index(force=True)} markets")


def cmd_discover(args):
    s, *_ = build(args)
    cats = [c.strip().upper() for c in args.categories.split(",")] if args.categories else None
    results = discover(PolymarketClient(), s, cats, top=args.top, out_dir=s.data_dir)
    print(f"{'wallet':44} {'name':18} {'pnl_month':>11} {'pnl_all':>12} {'sig/wk':>7} "
          f"{'tr/day':>7}  verdict")
    for r in results[: args.show]:
        print(f"{r.wallet:44} {r.name[:18]:18} {r.pnl_month:>11,.0f} {r.pnl_all:>12,.0f} "
              f"{r.copyable_signals_per_week:>7} {r.trades_per_day:>7}  {r.verdict}")
    print(f"\nFull list saved to {s.data_dir}/candidates.csv")


def cmd_review(args):
    _, store, _, _ = build(args)
    pending = store.mappings("pending")
    if not pending:
        print("Nothing to review.")
        return
    for m in pending:
        cands = json.loads(m["candidates"] or "[]")
        print("\n" + "=" * 70)
        print(f"Polymarket: {m['poly_text']}")
        print(f"Best match: {m['kalshi_ticker']}  buy {m['kalshi_side'].upper()}  "
              f"(score {m['score']:.0f})")
        for i, c in enumerate(cands, 1):
            notes = f"  [{'; '.join(c['notes'])}]" if c.get("notes") else ""
            print(f"  {i}. {c['ticker']}: {c['text']} ({c['score']:.0f}){notes}")
        ans = input("[a]pprove best / [1-5] pick another / [f]lip YES-NO / [r]eject / "
                    "[s]kip: ").strip().lower()
        if ans == "a":
            store.set_mapping_status(m["condition_id"], m["outcome_index"], "approved")
        elif ans.isdigit() and 1 <= int(ans) <= len(cands):
            store.set_mapping_status(m["condition_id"], m["outcome_index"], "approved",
                                     ticker=cands[int(ans) - 1]["ticker"])
        elif ans == "f":
            flipped = "no" if m["kalshi_side"] == "yes" else "yes"
            store.set_mapping_status(m["condition_id"], m["outcome_index"], "approved",
                                     side=flipped)
        elif ans == "r":
            store.set_mapping_status(m["condition_id"], m["outcome_index"], "rejected")
        print("saved" if ans in ("a", "f", "r") or ans.isdigit() else "skipped")


def cmd_map(args):
    _, store, _, _ = build(args)
    if args.action == "list":
        for m in store.mappings(args.status):
            print(f"{m['status']:9} {m['score']:5.0f}  {m['kalshi_ticker'] or '-':32} "
                  f"{(m['kalshi_side'] or '-'):3}  {m['poly_text'][:70]}")
    elif args.action == "add":
        if args.side not in ("yes", "no"):
            sys.exit("side must be yes or no")
        store.save_mapping(args.condition_id, int(args.outcome_index), args.ticker, args.side,
                           100, "approved", "manual", args.ticker)
        print("saved")


def cmd_resume(args):
    s, store, _, engine = build(args)
    if not engine.halted:
        print("Not halted.")
        return
    print(f"Clearing halt: {engine.halted}")
    store.set(f"halted:{s.mode}", None)


def cmd_report(args):
    _, _, _, engine = build(args)
    print(engine.report(days=args.days))


def cmd_test_match(args):
    _, _, _, engine = build(args)
    res = engine.matcher.match(args.question, args.outcome, parse_iso(args.end))
    print(f"best: {res.ticker} side={res.side} score={res.score:.0f} "
          f"auto={'yes' if res.auto_ok else 'no'} {res.reason}")
    for c in res.candidates:
        print(f"  {c.score:5.0f}  {c.ticker}: {c.text}  {c.notes}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="whalecopy", description=__doc__)
    p.add_argument("--config", default=None, help="path to config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the copy loop forever").set_defaults(fn=cmd_run)
    sub.add_parser("once", help="run one polling pass").set_defaults(fn=cmd_once)
    sub.add_parser("check", help="show settings and test the Kalshi key").set_defaults(fn=cmd_check)
    sub.add_parser("index", help="refresh the Kalshi market index now").set_defaults(fn=cmd_index)
    d = sub.add_parser("discover", help="rank Polymarket wallets worth following")
    d.add_argument("--categories", default="")
    d.add_argument("--top", type=int, default=50)
    d.add_argument("--show", type=int, default=25)
    d.set_defaults(fn=cmd_discover)
    sub.add_parser("review", help="approve or reject pending market matches").set_defaults(
        fn=cmd_review)
    m = sub.add_parser("map", help="list or add market mappings by hand")
    m.add_argument("action", choices=["list", "add"])
    m.add_argument("condition_id", nargs="?")
    m.add_argument("outcome_index", nargs="?")
    m.add_argument("ticker", nargs="?")
    m.add_argument("side", nargs="?")
    m.add_argument("--status", default=None)
    m.set_defaults(fn=cmd_map)
    sub.add_parser("resume", help="clear a safety halt after checking Kalshi").set_defaults(
        fn=cmd_resume)
    r = sub.add_parser("report", help="P&L and activity summary")
    r.add_argument("--days", type=int, default=7)
    r.set_defaults(fn=cmd_report)
    t = sub.add_parser("test-match", help="see how a Polymarket question would map")
    t.add_argument("question")
    t.add_argument("outcome")
    t.add_argument("--end", default=None, help="ISO end date, e.g. 2026-11-03")
    t.set_defaults(fn=cmd_test_match)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
