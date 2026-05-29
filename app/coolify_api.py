"""Thin client for Coolify's HTTP API.

Used only for *write* operations (redeploy, cancel). Read paths still go
through the Coolify Postgres directly because the API is incomplete for
those.

When the gateway runs on the same host as Coolify (the recommended setup),
point COOLIFY_API_URL at the internal Docker network address (e.g.
`http://coolify:8000`). That bypasses the reverse proxy, TLS termination,
and any external WAF — sub-millisecond latency and no User-Agent games.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx


LOG = logging.getLogger("log-gateway.coolify_api")


class CoolifyAPIError(Exception):
    """Raised when the Coolify API returns a non-2xx or is unreachable."""

    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class CoolifyAPI:
    """Singleton — lazily reads env on first use so config edits take effect on restart."""

    _instance: Optional["CoolifyAPI"] = None

    def __init__(self) -> None:
        self._url = (os.getenv("COOLIFY_API_URL", "") or "").rstrip("/")
        self._token = os.getenv("COOLIFY_API_TOKEN", "") or ""

    @classmethod
    def instance(cls) -> "CoolifyAPI":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def configured(self) -> bool:
        return bool(self._url and self._token)

    def _client(self) -> httpx.Client:
        if not self.configured:
            raise CoolifyAPIError(
                "Coolify API not configured. Set COOLIFY_API_URL + COOLIFY_API_TOKEN in .env",
                status_code=503,
            )
        return httpx.Client(
            base_url=self._url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "User-Agent": "log-gateway/admin",
            },
            timeout=15.0,
        )

    # ── application actions ─────────────────────────────────────────────

    def redeploy_application(self, uuid: str, force: bool = False) -> dict:
        """Trigger a redeploy of an application. Returns the Coolify response.

        Coolify endpoint: GET /api/v1/deploy?uuid=<uuid>[&force=true]
        """
        params = {"uuid": uuid}
        if force:
            params["force"] = "true"
        with self._client() as c:
            r = c.get("/api/v1/deploy", params=params)
            return _ensure_ok(r, action=f"redeploy {uuid}")

    def cancel_deployment(self, deployment_uuid: str) -> dict:
        """Cancel an in-progress deployment by its deployment_uuid."""
        with self._client() as c:
            r = c.delete(f"/api/v1/deployments/{deployment_uuid}")
            return _ensure_ok(r, action=f"cancel {deployment_uuid}")


def _ensure_ok(r: httpx.Response, action: str) -> dict:
    if r.status_code >= 400:
        try:
            body = r.json()
            detail = body.get("message") or body.get("error") or r.text
        except Exception:
            detail = r.text
        LOG.warning("coolify %s failed: %s %s", action, r.status_code, detail)
        raise CoolifyAPIError(
            f"coolify api error: {detail}",
            status_code=r.status_code,
        )
    try:
        return r.json()
    except Exception:
        return {"raw": r.text}
