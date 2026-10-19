from datetime import datetime, timezone

import pytest
from conftest import CID_FED, CID_GAME, NOW, WALLET, FakeKalshi, FakePoly, trade_row

from whalecopy.config import Settings, load_settings
from whalecopy.engine import CopyEngine, size_for

LAL = "KXNBAGAME-26OCT19LALBOS-LAL"


@pytest.fixture
def env(settings, store):
    poly, kalshi = FakePoly(), FakeKalshi()
    kalshi.set_price(LAL, yes_ask=0.56, yes_bid=0.54, no_ask=0.46, no_bid=0.44)
    eng = CopyEngine(settings, store, poly, kalshi, clock=lambda: NOW)
    eng.refresh_index(force=True)
    store.set(f"cursor:{WALLET}", NOW - 86400)
    store.save_mapping(CID_GAME, 0, LAL, "yes", 100, "approved", "manual", "")
    return eng, poly, kalshi, store


def test_first_poll_never_copies_history(settings, store):
    poly, kalshi = FakePoly(), FakeKalshi()
    poly.rows = [trade_row()]
    eng = CopyEngine(settings, store, poly, kalshi, clock=lambda: NOW)
    assert eng.poll_wallet(WALLET) == []
    assert store.get(f"cursor:{WALLET}") == NOW


def test_sizing_scales_with_whale_size():
    s = Settings()
    assert size_for(500, s) == 25
    assert size_for(5000, s) == 50
    assert 25 < size_for(2750, s) < 50


def test_paper_copy(env):
    eng, poly, kalshi, store = env
    poly.rows = [trade_row(price=0.55, usdc=5000)]
    eng.tick()
    pos = store.open_positions("paper")
    assert len(pos) == 1
    p = pos[0]
    assert p["kalshi_ticker"] == LAL and p["side"] == "yes"
    assert p["count"] == 86            # floor($50 / 0.58 limit)
    assert p["cost_usd"] == round(86 * 0.56, 2)
    assert kalshi.orders == []         # paper mode never sends orders
    # same fills seen again -> no double copy
    eng.tick()
    assert len(store.open_positions("paper")) == 1


def test_fills_split_across_rows_are_aggregated(env):
    eng, poly, *_ = env
    poly.rows = [trade_row(usdc=300, tx="0x1"), trade_row(usdc=300, tx="0x2")]
    eng.tick()
    assert len(eng.store.open_positions("paper")) == 1   # 600 total clears the 500 minimum


def test_skips_when_price_moved(env):
    eng, poly, kalshi, store = env
    kalshi.set_price(LAL, yes_ask=0.70)
    poly.rows = [trade_row(price=0.55)]
    eng.tick()
    assert store.open_positions("paper") == []
    reasons = [e["detail"]["reason"] for e in store.events_since(0) if e["kind"] == "skipped"]
    assert "Kalshi price already moved" in reasons


@pytest.mark.parametrize("row,reason", [
    (trade_row(usdc=100), "whale trade too small"),
    (trade_row(price=0.97), "price outside range"),
    (trade_row(ts=NOW - 5000), "signal too old"),
])
def test_signal_filters(env, row, reason):
    eng, poly, _, store = env
    poly.rows = [row]
    eng.tick()
    assert store.open_positions("paper") == []
    assert reason in [e["detail"]["reason"] for e in store.events_since(0)]


def test_daily_cap_and_cooldown(env):
    eng, poly, _, store = env
    eng.s.daily_max_usd = 60
    eng.s.cooldown_minutes = 0
    poly.rows = [trade_row(usdc=5000, tx="0x1", ts=NOW - 60)]
    eng.tick()
    poly.rows.append(trade_row(usdc=5000, tx="0x2", ts=NOW - 30))
    eng.tick()
    spent = sum(p["cost_usd"] for p in store.open_positions("paper"))
    assert spent <= 60
    eng.s.cooldown_minutes = 60
    poly.rows.append(trade_row(usdc=5000, tx="0x3", ts=NOW - 10))
    eng.tick()
    assert "cooldown" in [e["detail"].get("reason") for e in store.events_since(0)]


def test_pending_mapping_is_not_traded(env):
    eng, poly, _, store = env
    store.set_mapping_status(CID_GAME, 0, "pending")
    poly.rows = [trade_row()]
    eng.tick()
    assert store.open_positions("paper") == []


