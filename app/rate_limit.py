from __future__ import annotations

import ipaddress
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple


def _env_bool(name: str, default: bool) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def enabled() -> bool:
    return _env_bool("RATE_LIMIT_ENABLED", True)


def trust_proxy_headers() -> bool:
    # Only trusts XFF / X-Real-IP when the direct peer is a private IP.
    return _env_bool("TRUST_PROXY_HEADERS", True)


def _is_private_ip(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
        return bool(a.is_private or a.is_loopback)
    except Exception:
        return False


def client_ip(peer_ip: Optional[str], headers: dict) -> str:
    peer_ip = peer_ip or ""
    if trust_proxy_headers() and peer_ip and _is_private_ip(peer_ip):
        xff = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
        if isinstance(xff, str) and xff.strip():
            # First IP is the client.
            first = xff.split(",", 1)[0].strip()
            if first:
                return first
        xri = headers.get("x-real-ip") or headers.get("X-Real-IP")
        if isinstance(xri, str) and xri.strip():
            return xri.strip()
    return peer_ip or "unknown"


@dataclass
class RateLimitResult:
    allowed: bool
    retry_after_seconds: int


class TokenBucketLimiter:
    def __init__(self, rate_per_minute: int, burst: int):
        rpm = max(1, int(rate_per_minute))
        self.capacity = max(1, int(burst))
        self.rate_per_second = rpm / 60.0
        self._lock = threading.Lock()
        # ip -> (tokens, last_refill)
        self._state: dict[str, tuple[float, float]] = {}

    def allow(self, ip: str) -> RateLimitResult:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._state.get(ip, (float(self.capacity), now))
            # Refill.
            elapsed = max(0.0, now - last)
            tokens = min(float(self.capacity), tokens + elapsed * self.rate_per_second)
            if tokens >= 1.0:
                tokens -= 1.0
                self._state[ip] = (tokens, now)
                return RateLimitResult(True, 0)

            # How long until 1 token is available?
            need = 1.0 - tokens
            retry = int(max(1.0, need / self.rate_per_second))
            self._state[ip] = (tokens, now)
            return RateLimitResult(False, retry)


def api_limiter() -> TokenBucketLimiter:
    # Defaults tuned for log streaming + UI polling without being too permissive.
    rpm = _env_int("RATE_LIMIT_PER_MINUTE", 120)
    burst = _env_int("RATE_LIMIT_BURST", 60)
    return TokenBucketLimiter(rate_per_minute=rpm, burst=burst)


def admin_login_limiter() -> TokenBucketLimiter:
    rpm = _env_int("RATE_LIMIT_ADMIN_LOGIN_PER_MINUTE", 20)
    burst = _env_int("RATE_LIMIT_ADMIN_LOGIN_BURST", 10)
    return TokenBucketLimiter(rate_per_minute=rpm, burst=burst)
