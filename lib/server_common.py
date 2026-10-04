"""Shared helpers for the website's API endpoints: validation, rate limits, JSON replies, API-key handling."""
import hmac
import json
import os
import re
import sys
import threading
import time
from collections import defaultdict, deque
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import fantasy as F  # noqa: E402  (the engine lives in the project root)

# Keep a public deployment lighter and cheaper. Override with environment variables if you like.
F.WAIVER_POOL = int(os.environ.get("FANTASY_WAIVER_POOL", "30"))
F.BUY_LOW_COUNT = int(os.environ.get("FANTASY_BUY_LOW_COUNT", "5"))
F.W["learn"]["weeks"] = int(os.environ.get("FANTASY_LEARN_WEEKS", "3"))

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{2,40}$")
LEAGUE_RE = re.compile(r"^\d{6,25}$")
KEY_RE = re.compile(r"^sk-ant-[A-Za-z0-9_\-]{20,300}$")


class RateLimiter:
    """Sliding-window limiter. In memory, so on serverless hosts it is per warm instance: a speed bump against
    accidents and casual abuse, not a hard guarantee. Use a shared store (e.g. Upstash Redis) for strict limits."""

    def __init__(self):
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key, limit, window_seconds):
        now = time.time()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window_seconds:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self._hits) > 5000:  # keep memory bounded
                for k in [k for k, v in self._hits.items() if not v][:1000]:
                    self._hits.pop(k, None)
            return True


    def blocked(self, key, limit, window_seconds):
        """True if `key` has already used up `limit` hits in the window (does not record a hit)."""
        now = time.time()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window_seconds:
                q.popleft()
            return len(q) >= limit

    def record(self, key):
        with self._lock:
            self._hits[key].append(time.time())


LIMITER = RateLimiter()


def client_ip(h):
    fwd = h.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else h.headers.get("x-real-ip", "")) or h.client_address[0]


def query(h):
    return {k: v[0] for k, v in parse_qs(urlparse(h.path).query).items() if v}


def send_json(h, status, payload):
    body = json.dumps(payload).encode("utf-8")
    h.send_response(status)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.end_headers()
    h.wfile.write(body)


def pick_api_key(h):
    """Returns (key, mode, error) where error is None or (http_status, message).

    1. Owner mode: the site owner's browser sends X-Owner-Code. If it matches the OWNER_PASSCODE setting, the site's
       own ANTHROPIC_API_KEY is used. Nobody else can trigger it, and wrong guesses are rate limited.
    2. A visitor's own key (X-Anthropic-Key) is used for that visitor's request only.
    3. Optional public mode (ENABLE_SERVER_CLAUDE=1): the site's key for anyone, a couple of times per day each.
    """
    server_key = os.environ.get("ANTHROPIC_API_KEY", "")
    owner_code = (h.headers.get("x-owner-code") or "").strip()[:200]
    if owner_code:
        expected = os.environ.get("OWNER_PASSCODE", "")
        if not expected or not server_key:
            return "", "off", (400, "Owner mode is not set up on this site.")
        if LIMITER.blocked("badcode:" + client_ip(h), 8, 3600):  # checked BEFORE comparing, so guessing cannot continue
            return "", "off", (429, "Too many wrong passcodes. Try again in an hour.")
        if not hmac.compare_digest(owner_code.encode(), expected.encode()):
            LIMITER.record("badcode:" + client_ip(h))
            return "", "off", (403, "That owner passcode is not right.")
        if not LIMITER.allow("owner:" + client_ip(h), 200, 86400):  # a runaway loop should not drain the key
            return "", "off", (429, "Daily owner limit reached.")
        return server_key, "owner", None
    supplied = (h.headers.get("x-anthropic-key") or "").strip()
    if supplied:
        if not KEY_RE.match(supplied):
            return "", "off", (400, "That does not look like an Anthropic API key. It should start with sk-ant-.")
        return supplied, "your key", None
    if server_key and os.environ.get("ENABLE_SERVER_CLAUDE") == "1":
        per_day = int(os.environ.get("SERVER_CLAUDE_PER_DAY", "2"))
        if LIMITER.allow("claude:" + client_ip(h), per_day, 86400):
            return server_key, "server", None
        return "", "off (daily limit reached)", None
    return "", "off", None