def test_auto_match_via_matcher(env):
    eng, poly, kalshi, store = env
    eng.s.auto_approve_score = 80
    eng._matcher = None
    kalshi.set_price("KXFEDDECISION-26OCT-C50", no_ask=0.30, no_bid=0.28)
    poly.info[CID_FED] = {"question": "Will the Fed cut rates by 50 bps at the October 2026 "
                                      "meeting?", "outcomes": ["Yes", "No"],
                          "end": datetime(2026, 10, 28, tzinfo=timezone.utc)}
    poly.rows = [trade_row(cid=CID_FED, outcome="No", idx=1, price=0.29, title="Fed 50")]
    eng.tick()
    m = store.get_mapping(CID_FED, 1)
    assert m["status"] == "auto" and m["kalshi_ticker"].endswith("C50") and m["kalshi_side"] == "no"
    assert store.open_positions("paper")[0]["side"] == "no"


def test_whale_sell_closes_copy_and_settlement(env):
    eng, poly, kalshi, store = env
    poly.rows = [trade_row(usdc=5000, ts=NOW - 60)]
    eng.tick()
    poly.rows.append(trade_row(side="SELL", price=0.60, usdc=4000, tx="0xs", ts=NOW - 10))
    kalshi.set_price(LAL, yes_bid=0.59)
    eng.tick()
    assert store.open_positions("paper") == []
    closed = store.all_positions("paper")[0]
    assert closed["pnl_usd"] == pytest.approx(86 * 0.59 - 86 * 0.56, abs=0.01)


def test_settlement(env):
    eng, poly, kalshi, store = env
    poly.rows = [trade_row(usdc=5000)]
    eng.tick()
    kalshi.markets[LAL].update(status="settled", result="yes")
    eng.settle()
    p = store.all_positions("paper")[0]
    assert p["status"] == "settled" and p["pnl_usd"] == pytest.approx(86 - 86 * 0.56, abs=0.01)


def test_live_mode_sends_order_and_records_fill(env):
    eng, poly, kalshi, store = env
    eng.s.mode = "live"
    kalshi.fill_ratio = 0.5
    poly.rows = [trade_row(usdc=5000)]
    eng.tick()
    o = kalshi.orders[0]
    assert o["ticker"] == LAL and o["side"] == "yes" and o["action"] == "buy"
    assert o["limit_price"] == 0.58 and o["count"] == 86
    p = store.open_positions("live")[0]
    assert p["count"] == 43
    assert store.open_positions("paper") == []


def test_sports_slug_matches_paper_yes_live_only_if_enabled(env):
    eng, poly, kalshi, store = env
    cid = "0x" + "3" * 64
    kalshi.set_price("KXNBAGAME-26OCT19LALBOS-BOS", yes_ask=0.45)
    row = trade_row(cid=cid, outcome="Celtics", idx=1, price=0.44, tx="0xb")
    row["slug"] = "nba-lal-bos-2026-10-19"
    poly.rows = [row]
    eng.tick()
    assert store.get_mapping(cid, 1)["status"] == "sports"
    assert store.open_positions("paper")[0]["kalshi_ticker"].endswith("-BOS")

    eng.s.mode = "live"
    poly.rows.append(dict(row, transactionHash="0xc", timestamp=NOW - 5))
    eng.s.cooldown_minutes = 0
    eng.tick()
    assert kalshi.orders == []            # not enabled for live yet
    eng.s.sports_auto_approve = True
    poly.rows.append(dict(row, transactionHash="0xd", timestamp=NOW - 4))
    eng.tick()
    assert kalshi.orders and kalshi.orders[0]["ticker"].endswith("-BOS")


def test_paused(env):
    eng, poly, kalshi, store = env
    eng.s.paused = True
    poly.rows = [trade_row()]
    eng.tick()
    assert store.open_positions("paper") == []


def test_live_requires_second_switch(tmp_path, monkeypatch):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"wallets: ['{WALLET}']\n")
    monkeypatch.setenv("MODE", "live")
    with pytest.raises(ValueError, match="LIVE_CONFIRM"):
        load_settings(str(cfg))
    monkeypatch.setenv("MAX_TRADE_USD", "40")
    monkeypatch.setenv("MODE", "paper")
    assert load_settings(str(cfg)).max_trade_usd == 40.0


def test_wallets_from_env_with_empty_yaml_list(tmp_path, monkeypatch):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("wallets:\n")
    other = "0x" + "B" * 40
    monkeypatch.setenv("WALLETS", f"{WALLET}, {other}")
    assert load_settings(str(cfg)).wallets == [WALLET, other.lower()]
