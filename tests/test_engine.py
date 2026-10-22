from datetime import datetime, timezone

import pytest
from conftest import CID_FED, CID_GAME, NOW, WALLET, FakeKalshi, FakePoly, trade_row

from whalecopy.config import Settings, load_settings
from whalecopy.engine import CopyEngine, size_for
from whalecopy.kalshi import KalshiError, estimate_fee

LAL = "KXNBAGAME-26OCT19LALBOS-LAL"
N = 82   # floor($50 / (0.58 limit + fee room + 1c))
PAPER_COST = round(N * 0.56 + estimate_fee(N, 0.56, 0.07), 2)


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
    assert p["count"] == N
    assert p["cost_usd"] == PAPER_COST
    assert p["cost_usd"] <= 50
    assert kalshi.orders == []         # paper mode never sends orders
    # same fills seen again -> no double copy
    eng.tick()
    assert len(store.open_positions("paper")) == 1


def test_fills_split_across_rows_are_aggregated(env):
    eng, poly, *_ = env
    poly.rows = [trade_row(usdc=300, tx="0x1"), trade_row(usdc=300, tx="0x2")]
    eng.tick()
    assert len(eng.store.open_positions("paper")) == 1   # 600 total clears the 500 minimum


def test_small_buys_across_polls_build_up_to_one_copy(env):
    eng, poly, _, store = env
    for i, ts in enumerate((NOW - 400, NOW - 200, NOW - 30)):
        poly.rows.append(trade_row(usdc=200, ts=ts, tx=f"0xb{i}"))
        eng.tick()
        assert len(store.open_positions("paper")) == (1 if i == 2 else 0)
    copied = [e for e in store.events_since(0) if e["kind"] == "copied"]
    assert len(copied) == 1 and copied[0]["detail"]["whale_usdc"] == 600
    skipped = [e["detail"] for e in store.events_since(0) if e["kind"] == "skipped"]
    assert [d["built_up"] for d in skipped] == [200, 400]
    assert store.get(f"buildup:{WALLET}:{CID_GAME}:0") is None   # total starts over


def test_small_buys_outside_window_do_not_combine(env):
    eng, poly, _, store = env
    eng.s.buildup_window_sec = 100
    poly.rows = [trade_row(usdc=300, ts=NOW - 400, tx="0xb1")]
    eng.tick()
    poly.rows.append(trade_row(usdc=300, ts=NOW - 30, tx="0xb2"))
    eng.tick()
    assert store.open_positions("paper") == []


def test_whale_sell_resets_build_up(env):
    eng, poly, _, store = env
    poly.rows = [trade_row(usdc=300, ts=NOW - 400, tx="0xb1")]
    eng.tick()
    poly.rows.append(trade_row(usdc=50, ts=NOW - 300, side="SELL", tx="0xs1"))
    eng.tick()
    poly.rows.append(trade_row(usdc=300, ts=NOW - 30, tx="0xb2"))
    eng.tick()
    assert store.open_positions("paper") == []


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
    exit_value = N * 0.59 - estimate_fee(N, 0.59, 0.07)
    assert closed["pnl_usd"] == pytest.approx(exit_value - PAPER_COST, abs=0.01)


def test_settlement(env):
    eng, poly, kalshi, store = env
    poly.rows = [trade_row(usdc=5000)]
    eng.tick()
    kalshi.markets[LAL].update(status="settled", result="yes")
    eng.settle()
    p = store.all_positions("paper")[0]
    assert p["status"] == "settled" and p["pnl_usd"] == pytest.approx(N - PAPER_COST, abs=0.01)


def test_live_mode_sends_order_and_records_fill(env):
    eng, poly, kalshi, store = env
    eng.s.mode = "live"
    kalshi.fill_ratio = 0.5
    poly.rows = [trade_row(usdc=5000)]
    eng.tick()
    o = kalshi.orders[0]
    assert o["ticker"] == LAL and o["side"] == "yes" and o["action"] == "buy"
    assert o["limit_price"] == 0.58 and o["count"] == N
    assert o["client_order_id"].startswith("wc-")
    p = store.open_positions("live")[0]
    assert p["count"] == N // 2
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


