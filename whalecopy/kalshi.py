"""Minimal Kalshi Trade API v2 client: RSA-PSS request signing, market data, orders.

Docs: https://docs.kalshi.com
"""
from __future__ import annotations

import base64
import math
import logging
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

log = logging.getLogger(__name__)


class KalshiError(RuntimeError):
    pass


def sign_message(private_key, timestamp_ms: str, method: str, path: str) -> str:
    """Signature over timestamp + METHOD + path (path starts with /trade-api/v2, no query)."""
    msg = f"{timestamp_ms}{method.upper()}{path}".encode()
    sig = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()


def dollars(value) -> float | None:
    """Kalshi returns prices as fixed-point dollar strings like '0.5600'."""
    if value is None or value == "":
        return None
    return float(value)


def price_str(p: float) -> str:
    """Format a price in dollars for order requests (whole cents, 4 decimals)."""
    cents = Decimal(str(p)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    return f"{cents:.4f}"


@dataclass
class Quote:
    ticker: str
    status: str
    yes_ask: float | None
    no_ask: float | None
    yes_bid: float | None
    no_bid: float | None
    result: str | None

    def ask(self, side: str) -> float | None:
        return self.yes_ask if side == "yes" else self.no_ask

    def bid(self, side: str) -> float | None:
        return self.yes_bid if side == "yes" else self.no_bid

    @property
    def tradable(self) -> bool:
        return self.status in ("active", "open")


def _price_field(m: dict, name: str) -> float | None:
    v = dollars(m.get(f"{name}_dollars"))
    if v is None and m.get(name) is not None:
        v = float(m[name]) / 100.0  # legacy integer-cent field
    if v is not None and v <= 0:
        return None  # 0 means "no orders on that side"
    return v


def quote_from_market(m: dict) -> Quote:
    return Quote(
        ticker=m["ticker"],
        status=m.get("status", ""),
        yes_ask=_price_field(m, "yes_ask"),
        no_ask=_price_field(m, "no_ask"),
        yes_bid=_price_field(m, "yes_bid"),
        no_bid=_price_field(m, "no_bid"),
        result=m.get("result") or None,
    )


class KalshiClient:
    def __init__(self, base_url: str, key_id: str = "", private_key_pem: bytes | None = None,
                 session: requests.Session | None = None, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.path_prefix = urlparse(self.base_url).path  # /trade-api/v2
        self.key_id = key_id
        self.private_key = (
            serialization.load_pem_private_key(private_key_pem, password=None)
            if private_key_pem else None
        )
        self.session = session or requests.Session()
        self.timeout = timeout

    # -- transport -----------------------------------------------------------
    def _headers(self, method: str, path: str, auth: bool) -> dict:
        h = {"Accept": "application/json"}
        if auth:
            if not (self.key_id and self.private_key):
                raise KalshiError("This call needs a Kalshi API key")
            ts = str(int(time.time() * 1000))
            h.update({
                "KALSHI-ACCESS-KEY": self.key_id,
                "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": sign_message(
                    self.private_key, ts, method, self.path_prefix + path),
            })
        return h

    def _request(self, method: str, path: str, *, params=None, json=None, auth=False,
                 retries: int = 3):
        url = self.base_url + path
        for attempt in range(retries + 1):
            try:
                r = self.session.request(method, url, params=params, json=json,
                                         headers=self._headers(method, path, auth),
                                         timeout=self.timeout)
            except requests.RequestException as e:
                if attempt == retries or method != "GET":
                    raise KalshiError(f"{method} {path} failed: {e}") from e
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 429 or (r.status_code >= 500 and method == "GET"):
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
            if r.status_code >= 400:
                raise KalshiError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else {}
        raise KalshiError(f"{method} {path} exhausted retries")

    # -- market data (public) --------------------------------------------------
    def iter_open_markets(self, page_size: int = 1000, max_pages: int = 400):
        cursor = None
        for _ in range(max_pages):
            params = {"status": "open", "limit": page_size}
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", "/markets", params=params)
            yield from data.get("markets", [])
            cursor = data.get("cursor")
            if not cursor:
                return
            time.sleep(0.1)  # stay well under rate limits

    def get_market(self, ticker: str) -> dict:
        return self._request("GET", f"/markets/{ticker}")["market"]

    def get_quote(self, ticker: str) -> Quote:
        return quote_from_market(self.get_market(ticker))

    # -- account (signed) --------------------------------------------------------
    def get_balance(self) -> float:
        data = self._request("GET", "/portfolio/balance", auth=True)
        if "balance_dollars" in data:
            return float(data["balance_dollars"])
        return float(data.get("balance", 0)) / 100.0

    def create_order(self, *, ticker: str, side: str, action: str, count: int,
                     limit_price: float, client_order_id: str,
                     time_in_force: str = "immediate_or_cancel") -> dict:
        if side not in ("yes", "no") or action not in ("buy", "sell"):
            raise ValueError("bad side/action")
        if count < 1:
            raise ValueError("count must be >= 1")
        body = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": int(count),
            "type": "limit",
            f"{side}_price_dollars": price_str(limit_price),
            "time_in_force": time_in_force,
            "client_order_id": client_order_id,
        }
        if action == "sell":
            body["reduce_only"] = True
        data = self._request("POST", "/portfolio/orders", json=body, auth=True, retries=0)
        return data.get("order") or data


def filled_count(order: dict) -> int:
    for key in ("fill_count", "fill_count_fp"):
        if order.get(key) not in (None, ""):
            return int(float(order[key]))
    initial = order.get("initial_count")
    remaining = order.get("remaining_count")
    if initial is not None and remaining is not None:
        return int(float(initial)) - int(float(remaining))
    return 0


def fees_usd(order: dict) -> float:
    total = 0.0
    for key in ("taker_fees", "maker_fees"):
        if order.get(f"{key}_dollars") not in (None, ""):
            total += float(order[f"{key}_dollars"])
        elif order.get(key) not in (None, ""):
            total += float(order[key]) / 100
    return total


def estimate_fee(count: int, price: float, rate: float) -> float:
    """Paper-mode fee estimate, rounded up to the cent (Kalshi-style rate * C * P * (1-P))."""
    return math.ceil(rate * count * price * (1 - price) * 100) / 100


def fill_cost_usd(order: dict, count: int, limit_price: float) -> float:
    """Best-effort cost of a BUY's fills, excluding fees; falls back to the limit price."""
    for key in ("taker_fill_cost_dollars",):
        if order.get(key) not in (None, ""):
            maker = order.get("maker_fill_cost_dollars") or 0
            return float(order[key]) + float(maker)
    if order.get("taker_fill_cost") is not None:
        return (float(order["taker_fill_cost"]) + float(order.get("maker_fill_cost") or 0)) / 100
    return count * limit_price
