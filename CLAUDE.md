# whalecopy

Copy-trading bot: watches chosen Polymarket wallets and mirrors their buys onto the
matching Kalshi market ($25–50 per trade, capped by daily/open/per-market limits).
Paper mode by default; live mode places real-money orders on Kalshi.

## Commands

```bash
pip install -e ".[dev]"
pytest                      # offline tests; all API calls are faked (tests/conftest.py)
whalecopy discover          # rank Polymarket wallets -> data/candidates.csv
whalecopy index             # download Kalshi open markets into SQLite
whalecopy run               # main loop (poll every 30s)
whalecopy once | report | review | check | map list | test-match "<q>" <outcome>
```

## Layout

- `whalecopy/config.py`: `Settings` dataclass. config.yaml, then UPPER_CASE env vars win.
  Live needs MODE=live **and** LIVE_CONFIRM=yes-real-money.
- `whalecopy/polymarket.py`: Data API (`/v1/leaderboard`, `/activity`) + Gamma market
  lookup. `aggregate_trades` merges split fills per market/outcome/side.
- `whalecopy/kalshi.py`: Trade API v2 client. RSA-PSS signs `timestamp + METHOD + path`
  (path includes `/trade-api/v2`, no query). Prices use `*_dollars` string fields.
- `whalecopy/matcher.py`: Polymarket outcome -> (Kalshi ticker, side). Sports moneylines
  via slug (`nba-lal-bos-2026-10-19` -> `KX...GAME-26OCT19LALBOS-BOS`); everything else via
  fuzzy text with date, number, direction/negation and ambiguity guards. Unsure matches
  go to a review queue.
- `whalecopy/engine.py`: the loop. `handle_buy` order of checks: age, paused, whale size,
  price range, cooldown, mapping, daily count, opposite side, budget caps, Kalshi quote,
  slippage, then order. Live orders use a deterministic `client_order_id`; an unclear
  order result halts the bot until `whalecopy resume`. `handle_sell` exits copies when
  the source whale sells (even late).
  `settle` books P&L when Kalshi markets resolve.
- `whalecopy/store.py`: SQLite (cursors, seen fills, Kalshi index, mappings, positions,
  event log). Paper and live positions are kept apart by `mode`.
- `whalecopy/dashboard.py`: read-only web page (stdlib HTTP server, Basic auth) started by
  `whalecopy run` when DASHBOARD_PASSWORD is set; port 8080, exposed by `[http_service]` in
  fly.toml. Opens SQLite with `mode=ro`; never trades.
- `whalecopy/discover.py`: leaderboard + per-wallet stats to pick wallets worth following.
- Deploy: `Dockerfile`, `fly.toml` (Fly worker + /data volume), or `Dockerfile.cloudflare` +
  `cloudflare/` (Cloudflare Containers; Litestream keeps SQLite in R2). See README.md.

## Rules for working on this repo

- This code can spend real money. Never switch MODE to live, set LIVE_CONFIRM, or run
  live orders on the user's behalf; the user does that themselves.
- Never commit or print Kalshi API keys or PEM contents. `.env`, `*.key`, `*.pem`,
  `config.yaml` and `data/` are gitignored.
- Any change to risk limits, sizing, order construction or matching needs a test in
  `tests/` (fake Kalshi/Polymarket clients live in `tests/conftest.py`).
- Prefer skipping a trade over guessing. A wrong market match loses money even when the
  whale is right.

## Known gaps / next ideas

- Not yet run against the live APIs (built in a sandbox with no network to Kalshi or
  Polymarket). First real run: `whalecopy index`, `whalecopy discover`, then a few hours
  of paper mode while watching `whalecopy report` and `fly logs`.
- Gamma lookup uses `condition_ids=` param; verify. If it fails, matching falls back to
  the activity title with no end date (so nothing auto-approves except sports).
- Sports matcher assumes Polymarket outcome order = team order in the slug. Verify with
  `whalecopy map list --status sports` before setting `sports_auto_approve`.
- Paper fills ignore order-book depth; fees are estimated with `paper_fee_rate`.
- No reconciliation against Kalshi `/portfolio/positions` yet; positions changed by hand
  on Kalshi aren't seen by the bot.
- Possible v2: compare Kalshi price with sharp sportsbook lines (odds API) and skip copies
  already priced above fair value.
