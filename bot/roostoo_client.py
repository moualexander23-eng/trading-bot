"""Thin, defensive client for the Roostoo mock-exchange REST API.

- HMAC-SHA256 request signing (RCL_TopLevelCheck endpoints)
- client-side rate limiting (exchange limit: 30 calls/min, we stay below)
- retries with exponential backoff on network / HTTP errors
- server-clock offset so timestamps stay inside the 60s validity window
- every call is logged with its success flag for the audit trail
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
from collections import deque
from decimal import ROUND_DOWN, Decimal

import requests

log = logging.getLogger("roostoo")
api_log = logging.getLogger("api")


class RateLimiter:
    """Sliding-window limiter: at most `max_calls` per `period` seconds."""

    def __init__(self, max_calls: int = 25, period: float = 60.0):
        self.max_calls, self.period = max_calls, period
        self.calls: deque[float] = deque()
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            while self.calls and now - self.calls[0] > self.period:
                self.calls.popleft()
            if len(self.calls) >= self.max_calls:
                sleep_for = self.period - (now - self.calls[0]) + 0.05
                time.sleep(max(sleep_for, 0))
                self.calls.popleft()
            self.calls.append(time.monotonic())


def format_decimal(value: float, decimals: int) -> str:
    """Round DOWN to `decimals` places and render without scientific notation."""
    q = Decimal(1).scaleb(-decimals) if decimals > 0 else Decimal(1)
    d = Decimal(str(value)).quantize(q, rounding=ROUND_DOWN)
    return format(d, "f")


class RoostooClient:
    def __init__(self, api_key: str, secret: str, base_url: str = "https://mock-api.roostoo.com",
                 max_calls_per_min: int = 25, timeout: float = 10.0, retries: int = 3):
        self.api_key, self.secret = api_key, secret.encode()
        self.base_url = base_url.rstrip("/")
        self.limiter = RateLimiter(max_calls_per_min, 60.0)
        self.timeout, self.retries = timeout, retries
        self.session = requests.Session()
        self.clock_offset_ms = 0

    # ---------- plumbing ----------
    def _timestamp(self) -> str:
        return str(int(time.time() * 1000) + self.clock_offset_ms)

    def sign(self, params: dict) -> tuple[dict, str]:
        total = "&".join(f"{k}={params[k]}" for k in sorted(params))
        sig = hmac.new(self.secret, total.encode(), hashlib.sha256).hexdigest()
        return {"RST-API-KEY": self.api_key, "MSG-SIGNATURE": sig}, total

    def _request(self, method: str, path: str, params: dict | None = None,
                 signed: bool = False, timestamped: bool = False, idempotent: bool = True) -> dict:
        """Order-creating calls pass idempotent=False: they are only retried when
        the connection was never established, so a timeout can't double-fill."""
        url = f"{self.base_url}{path}"
        last_err = None
        for attempt in range(self.retries + 1):
            p = dict(params or {})
            if signed or timestamped:
                p["timestamp"] = self._timestamp()
            headers = {}
            body = None
            if signed:
                headers, body = self.sign(p)
            self.limiter.wait()
            t0 = time.monotonic()
            try:
                if method == "GET":
                    r = self.session.get(url, params=p, headers=headers, timeout=self.timeout)
                else:
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                    r = self.session.post(url, data=body, headers=headers, timeout=self.timeout)
                ms = (time.monotonic() - t0) * 1000
                if r.status_code >= 500 or r.status_code == 429:
                    raise requests.HTTPError(f"HTTP {r.status_code}: {r.text[:200]}")
                r.raise_for_status()
                data = r.json()
                ok = data.get("Success", True) if isinstance(data, dict) else True
                api_log.info("%s %s ok=%s %.0fms err=%s", method, path, ok, ms,
                             data.get("ErrMsg", "") if isinstance(data, dict) else "")
                return data
            except (requests.RequestException, ValueError) as e:
                last_err = e
                api_log.warning("%s %s attempt=%d failed: %s", method, path, attempt + 1, e)
                if not idempotent and not isinstance(e, requests.ConnectTimeout):
                    break
                time.sleep(min(2 ** attempt, 10))
        return {"Success": False, "ErrMsg": f"request failed after retries: {last_err}"}

    # ---------- public ----------
    def server_time(self) -> dict:
        return self._request("GET", "/v3/serverTime")

    def sync_clock(self) -> None:
        t0 = time.time()
        st = self.server_time().get("ServerTime")
        if st:
            local = int((t0 + time.time()) / 2 * 1000)
            self.clock_offset_ms = int(st) - local
            log.info("clock offset vs server: %d ms", self.clock_offset_ms)

    def exchange_info(self) -> dict:
        return self._request("GET", "/v3/exchangeInfo")

    def ticker(self, pair: str | None = None) -> dict:
        return self._request("GET", "/v3/ticker", {"pair": pair} if pair else {}, timestamped=True)

    # ---------- signed ----------
    def balance(self) -> dict:
        return self._request("GET", "/v3/balance", signed=True)

    def pending_count(self) -> dict:
        return self._request("GET", "/v3/pending_count", signed=True)

    def place_order(self, pair: str, side: str, quantity: str, order_type: str = "MARKET",
                    price: str | None = None) -> dict:
        p = {"pair": pair, "side": side.upper(), "type": order_type.upper(), "quantity": quantity}
        if order_type.upper() == "LIMIT":
            p["price"] = price
        return self._request("POST", "/v3/place_order", p, signed=True, idempotent=False)

    def query_order(self, order_id: str | None = None, pair: str | None = None,
                    pending_only: bool | None = None) -> dict:
        p: dict = {}
        if order_id:
            p["order_id"] = str(order_id)
        else:
            if pair:
                p["pair"] = pair
            if pending_only is not None:
                p["pending_only"] = "TRUE" if pending_only else "FALSE"
        return self._request("POST", "/v3/query_order", p, signed=True)

    def cancel_order(self, order_id: str | None = None, pair: str | None = None) -> dict:
        p = {"order_id": str(order_id)} if order_id else ({"pair": pair} if pair else {})
        return self._request("POST", "/v3/cancel_order", p, signed=True)

    def short_open(self, pair: str, collateral: str) -> dict:
        return self._request("POST", "/v6/short_open", {"pair": pair, "collateral": collateral},
                             signed=True, idempotent=False)

    def short_close(self, pair: str, close_qty: str | None = None) -> dict:
        p = {"pair": pair}
        if close_qty is not None:
            p["close_qty"] = close_qty
        return self._request("POST", "/v6/short_close", p, signed=True, idempotent=False)

    def short_positions(self) -> dict:
        return self._request("GET", "/v6/short_positions", signed=True)