def test_unknown_order_outcome_halts_everything(env):
    eng, poly, kalshi, store = env
    eng.s.mode = "live"
    kalshi.order_error = KalshiError("POST /portfolio/orders failed: read timed out")
    poly.rows = [trade_row(usdc=5000)]
    eng.tick()
    assert eng.halted and store.open_positions("live") == []
    kalshi.order_error = None
    poly.rows.append(trade_row(usdc=5000, tx="0x9", ts=NOW - 5))
    eng.s.cooldown_minutes = 0
    eng.tick()
    assert len(kalshi.orders) == 1          # nothing else is sent while halted


def test_rejected_order_does_not_halt(env):
    eng, poly, kalshi, _ = env
    eng.s.mode = "live"
    kalshi.order_error = KalshiError("POST /portfolio/orders -> 400: insufficient balance")
    poly.rows = [trade_row(usdc=5000)]
    eng.tick()
    assert not eng.halted


def test_one_bad_trade_does_not_drop_the_rest(env, monkeypatch):
    eng, poly, _, store = env
    cid2 = "0x" + "4" * 64
    store.save_mapping(cid2, 0, LAL, "yes", 100, "approved", "manual", "")
    poly.info = None   # market_info would crash if called; first trade has an unknown mapping
    bad = trade_row(cid="0x" + "5" * 64, tx="0xbad", ts=NOW - 40)
    good = trade_row(cid=cid2, tx="0xgood", ts=NOW - 30)
    poly.rows = [bad, good]
    eng.tick()
    assert len(store.open_positions("paper")) == 1
    assert any(e["kind"] == "error" for e in store.events_since(0))


def test_no_conflicting_positions_in_same_event(env):
    eng, poly, kalshi, store = env
    cid2 = "0x" + "6" * 64
    store.save_mapping(cid2, 1, "KXNBAGAME-26OCT19LALBOS-BOS", "yes", 100, "approved", "", "")
    kalshi.set_price("KXNBAGAME-26OCT19LALBOS-BOS", yes_ask=0.45)
    poly.rows = [trade_row(tx="0x1", ts=NOW - 40),
                 trade_row(cid=cid2, idx=1, outcome="Celtics", price=0.44, tx="0x2", ts=NOW - 30)]
    eng.tick()
    held = store.open_positions("paper")
    assert len(held) == 1 and held[0]["kalshi_ticker"] == LAL


def test_late_whale_sell_still_exits(env):
    eng, poly, kalshi, store = env
    poly.rows = [trade_row(usdc=5000, ts=NOW - 60)]
    eng.tick()
    poly.rows.append(trade_row(side="SELL", price=0.60, usdc=4000, tx="0xs", ts=NOW - 5000))
    kalshi.set_price(LAL, yes_bid=0.59)
    eng.store.set(f"cursor:{WALLET}", NOW - 86400)   # e.g. after downtime
    eng.tick()
    assert store.open_positions("paper") == []


def test_voided_market_refunds(env):
    eng, poly, kalshi, store = env
    poly.rows = [trade_row(usdc=5000)]
    eng.tick()
    kalshi.markets[LAL].update(status="finalized", result="")
    eng.settle()
    p = store.all_positions("paper")[0]
    assert p["status"] == "voided" and p["pnl_usd"] == 0


def test_config_rejects_bool_typos_and_yaml_secrets(tmp_path, monkeypatch):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("min_trade_usd: 25\n")
    monkeypatch.setenv("MIN_TRADE_USD", "27.5")
    assert load_settings(str(cfg)).min_trade_usd == 27.5
    monkeypatch.setenv("PAUSED", "ture")
    with pytest.raises(ValueError, match="PAUSED"):
        load_settings(str(cfg))
    monkeypatch.delenv("PAUSED")
    cfg.write_text("live_confirm: yes-real-money\n")
    with pytest.raises(ValueError, match="environment variable"):
        load_settings(str(cfg))
