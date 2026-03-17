from __future__ import annotations

import json
import re
from typing import Optional

from docker.errors import APIError, NotFound
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from sqlalchemy.orm import Session

from .. import coolify_db
from .. import docker_client
from .. import rate_limit
from ..auth import check_container_permission, get_api_key
from ..database import get_db
from ..models import ApiKey


CONTAINER_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


router = APIRouter(prefix="/api", tags=["api"])


def _validate_container_name(name: str) -> str:
    if not name or not CONTAINER_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="Invalid container name")
    return name


@router.get("/containers")
def containers(
    api_key: ApiKey = Depends(get_api_key),
    db: Session = Depends(get_db),
):
    # db is intentionally present to ensure a DB session exists per request.
    _ = db
    try:
        all_running = [c.as_dict() for c in docker_client.list_containers()]
    except docker_client.DockerUnavailable:
        raise HTTPException(status_code=503, detail="Docker socket unavailable")

    allowed = set(api_key.allowed_list())
    safe = [c for c in all_running if c.get("name") in allowed]
    return safe


@router.get("/projects")
def projects(
    api_key: ApiKey = Depends(get_api_key),
):
    if not coolify_db.coolify_db.is_configured:
        raise HTTPException(status_code=503, detail="Coolify DB not configured")

    allowed = set(api_key.allowed_list())
    results = coolify_db.coolify_db.get_detailed_projects()

    # Filter projects to only include allowed containers
    filtered_projects = []
    for p in results:
        filtered_stages = []
        for stage in p.get("stages", []):
            filtered_services = [
                s for s in stage.get("services", []) 
                if s.get("container_name") in allowed
            ]
            if filtered_services:
                stage_copy = stage.copy()
                stage_copy["services"] = filtered_services
                filtered_stages.append(stage_copy)
        
        if filtered_stages:
            p_copy = p.copy()
            p_copy["stages"] = filtered_stages
            filtered_projects.append(p_copy)

    return filtered_projects


async def _ws_send_error(ws: WebSocket, message: str, code: int = 4401) -> None:
    try:
        await ws.send_text(json.dumps({"type": "error", "message": message}))
    finally:
        await ws.close(code=code)


def _ws_bearer_from_headers(ws: WebSocket) -> Optional[str]:
    raw = ws.headers.get("authorization")
    if not raw:
        return None
    parts = raw.split(" ", 1)
    if len(parts) != 2:
        return None
    if parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


@router.get("/logs/{container_name}")
async def logs_http_redirect(container_name: str):
    """Fallback for when WebSocket handshake fails or is downgraded by proxy."""
    return {"error": "WebSocket connection required for logs", "path": f"/api/logs/{container_name}"}


@router.websocket("/logs/{container_name}")
async def logs_ws(websocket: WebSocket, container_name: str, tail: Optional[int] = None):
    await websocket.accept()

    if rate_limit.enabled():
        peer = websocket.client.host if websocket.client else None
        ip = rate_limit.client_ip(peer, dict(websocket.headers))
        res = websocket.app.state.api_limiter.allow(ip)
        if not res.allowed:
            await _ws_send_error(websocket, "Rate limit exceeded", code=4429)
            return

    try:
        _validate_container_name(container_name)
    except HTTPException as e:
        await _ws_send_error(websocket, str(e.detail), code=4400)
        return

    token = _ws_bearer_from_headers(websocket)
    auth_tail = None
    if not token:
        # Browser-friendly auth: expect first message with token.
        try:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            if isinstance(msg, dict) and msg.get("type") == "auth":
                token = str(msg.get("token") or "").strip() or None
                if msg.get("tail") is not None:
                    auth_tail = int(msg.get("tail"))
        except WebSocketDisconnect:
            return
        except Exception:
            token = None

    if not token:
        await _ws_send_error(websocket, "Missing API key", code=4401)
        return

    # Validate token against DB on every WS connect.
    from ..models import ApiKey as ApiKeyModel

    SessionLocal = websocket.app.state.SessionLocal
    db = SessionLocal()
    try:
        api_key = db.get(ApiKeyModel, token)
        if not api_key:
            await _ws_send_error(websocket, "Invalid API key", code=4401)
            return

        # Get actual container to resolve name/ID and check permissions
        try:
            container_obj = docker_client._client().containers.get(container_name)
            actual_name = container_obj.name.lstrip("/")
        except (NotFound, docker_client.DockerUnavailable):
            await _ws_send_error(websocket, "Container not found", code=4404)
            return
        except Exception:
            await _ws_send_error(websocket, "Internal server error", code=4511)
            return

        try:
            check_container_permission(api_key, actual_name)
        except HTTPException as e:
            await _ws_send_error(websocket, str(e.detail), code=4403)
            return

        effective_tail = int(auth_tail if auth_tail is not None else tail)
        try:
            async for line in docker_client.stream_logs(
                actual_name, tail=effective_tail
            ):
                await websocket.send_text(json.dumps({"type": "log", "line": line}))
        except NotFound:
            await _ws_send_error(websocket, "Container not found", code=4404)
        except docker_client.DockerUnavailable:
            await _ws_send_error(websocket, "Docker socket unavailable", code=4503)
        except APIError:
            await _ws_send_error(websocket, "Docker API error", code=4502)
        except WebSocketDisconnect:
            return
        except Exception:
            await _ws_send_error(websocket, "Internal server error", code=4511)
    finally:
        db.close()
