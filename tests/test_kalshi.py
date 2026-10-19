import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from whalecopy.kalshi import KalshiClient, filled_count, price_str, quote_from_market


def make_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    return key, pem


class RecordingSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append(dict(method=method, url=url, params=params, json=json, headers=headers))

        class R:
            status_code = 200
            content = b"x"

            def json(_):
                return self.payload
            text = ""
        return R()


def test_signature_covers_full_path_and_verifies():
    key, pem = make_key()
    sess = RecordingSession({"balance": 12345})
    c = KalshiClient("https://demo-api.kalshi.co/trade-api/v2", "kid", pem, session=sess)
    assert c.get_balance() == 123.45
    h = sess.calls[0]["headers"]
    assert h["KALSHI-ACCESS-KEY"] == "kid"
    msg = f"{h['KALSHI-ACCESS-TIMESTAMP']}GET/trade-api/v2/portfolio/balance".encode()
    key.public_key().verify(
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256())  # raises if invalid


def test_order_body():
    _, pem = make_key()
    sess = RecordingSession({"order": {"fill_count": 3}})
    c = KalshiClient("https://api.elections.kalshi.com/trade-api/v2", "kid", pem, session=sess)
    c.create_order(ticker="T", side="no", action="buy", count=3, limit_price=0.587,
                   client_order_id="c1")
    body = sess.calls[0]["json"]
    assert body["no_price_dollars"] == "0.5800"   # rounds down to whole cents
    assert body["count"] == 3 and body["side"] == "no" and body["action"] == "buy"
    assert body["time_in_force"] == "immediate_or_cancel"
    assert "reduce_only" not in body


def test_public_calls_are_unsigned():
    sess = RecordingSession({"market": {"ticker": "T", "status": "active",
                                        "yes_ask_dollars": "0.4100"}})
    c = KalshiClient("https://api.elections.kalshi.com/trade-api/v2", session=sess)
    q = c.get_quote("T")
    assert q.yes_ask == 0.41 and q.no_ask is None
    assert "KALSHI-ACCESS-KEY" not in sess.calls[0]["headers"]


def test_open_markets_exclude_combos_and_follow_cursor():
    class Paged(RecordingSession):
        def request(self, method, url, params=None, **kw):
            self.calls.append(dict(params=dict(params)))
            page = {"markets": [{"ticker": f"M{len(self.calls)}"}],
                    "cursor": "next" if len(self.calls) == 1 else ""}

            class R:
                status_code = 200
                content = b"x"
                text = ""

                def json(_):
                    return page
            return R()

    sess = Paged(None)
    c = KalshiClient("https://api.elections.kalshi.com/trade-api/v2", session=sess)
    assert [m["ticker"] for m in c.iter_open_markets()] == ["M1", "M2"]
    assert all(call["params"]["mve_filter"] == "exclude" for call in sess.calls)
    assert sess.calls[1]["params"]["cursor"] == "next"


def test_helpers():
    assert price_str(0.5) == "0.5000"
    assert quote_from_market({"ticker": "T", "yes_ask": 55, "status": "active"}).yes_ask == 0.55
    assert quote_from_market({"ticker": "T", "yes_ask_dollars": "0.0000"}).yes_ask is None
    assert filled_count({"fill_count": 4}) == 4
    assert filled_count({"initial_count": 5, "remaining_count": 2}) == 3
