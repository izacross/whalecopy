import base64
import json
import socket
import threading
import urllib.error
import urllib.request

from conftest import NOW, WALLET
from whalecopy import dashboard
from whalecopy.config import Settings
from whalecopy.kalshi import Quote
from whalecopy.store import Store

OTHER = "0x" + "b" * 40


def quote(bid):
    return Quote("T", "active", None, None, bid, bid, None)


def seed(store):
    # an open copy, a winner that settled, a loser the whale sold, and a skip
    store.log_event("copied", WALLET, "c1", title="Lakers vs. Celtics", outcome="Lakers",
                    ticker="KX-A", side="yes", count=50, cost=27.5, mode="paper")
    store.open_position("paper", "KX-A", "yes", 50, 27.5, WALLET, "c1", 0)
    won = store.open_position("paper", "KX-B", "yes", 40, 20.0, WALLET, "c2", 0)
    store.close_position(won, 40.0, status="settled")
    lost = store.open_position("paper", "KX-C", "no", 30, 15.0, OTHER, "c3", 1)
    store.close_position(lost, 9.0)
    store.log_event("skipped", WALLET, "c4", reason="cooldown", title="<b>x</b>", mode="paper")
    # a live position must never show on the paper dashboard
    store.open_position("live", "KX-L", "yes", 10, 5.0, WALLET, "c5", 0)


def test_summary_totals_and_traders(store):
    seed(store)
    d = dashboard.summarize(store, "paper", [WALLET], {WALLET: "lava-lava"},
                            quote=lambda t: quote(0.70), now=NOW)
    t = d["totals"]
    assert (t["open"], t["closed"], t["wins"], t["losses"]) == (1, 2, 1, 1)
    assert t["realized"] == 14.0            # +20 win, -6 loss
    assert t["unrealized"] == 7.5           # 50 * 0.70 - 27.5
    assert t["open_cost"] == 27.5
    by = {tr["wallet"]: tr for tr in d["traders"]}
    assert by[WALLET]["name"] == "lava-lava" and by[WALLET]["realized"] == 20.0
    assert by[OTHER]["following"] is False and by[OTHER]["realized"] == -6.0
    assert d["open"][0]["whale_title"] == "Lakers vs. Celtics / Lakers"
    assert d["skips"] == [("cooldown", 1)]
    assert [c[1] for c in d["curve"]] == [20.0, 14.0]  # running total per closed trade


def test_missing_quotes_dont_break_the_page(store):
    seed(store)

    def boom(_):
        raise RuntimeError("kalshi down")

    d = dashboard.summarize(store, "paper", [WALLET], {}, quote=boom, now=NOW)
    assert d["totals"]["unrealized"] is None and d["totals"]["unmarked"] == 1
    page = dashboard.render(d)
    assert "no Kalshi price right now" in page


def test_render_escapes_market_text(store):
    seed(store)
    page = dashboard.render(dashboard.summarize(store, "paper", [WALLET], {}, now=NOW))
    assert "<b>x</b>" not in page and "&lt;b&gt;x&lt;/b&gt;" in page
    assert "Paper mode" in page and "KX-L" not in page


def test_auth():
    hdr = lambda up: "Basic " + base64.b64encode(up.encode()).decode()  # noqa: E731
    assert dashboard.check_auth(hdr("izac:s3cret-pass"), "s3cret-pass")
    assert dashboard.check_auth(hdr(":s3cret-pass"), "s3cret-pass")
    assert not dashboard.check_auth(hdr("izac:wrong"), "s3cret-pass")
    assert not dashboard.check_auth(hdr("izac:"), "")
    assert not dashboard.check_auth(None, "s3cret-pass")
    assert not dashboard.check_auth("Basic !!!", "s3cret-pass")


def test_wallet_names_from_setting_and_csv(tmp_path):
    (tmp_path / "candidates.csv").write_text(f"wallet,name\n{WALLET},from-csv\n{OTHER},b\n")
    s = Settings(data_dir=str(tmp_path), wallet_names=[f"{WALLET.upper()}=lava-lava"])
    assert dashboard.wallet_names(s) == {WALLET: "lava-lava", OTHER: "b"}


def test_quote_cache_reuses_and_survives_errors():
    calls = []
    clock = [0.0]

    def fetch(t):
        calls.append(t)
        if t == "bad":
            raise RuntimeError("x")
        return quote(0.5)

    c = dashboard.QuoteCache(fetch, ttl=60, clock=lambda: clock[0])
    assert c("a").yes_bid == 0.5 and c("a").yes_bid == 0.5 and calls == ["a"]
    assert c("bad") is None
    clock[0] = 61
    c("a")
    assert calls == ["a", "bad", "a"]


def test_server_requires_password_and_is_read_only(tmp_path):
    s = Settings(data_dir=str(tmp_path), wallets=[WALLET], dashboard_password="s3cret-pass",
                 dashboard_port=0)
    seed(Store(s.db_path, clock=lambda: NOW))
    server = dashboard.start(s, quote=lambda t: quote(0.6))
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        try:
            urllib.request.urlopen(url + "/")
            raise AssertionError("expected 401")
        except urllib.error.HTTPError as e:
            assert e.code == 401
        auth = "Basic " + base64.b64encode(b"izac:s3cret-pass").decode()
        req = urllib.request.Request(url + "/api/summary", headers={"Authorization": auth})
        d = json.loads(urllib.request.urlopen(req).read())
        assert d["totals"]["open"] == 1
        page = urllib.request.urlopen(urllib.request.Request(
            url + "/", headers={"Authorization": auth})).read().decode()
        assert "By trader" in page
        assert urllib.request.urlopen(url + "/healthz").read() == b"ok"
        port = server.server_address[1]
        if server.address_family == socket.AF_INET6:  # skipped where IPv6 is unavailable
            assert urllib.request.urlopen(f"http://[::1]:{port}/healthz").read() == b"ok"
    finally:
        server.shutdown()
        server.server_close()


def test_readonly_store_cannot_write(tmp_path):
    Store(tmp_path / "x.db").set("k", 1)
    ro = Store(tmp_path / "x.db", readonly=True)
    assert ro.get("k") == 1
    try:
        ro.set("k", 2)
        raise AssertionError("write should fail")
    except Exception as e:
        assert "readonly" in str(e)


def test_short_dashboard_password_rejected():
    import pytest
    with pytest.raises(ValueError):
        Settings(dashboard_password="short").validate()
