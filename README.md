# whalecopy

Watches the Polymarket wallets you choose. When one of them makes a sizable buy, whalecopy
finds the same market on Kalshi and buys the same side there, $25–50 per trade, inside
daily and total risk caps. When that whale sells, it exits your copy.

It starts in **paper mode**: it watches real trades and real Kalshi prices but only
simulates your orders. Nothing touches your money until you turn on live mode yourself,
with two separate switches.

> Not financial advice. Copying traders loses money too: you get worse prices than they
> did, you can't see why they traded, and past winners regress. Run paper mode first.

## How it decides to copy

1. **Signal.** A followed wallet buys ≥ $500 (summed across its split fills) at a price
   between 0.05 and 0.92, within the last 10 minutes.
2. **Match.** The Polymarket market is mapped to a Kalshi ticker and side:
   - *Games* (moneylines): matched by slug, e.g. `nba-lal-bos-2026-10-19` →
     `KXNBAGAME-26OCT19LALBOS-BOS`. Used in paper mode automatically. In live mode only
     after you check them and set `sports_auto_approve: true`.
   - *Everything else*: fuzzy text match with guards: dates must agree, numbers like
     "50 bps" or "$120k" must agree, direction and negation words (above/below, not)
     must agree, and the best match can't be a near-tie. Only scores ≥ 92 trade automatically;
     the rest wait for `whalecopy review`. Wrong matches are the main way this bot can
     lose money on a "right" call, so it errs toward skipping.
3. **Price check.** Kalshi's ask must be no more than the whale's price + 3¢.
4. **Risk check.** Daily cap ($250), open cap ($1,000), per-market cap ($100), 15 trades a
   day, one copy per wallet+market per hour, and never two conflicting positions in one
   Kalshi event (e.g. both teams of a game).
5. **Size.** $25 for a $500 whale buy, scaling to $50 for $5,000+.
6. **Order.** Live mode sends an immediate-or-cancel limit order at whale price + 3¢,
   so it can't fill worse than that. Each order's id is derived from the whale's fill, so
   one signal can never become two orders. If Kalshi doesn't answer clearly (timeout,
   server error), the bot **halts** all trading and alerts you; check your Kalshi
   positions, then run `whalecopy resume`. Paper mode assumes a fill at the ask and
   subtracts an estimated fee (`paper_fee_rate`).

Polymarket's international site lists many markets Kalshi doesn't (geopolitics, many
crypto and non-US events). Those signals are skipped. `whalecopy report` shows how many.

## 1. Run it locally (paper mode)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp config.example.yaml config.yaml
pytest                        # 40 offline tests

whalecopy discover            # ranks Polymarket wallets; saves data/candidates.csv
```

Pick 3–8 wallets marked `good candidate` and paste them under `wallets:` in
`config.yaml`. Then:

```bash
whalecopy index               # downloads the Kalshi market list (~1-2 min)
whalecopy run                 # Ctrl-C to stop
```

Other commands:

| Command | What it does |
|---|---|
| `whalecopy report --days 7` | Copies, P&L, win rate, and why trades were skipped |
| `whalecopy review` | Approve, re-pick, flip, or reject uncertain matches |
| `whalecopy map list --status sports` | See game matches before enabling them live |
| `whalecopy map add <conditionId> <outcomeIndex> <TICKER> yes\|no` | Add a match by hand |
| `whalecopy test-match "Will the Fed cut 50 bps in October 2026?" Yes --end 2026-10-28` | Preview a match |
| `whalecopy check` | Show settings, test your Kalshi key |
| `whalecopy resume` | Clear a safety halt after checking your Kalshi account |

## 2. Deploy to the cloud (Fly.io)

Costs a few dollars a month for one small machine plus a 1 GB volume.

```bash
# install flyctl: https://fly.io/docs/flyctl/install/
fly auth login
# edit `app = "..."` in fly.toml to something unique, then:
fly launch --copy-config --no-deploy
fly volumes create whalecopy_data --size 1 --region iad
fly secrets set WALLETS="0xabc...,0xdef..."          # or bake them into config.yaml
fly secrets set NOTIFY_WEBHOOK_URL="https://hooks.slack.com/..."   # optional alerts
fly deploy
fly scale count 1                                   # exactly one machine, always
fly logs                                            # watch it work
```

Run commands on the server with `fly ssh console -C "whalecopy report"` (or `review`).
Pause instantly with `fly secrets set PAUSED=true`. Any setting can change the same way,
e.g. `fly secrets set MAX_TRADE_USD=40`.

**Railway / Render alternative:** create a service from this folder (it uses the
Dockerfile), add a volume mounted at `/data`, and set the same env vars in the dashboard.
Run it as a background worker, not a web service.

## 3. Going live (when you're ready)

Do this only after a couple of weeks of paper results you're happy with.

1. In Kalshi, create an API key (Settings → API). Keep the private key file safe.
2. Optional dry run on Kalshi's demo exchange with a demo key:
   `KALSHI_ENV=demo whalecopy check` confirms the key and signing work.
3. Check sports matches: `whalecopy map list --status sports`. If they look right, set
   `SPORTS_AUTO_APPROVE=true`.
4. Set the secrets and flip both switches:

```bash
fly secrets set KALSHI_API_KEY_ID="..." \
  KALSHI_PRIVATE_KEY_PEM="$(cat kalshi.key)" \
  MODE=live LIVE_CONFIRM=yes-real-money
```

The bot refuses to start live without `LIVE_CONFIRM=yes-real-money`. On startup it posts
your Kalshi balance to the webhook. Paper and live positions are tracked separately.

To go back to paper: `fly secrets set MODE=paper`.

## Known limits

- **Polymarket data lags the chain** by seconds to a minute, and the bot polls every 30s,
  so you are usually 30–90s behind the whale.
- **Paper fills are optimistic.** It assumes your whole order fills at the best ask;
  live, thin Kalshi books may fill partly or not at all.
- **Exits** close your whole copy when the whale sells ≥ $250 of that outcome. It doesn't
  track partial trims.
- **Fees** in paper mode are an estimate (`paper_fee_rate`, default 0.07 × contracts ×
  price × (1 − price)). Live mode records the fees Kalshi reports.
- The Polymarket and Kalshi APIs change. If a request starts failing, check
  docs.polymarket.com and docs.kalshi.com first.
- The sports matcher assumes Polymarket's outcome order follows the team order in the
  slug. That's why it stays paper-only until you confirm it.
