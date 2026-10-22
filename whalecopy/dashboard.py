"""Web dashboard: copied trades, open positions, wins/losses, P&L per trader.

Started by `whalecopy run` when DASHBOARD_PASSWORD is set (or on its own with
`whalecopy dashboard`). Plain stdlib HTTP server with Basic auth. Pages read the database
read-only; the one write is approving or rejecting a pending market match (the same as
`whalecopy review`). It never places orders or shows keys.
"""
from __future__ import annotations

import base64
import csv
import hmac
import html
import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs

from .config import Settings
from .kalshi import Quote
from .store import Store, day_start

log = logging.getLogger(__name__)

CLOSED_STATUSES = ("closed", "settled", "voided")
EVENT_KINDS = ("copied", "exited", "settled", "skipped", "halted", "error")
REVIEW_ACTIONS = ("approve", "flip", "reject")


# -- data -----------------------------------------------------------------------------
def wallet_names(s: Settings) -> dict[str, str]:
    """Display names: `wallet_names` setting ("0xabc=name"), else data/candidates.csv."""
    names: dict[str, str] = {}
    path = Path(s.data_dir) / "candidates.csv"
    if path.exists():
        try:
            with path.open(newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("wallet") and row.get("name"):
                        names[row["wallet"].lower()] = row["name"]
        except (OSError, csv.Error) as e:
            log.warning("could not read %s: %s", path, e)
    for item in s.wallet_names:
        if "=" in item:
            w, n = item.split("=", 1)
            names[w.strip().lower()] = n.strip()
    return names


def short(wallet: str) -> str:
    return f"{wallet[:6]}…{wallet[-4:]}" if len(wallet or "") > 12 else (wallet or "?")


def summarize(store: Store, mode: str, wallets: list[str], names: dict[str, str],
              quote: Callable[[str], Quote | None] | None = None, now: float | None = None,
              days: int = 7) -> dict:
    """Everything the page shows, as plain data (so it can be tested without HTTP)."""
    now = time.time() if now is None else now
    positions = store.all_positions(mode)
    open_ = [p for p in positions if p["status"] == "open"]
    closed = [p for p in positions if p["status"] in CLOSED_STATUSES]

    # Mark open positions at the Kalshi bid: roughly what selling now would bring.
    for p in open_:
        q = None
        if quote:
            try:
                q = quote(p["kalshi_ticker"])
            except Exception as e:  # a slow/broken quote must not break the page
                log.warning("dashboard quote for %s failed: %s", p["kalshi_ticker"], e)
        bid = q.bid(p["side"]) if q else None
        p["bid"] = bid
        p["value_now"] = round(p["count"] * bid, 2) if bid is not None else None
        p["unrealized"] = round(p["value_now"] - p["cost_usd"], 2) if bid is not None else None

    events = store.events_since(int(now) - days * 86400)
    events = [e for e in events if e["detail"].get("mode", mode) == mode]
    titles = store.copy_titles(mode)
    for p in positions:
        p["whale_title"] = (titles.get((p["condition_id"], p["kalshi_ticker"]))
                            or (store.kalshi_market(p["kalshi_ticker"]) or {}).get("title") or "")

    def tally(ps_open, ps_closed) -> dict:
        wins = sum(1 for p in ps_closed if (p["pnl_usd"] or 0) > 0)
        losses = sum(1 for p in ps_closed if (p["pnl_usd"] or 0) < 0)
        marked = [p["unrealized"] for p in ps_open if p.get("unrealized") is not None]
        return {
            "open": len(ps_open),
            "open_cost": round(sum(p["cost_usd"] for p in ps_open), 2),
            "closed": len(ps_closed),
            "wins": wins,
            "losses": losses,
            "realized": round(sum(p["pnl_usd"] or 0 for p in ps_closed), 2),
            "unrealized": round(sum(marked), 2) if marked else None,
            "unmarked": len(ps_open) - len(marked),
        }

    trader_wallets = list(dict.fromkeys(list(wallets) + [p["source_wallet"] for p in positions]))
    traders = []
    for w in trader_wallets:
        t = tally([p for p in open_ if p["source_wallet"] == w],
                  [p for p in closed if p["source_wallet"] == w])
        t["wallet"] = w
        t["name"] = names.get(w, short(w))
        t["following"] = w in wallets
        t["copies"] = sum(1 for p in positions if p["source_wallet"] == w)
        traders.append(t)

    # Cumulative realized P&L, one point per closed position, for the chart.
    curve, total = [], 0.0
    for p in sorted(closed, key=lambda p: p["closed_at"] or 0):
        total += p["pnl_usd"] or 0
        curve.append((p["closed_at"] or 0, round(total, 2)))

    skips: dict[str, int] = {}
    for e in events:
        if e["kind"] == "skipped":
            r = e["detail"].get("reason", "?")
            skips[r] = skips.get(r, 0) + 1

    today = day_start(now)
    return {
        "mode": mode,
        "now": int(now),
        "days": days,
        "halted": store.get(f"halted:{mode}"),
        "totals": tally(open_, closed),
        "today": {"copies": store.copies_since(mode, today),
                  "spent": round(store.spent_since(mode, today), 2)},
        "traders": traders,
        "open": sorted(open_, key=lambda p: -(p["opened_at"] or 0)),
        "closed": sorted(closed, key=lambda p: -(p["closed_at"] or 0))[:100],
        "curve": curve,
        "skips": sorted(skips.items(), key=lambda x: -x[1]),
        "events": [e for e in reversed(events) if e["kind"] in EVENT_KINDS][:60],
        "pending_reviews": len(store.mappings("pending")),
        "reviews": [dict(m, candidates=json.loads(m["candidates"] or "[]"))
                    for m in store.mappings("pending")],
        "names": names,
    }


def apply_review(store: Store, condition_id: str, outcome_index: int, action: str,
                 ticker: str | None = None) -> str:
    """Approve, flip or reject one pending match. Returns what was done, for the page."""
    m = store.get_mapping(condition_id, outcome_index)
    if not m or m["status"] != "pending":
        raise ValueError("that match is no longer waiting for review")
    if action not in REVIEW_ACTIONS:
        raise ValueError("unknown action")
    if action == "reject":
        store.set_mapping_status(condition_id, outcome_index, "rejected")
        return "Rejected. The bot won't copy trades on this market."
    if ticker and ticker not in {c["ticker"] for c in json.loads(m["candidates"] or "[]")}:
        raise ValueError("that Kalshi market wasn't one of the suggested matches")
    side = m["kalshi_side"]
    if action == "flip":
        side = "no" if side == "yes" else "yes"
    store.set_mapping_status(condition_id, outcome_index, "approved", ticker=ticker or None,
                             side=side)
    return f"Approved: buy {side.upper()} on {ticker or m['kalshi_ticker']}."


def review_token(password: str) -> str:
    """Hidden form field, so another website can't submit approvals through your browser."""
    return hmac.new(password.encode(), b"whalecopy-review", "sha256").hexdigest()


# -- html -----------------------------------------------------------------------------
def esc(v) -> str:
    return html.escape("" if v is None else str(v))


def money(v, sign: bool = False) -> str:
    if v is None:
        return "–"
    s = f"${abs(v):,.2f}"
    if v < 0:
        return "−" + s
    return ("+" + s) if sign and v > 0 else s


def pnl_cell(v) -> str:
    cls = "pos" if (v or 0) > 0 else "neg" if (v or 0) < 0 else ""
    return f'<td class="num {cls}">{money(v, sign=True)}</td>'


def ts_cell(ts) -> str:
    if not ts:
        return "<td>–</td>"
    utc = time.strftime("%b %d %H:%M UTC", time.gmtime(ts))
    return f'<td class="ts" data-ts="{int(ts)}">{utc}</td>'


def chart(curve: list[tuple[int, float]]) -> str:
    if not curve:
        return '<p class="muted" style="padding:0 14px">The chart appears once a trade has closed.</p>'
    w, h, pad = 640, 160, 8
    ys = [0.0] + [c[1] for c in curve]   # start at $0, then one point per closed trade
    y0, y1 = min(ys), max(ys)
    if y1 == y0:
        y1 = y0 + 1

    def X(i):
        return pad + i / (len(ys) - 1) * (w - 2 * pad)

    def Y(y):
        return h - pad - (y - y0) / (y1 - y0) * (h - 2 * pad)

    pts = " ".join(f"{X(i):.1f},{Y(y):.1f}" for i, y in enumerate(ys))
    cls = "pos" if curve[-1][1] >= 0 else "neg"
    return (f'<svg viewBox="0 0 {w} {h}" class="chart" preserveAspectRatio="none" role="img" '
            f'aria-label="Cumulative realized profit and loss">'
            f'<line x1="{pad}" x2="{w - pad}" y1="{Y(0):.1f}" y2="{Y(0):.1f}" class="zero"/>'
            f'<polyline points="{pts}" class="line {cls}"/></svg>')


def render(d: dict) -> str:
    t = d["totals"]
    names = d["names"]
    who = lambda w: esc(names.get(w, short(w)))  # noqa: E731
    total_pnl = t["realized"] + (t["unrealized"] or 0)
    decided = t["wins"] + t["losses"]
    win_rate = f"{t['wins'] / decided:.0%}" if decided else "–"

    cards = [
        ("Total P&amp;L", money(total_pnl, sign=True), "pos" if total_pnl > 0 else
         "neg" if total_pnl < 0 else "", "realized + open positions at today's price"),
        ("Realized P&amp;L", money(t["realized"], sign=True), "pos" if t["realized"] > 0 else
         "neg" if t["realized"] < 0 else "", f"{t['closed']} closed trades, after fees"),
        ("Open positions", str(t["open"]), "", f"{money(t['open_cost'])} at cost; "
         f"{money(t['unrealized'], sign=True)} if sold now"),
        ("Win rate", win_rate, "", f"{t['wins']} won, {t['losses']} lost"),
        ("Today", str(d["today"]["copies"]), "", f"trades copied, {money(d['today']['spent'])} spent"),
    ]
    card_html = "".join(
        f'<div class="card"><div class="label">{lbl}</div><div class="big {cls}">{val}</div>'
        f'<div class="muted">{sub}</div></div>' for lbl, val, cls, sub in cards)

    banners = []
    if d["mode"] != "live":
        banners.append('<div class="banner info">Paper mode: these are simulated trades, '
                       'no real money.</div>')
    if d["halted"]:
        banners.append(f'<div class="banner bad">The bot is HALTED: {esc(d["halted"])}. '
                       'It will not copy anything until someone checks Kalshi and runs '
                       '<code>whalecopy resume</code>.</div>')
    if d.get("flash"):
        banners.append(f'<div class="banner info">{esc(d["flash"])}</div>')
    if d["pending_reviews"]:
        banners.append(f'<div class="banner warn">{d["pending_reviews"]} market matches are '
                       'waiting for your OK. See <a href="#review">Matches to review</a>.</div>')
    if t["unmarked"]:
        banners.append(f'<div class="banner warn">{t["unmarked"]} open positions have no Kalshi '
                       "price right now, so they're left out of today's value.</div>")

    trader_rows = "".join(
        f'<tr><td>{esc(tr["name"])}<div class="muted mono">{esc(short(tr["wallet"]))}'
        f'{"" if tr["following"] else " · no longer followed"}</div></td>'
        f'<td class="num">{tr["copies"]}</td><td class="num">{tr["open"]}</td>'
        f'<td class="num">{tr["wins"]}–{tr["losses"]}</td>'
        f'{pnl_cell(tr["realized"])}{pnl_cell(tr["unrealized"])}'
        f'{pnl_cell(tr["realized"] + (tr["unrealized"] or 0))}</tr>'
        for tr in d["traders"]) or '<tr><td colspan="7" class="muted">No traders yet.</td></tr>'

    open_rows = "".join(
        f'<tr>{ts_cell(p["opened_at"])}<td>{who(p["source_wallet"])}</td>'
        f'<td>{esc(p["whale_title"])}'
        f'<div class="muted mono">{esc(p["kalshi_ticker"])}</div></td>'
        f'<td>{esc(p["side"].upper())}</td><td class="num">{p["count"]}</td>'
        f'<td class="num">{money(p["cost_usd"])}</td>'
        f'<td class="num">{money(p["value_now"])}</td>{pnl_cell(p["unrealized"])}</tr>'
        for p in d["open"]) or '<tr><td colspan="8" class="muted">No open positions.</td></tr>'

    outcome = {"settled": "market resolved", "closed": "sold (whale sold)",
               "voided": "voided, refunded"}
    closed_rows = "".join(
        f'<tr>{ts_cell(p["closed_at"])}<td>{who(p["source_wallet"])}</td>'
        f'<td>{esc(p["whale_title"])}<div class="muted mono">{esc(p["kalshi_ticker"])}</div></td>'
        f'<td>{esc(p["side"].upper())}</td><td>{esc(outcome.get(p["status"], p["status"]))}</td>'
        f'<td class="num">{money(p["cost_usd"])}</td>'
        f'<td class="num">{money(p["close_value_usd"])}</td>{pnl_cell(p["pnl_usd"])}</tr>'
        for p in d["closed"]) or '<tr><td colspan="8" class="muted">Nothing closed yet.</td></tr>'

    def describe(e) -> str:
        x = e["detail"]
        if e["kind"] == "copied":
            return (f'Copied {x.get("count")}× {esc(x.get("ticker"))} {esc(str(x.get("side", "")).upper())} '
                    f'for {money(x.get("cost"))}: {esc(x.get("title"))} / {esc(x.get("outcome"))}')
        if e["kind"] == "exited":
            return f'Sold {x.get("count")}× {esc(x.get("ticker"))} for {money(x.get("value"))}'
        if e["kind"] == "settled":
            return (f'{esc(x.get("ticker"))} resolved {esc(str(x.get("result", "")).upper())}, '
                    f'P&amp;L {money(x.get("pnl"), sign=True)}')
        if e["kind"] == "skipped":
            what = " / ".join(esc(v) for v in (x.get("title"), x.get("outcome")) if v)
            return f'Skipped ({esc(x.get("reason"))}): {what}'
        if e["kind"] == "halted":
            return f'HALTED: {esc(x.get("reason"))}'
        return f'Error: {esc(x.get("error"))} {esc(x.get("title"))}'

    event_rows = "".join(
        f'<tr class="ev-{e["kind"]}">{ts_cell(e["ts"])}'
        f'<td>{who(e["wallet"]) if e["wallet"] else ""}</td><td>{describe(e)}</td></tr>'
        for e in d["events"]) or '<tr><td colspan="3" class="muted">No activity yet.</td></tr>'

    token = esc(d.get("token", ""))

    def review_form(m) -> str:
        side = esc((m["kalshi_side"] or "").upper())
        other = "NO" if side == "YES" else "YES"
        opts = "".join(
            f'<label class="cand"><input type="radio" name="ticker" value="{esc(c["ticker"])}"'
            f'{" checked" if c["ticker"] == m["kalshi_ticker"] else ""}> '
            f'<span class="mono">{esc(c["ticker"])}</span> {esc(c["text"])} '
            f'<span class="muted">(score {c["score"]:.0f}'
            f'{"; " + esc("; ".join(c["notes"])) if c.get("notes") else ""})</span></label>'
            for c in m["candidates"][:5])
        return (
            f'<form method="post" action="/review" class="review">'
            f'<input type="hidden" name="token" value="{token}">'
            f'<input type="hidden" name="condition_id" value="{esc(m["condition_id"])}">'
            f'<input type="hidden" name="outcome_index" value="{m["outcome_index"]}">'
            f'<div class="q">{esc(m["poly_text"])}</div>'
            f'<div class="muted">Bot\'s pick: buy <b>{side}</b> on '
            f'<span class="mono">{esc(m["kalshi_ticker"])}</span>: {esc(m["kalshi_text"])}</div>'
            f'{opts}<div class="buttons">'
            f'<button name="action" value="approve" class="ok">Approve (buy {side})</button>'
            f'<button name="action" value="flip">Approve but buy {other}</button>'
            f'<button name="action" value="reject" class="no">Reject</button></div></form>')

    review_html = "".join(review_form(m) for m in d.get("reviews", [])) or \
        '<p class="muted" style="padding:0 14px">Nothing waiting for review.</p>'

    skip_rows = "".join(f'<tr><td>{esc(r)}</td><td class="num">{n}</td></tr>'
                        for r, n in d["skips"]) or \
        '<tr><td colspan="2" class="muted">No skipped trades.</td></tr>'

    return PAGE.format(
        mode=esc(d["mode"].upper()), updated=d["now"], banners="".join(banners),
        cards=card_html, chart=chart(d["curve"]), trader_rows=trader_rows,
        open_rows=open_rows, closed_rows=closed_rows, event_rows=event_rows,
        skip_rows=skip_rows, days=d["days"], review_html=review_html)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>whalecopy dashboard</title>
<style>
:root {{ --bg:#f7f7f5; --panel:#fff; --text:#1d1d1b; --muted:#6b6b66; --line:#e4e4df;
  --pos:#137a3a; --neg:#b42318; --info:#e8f0fe; --warn:#fff4dc; --bad:#fde7e6; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141413; --panel:#1f1f1d; --text:#ececea;
  --muted:#9d9d97; --line:#33332f; --pos:#4cc47a; --neg:#ff7a6e; --info:#1d2a40;
  --warn:#3a2f14; --bad:#45201d; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text);
  font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
main {{ max-width:1100px; margin:0 auto; padding:20px 16px 60px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} h2 {{ font-size:17px; margin:28px 0 10px; }}
.muted {{ color:var(--muted); font-size:13px; }} .mono {{ font-family:ui-monospace,Menlo,monospace; }}
.pos {{ color:var(--pos); }} .neg {{ color:var(--neg); }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-top:16px; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px; }}
.card .label {{ font-size:13px; color:var(--muted); }} .card .big {{ font-size:26px; font-weight:600; margin:2px 0; }}
.banner {{ border-radius:8px; padding:10px 14px; margin-top:12px; }}
.banner.info {{ background:var(--info); }} .banner.warn {{ background:var(--warn); }} .banner.bad {{ background:var(--bad); font-weight:600; }}
.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; }}
th,td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }}
th {{ font-size:12px; color:var(--muted); font-weight:600; text-transform:uppercase; letter-spacing:.03em; }}
tr:last-child td {{ border-bottom:0; }}
td.num, th.num {{ text-align:right; white-space:nowrap; }} td.ts {{ white-space:nowrap; color:var(--muted); font-size:13px; }}
tr.ev-skipped td {{ color:var(--muted); }} tr.ev-halted td, tr.ev-error td {{ color:var(--neg); }}
.chart {{ width:100%; height:160px; display:block; padding:8px; }}
.chart line, .chart polyline {{ vector-effect:non-scaling-stroke; }}
.chart .zero {{ stroke:var(--line); stroke-width:1; }}
.chart .line {{ fill:none; stroke-width:2.5; }} .chart .line.pos {{ stroke:var(--pos); }} .chart .line.neg {{ stroke:var(--neg); }}
details summary {{ cursor:pointer; margin:28px 0 10px; font-weight:600; }}
code {{ font-size:13px; }}
form.review {{ padding:12px 14px; border-bottom:1px solid var(--line); }}
form.review:last-child {{ border-bottom:0; }}
form.review .q {{ font-weight:600; margin-bottom:4px; }}
label.cand {{ display:block; margin:6px 0; font-size:14px; }}
.buttons {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }}
button {{ font:inherit; padding:8px 14px; border-radius:8px; border:1px solid var(--line);
  background:var(--panel); color:var(--text); cursor:pointer; }}
