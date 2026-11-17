"""HTTP client and TOTP helper shared by seed.py and verify.py.

Standard library only: the CI runner's Python, no dependencies.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.cookiejar
import json
import struct
import time
import urllib.error
import urllib.request

TOTP_STEP = 30


class ApiError(Exception):
    def __init__(self, method: str, path: str, status: int, body: str):
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


class Client:
    """One signed-in browser: a cookie jar and JSON requests."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method: str, path: str, body: object = None, expect: int | None = None):
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(req, timeout=60) as resp:
                status, raw = resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read().decode("utf-8", "replace")
        if expect is not None and status != expect:
            raise ApiError(method, path, status, raw)
        if expect is None and status >= 400:
            raise ApiError(method, path, status, raw)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def get(self, path: str):
        return self.request("GET", path)

    def post(self, path: str, body: object = None, expect: int | None = None):
        return self.request("POST", path, body if body is not None else {}, expect)

    def patch(self, path: str, body: object):
        return self.request("PATCH", path, body)

    def put(self, path: str, body: object):
        return self.request("PUT", path, body)


def totp(secret_b32: str, counter: int) -> str:
    """RFC 6238 with SHA-1, 6 digits, as authenticator apps and PerchTail use."""
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


class TotpDevice:
    """An authenticator for one account. PerchTail accepts each time step
    once per account, and the steps next to the current one, so every code
    comes from a step not used before: the current one, else the next one,
    else wait. `last_counter` is kept by the caller across runs."""

    def __init__(self, secret_b32: str, last_counter: int = -1):
        self.secret = secret_b32
        self.last_counter = last_counter

    def code(self) -> str:
        while True:
            now = int(time.time()) // TOTP_STEP
            counter = max(now, self.last_counter + 1)
            if counter <= now + 1:
                self.last_counter = counter
                return totp(self.secret, counter)
            time.sleep(TOTP_STEP - time.time() % TOTP_STEP + 0.5)
