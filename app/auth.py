from __future__ import annotations

import json
import os
import secrets
import time
from hmac import compare_digest
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from .models import ApiKey
from .database import get_db


ADMIN_COOKIE_NAME = "lg_admin"
ADMIN_SESSION_MAX_AGE_SECONDS = 8 * 60 * 60


bearer = HTTPBearer(auto_error=False)
basic = HTTPBasic(auto_error=False)


def admin_username() -> str:
    return os.getenv("ADMIN_USERNAME", "admin")


def admin_password() -> str:
    return os.getenv("ADMIN_PASSWORD", "changeme")


def _admin_serializer() -> URLSafeTimedSerializer:
    # Derive secret from admin password to avoid introducing extra env vars.
    secret = admin_password()
    return URLSafeTimedSerializer(secret_key=secret, salt="log-gateway-admin")


def set_admin_session(response, username: str) -> None:
    s = _admin_serializer()
    token = s.dumps({"u": username, "iat": int(time.time())})
    response.set_cookie(
        ADMIN_COOKIE_NAME,
        token,
        max_age=ADMIN_SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
    )


def clear_admin_session(response) -> None:
    response.delete_cookie(ADMIN_COOKIE_NAME)


def _get_admin_from_cookie(request) -> Optional[str]:
    # Handle both Request and WebSocket objects
    if hasattr(request, "cookies"):
        raw = request.cookies.get(ADMIN_COOKIE_NAME)
    else:
        raw = None

    if not raw:
        return None
    s = _admin_serializer()
    try:
        data = s.loads(raw, max_age=ADMIN_SESSION_MAX_AGE_SECONDS)
        u = data.get("u") if isinstance(data, dict) else None
        return str(u) if u else None
    except (BadSignature, SignatureExpired):
        return None


def require_admin(
    request: Request,
    creds: HTTPBasicCredentials = Depends(basic),
) -> str:
    u = _get_admin_from_cookie(request)
    if u:
        return u

    # Optional fallback to HTTP Basic.
    if (
        creds
        and compare_digest(creds.username or "", admin_username())
        and compare_digest(creds.password or "", admin_password())
    ):
        return creds.username

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin authentication required"
    )


def _parse_bearer(auth_header: str) -> Optional[str]:
    if not auth_header:
        return None
    parts = auth_header.split(" ", 1)
    if len(parts) != 2:
        return None
    scheme, token = parts[0].strip(), parts[1].strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def get_api_key(
    db: Session = Depends(get_db),
    creds: HTTPAuthorizationCredentials = Depends(bearer),
) -> ApiKey:
    token = creds.credentials if creds else None
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Bearer token"
        )
    key = db.get(ApiKey, token)
    if not key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key"
        )
    return key


def check_container_permission(api_key: ApiKey, container_name: str) -> None:
    allowed = set(api_key.allowed_list())
    if container_name not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key not allowed for container",
        )


def generate_api_key() -> str:
    # URL-safe token suitable for bearer auth.
    return secrets.token_urlsafe(32)


def json_dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True)