button.ok {{ background:var(--pos); border-color:var(--pos); color:#fff; }}
button.no {{ color:var(--neg); }}
</style></head>
<body><main>
<h1>whalecopy · {mode} mode</h1>
<div class="muted">Updated <span class="ts-inline" data-ts="{updated}"></span>. Refreshes every minute.</div>
{banners}
<div class="cards">{cards}</div>

<h2 id="review">Matches to review</h2>
<div class="muted" style="margin-bottom:8px">The bot found a Kalshi market it thinks matches,
but wasn't sure enough to use it alone. Approve it and the bot copies the next trade there.</div>
<div class="panel">{review_html}</div>

<h2>Profit so far, trade by trade</h2>
<div class="panel">{chart}</div>

<h2>By trader</h2>
<div class="panel"><table>
<tr><th>Trader</th><th class="num">Copies</th><th class="num">Open</th><th class="num">Won–lost</th>
<th class="num">Realized</th><th class="num">Open P&amp;L</th><th class="num">Total</th></tr>
{trader_rows}</table></div>

<h2>Open positions</h2>
<div class="panel"><table>
<tr><th>Opened</th><th>Trader</th><th>Market</th><th>Side</th><th class="num">Contracts</th>
<th class="num">Cost</th><th class="num">Worth now</th><th class="num">P&amp;L</th></tr>
{open_rows}</table></div>

<h2>Closed trades</h2>
<div class="panel"><table>
<tr><th>Closed</th><th>Trader</th><th>Market</th><th>Side</th><th>How</th>
<th class="num">Cost</th><th class="num">Paid out</th><th class="num">P&amp;L</th></tr>
{closed_rows}</table></div>

<h2>Recent activity (last {days} days)</h2>
<div class="panel"><table>
<tr><th>When</th><th>Trader</th><th>What happened</th></tr>
{event_rows}</table></div>

<details><summary>Why trades were skipped (last {days} days)</summary>
<div class="panel"><table><tr><th>Reason</th><th class="num">Count</th></tr>
{skip_rows}</table></div></details>
</main>
<script>
document.querySelectorAll("[data-ts]").forEach(function (el) {{
  var d = new Date(parseInt(el.dataset.ts, 10) * 1000);
  el.textContent = d.toLocaleString(undefined, {{month:"short", day:"numeric",
    hour:"numeric", minute:"2-digit"}});
}});
</script>
</body></html>
"""


# -- server ---------------------------------------------------------------------------
class QuoteCache:
    """Kalshi prices for open positions, reused for a minute so reloads stay cheap."""

    def __init__(self, fetch: Callable[[str], Quote], ttl: float = 60.0, clock=time.time):
        self.fetch, self.ttl, self.clock = fetch, ttl, clock
        self._cache: dict[str, tuple[float, Quote | None]] = {}
        self._lock = threading.Lock()

    def __call__(self, ticker: str) -> Quote | None:
        with self._lock:
            hit = self._cache.get(ticker)
            if hit and self.clock() - hit[0] < self.ttl:
                return hit[1]
        try:
            q = self.fetch(ticker)
        except Exception as e:
            log.warning("dashboard quote for %s failed: %s", ticker, e)
            q = None
        with self._lock:
            self._cache[ticker] = (self.clock(), q)
        return q


def check_auth(header: str | None, password: str) -> bool:
    """HTTP Basic auth; any username, the password must match."""
    if not password or not header or not header.startswith("Basic "):
        return False
    try:
        user_pass = base64.b64decode(header[6:], validate=True).decode()
    except (ValueError, UnicodeDecodeError):
        return False
    _, _, given = user_pass.partition(":")
    return hmac.compare_digest(given.encode(), password.encode())


FLASH = {"ok": "Saved. The bot uses your answer from its next trade on.",
         "stale": "That match was already handled, so nothing changed."}


def make_handler(s: Settings, quote: Callable[[str], Quote | None] | None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep request noise out of the bot's logs
            log.debug("dashboard %s " + fmt, self.address_string(), *args)

        def _send(self, code: int, body: str, ctype: str, extra: dict | None = None):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                return self._send(200, "ok", "text/plain")
            if not check_auth(self.headers.get("Authorization"), s.dashboard_password):
                return self._send(401, "Password required", "text/plain",
                                  {"WWW-Authenticate": 'Basic realm="whalecopy", charset="UTF-8"'})
            if path not in ("/", "/api/summary"):
                return self._send(404, "Not found", "text/plain")
            store = Store(s.db_path, readonly=True)
            try:
                d = summarize(store, s.mode, s.wallets, wallet_names(s), quote)
            finally:
                store.db.close()
            if path == "/api/summary":
                return self._send(200, json.dumps(d, default=str), "application/json")
            d["token"] = review_token(s.dashboard_password)
            d["flash"] = FLASH.get(self.path.split("?", 1)[1] if "?" in self.path else "")
            return self._send(200, render(d), "text/html; charset=utf-8")

        def do_POST(self):
            if not check_auth(self.headers.get("Authorization"), s.dashboard_password):
                return self._send(401, "Password required", "text/plain",
                                  {"WWW-Authenticate": 'Basic realm="whalecopy", charset="UTF-8"'})
            if self.path.split("?", 1)[0] != "/review":
                return self._send(404, "Not found", "text/plain")
            length = int(self.headers.get("Content-Length") or 0)
            if length > 10_000:
                return self._send(413, "Too large", "text/plain")
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
            if not hmac.compare_digest(form.get("token", ""), review_token(s.dashboard_password)):
                return self._send(403, "Reload the dashboard and try again.", "text/plain")
            store = Store(s.db_path)
            try:
                msg = apply_review(store, form.get("condition_id", ""),
                                   int(form.get("outcome_index") or -1),
                                   form.get("action", ""), form.get("ticker"))
                log.info("dashboard review: %s", msg)
                result = "ok"
            except (ValueError, KeyError) as e:
                log.info("dashboard review refused: %s", e)
                result = "stale"
            finally:
                store.db.close()
            self.send_response(303)
            self.send_header("Location", f"/?{result}#review")
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


class DualStackServer(ThreadingHTTPServer):
    """Listen on IPv6 and IPv4 (Fly's private network and `fly proxy` use IPv6)."""
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def start(s: Settings, quote: Callable[[str], Quote | None] | None = None,
          background: bool = True) -> ThreadingHTTPServer:
    if not s.dashboard_password:
        raise ValueError("Set DASHBOARD_PASSWORD to turn on the dashboard")
    if not s.db_path.exists():  # read-only open needs the file; create it the normal way
        Store(s.db_path).db.close()
    handler = make_handler(s, quote)
    try:
        server = DualStackServer(("::", s.dashboard_port), handler)
    except OSError:  # no IPv6 on this machine
        server = ThreadingHTTPServer(("0.0.0.0", s.dashboard_port), handler)
    server.daemon_threads = True
    log.info("dashboard listening on port %d", s.dashboard_port)
    if background:
        threading.Thread(target=server.serve_forever, name="dashboard", daemon=True).start()
    else:
        server.serve_forever()
    return server
