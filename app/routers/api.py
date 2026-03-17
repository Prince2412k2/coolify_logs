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


CONTAINER_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


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





@router.websocket("/logs/{container_name}/ping")
async def logs_ping_ws(websocket: WebSocket, container_name: str):
    await websocket.accept()
    await websocket.send_text(json.dumps({"type": "pong", "container": container_name}))
    await websocket.close()


@router.websocket("/logs/{container_name}")
async def logs_ws(websocket: WebSocket, container_name: str):
    # Retrieve tail from query params manually
    tail_val = 100
    try:
        q_tail = websocket.query_params.get("tail")
        if q_tail:
            tail_val = int(q_tail)
    except (ValueError, TypeError):
        pass

    # validate container
    try:
        _validate_container_name(container_name)
    except HTTPException as e:
        await websocket.close(code=4400)
        return

    # Check token from headers or query params
    token = _ws_bearer_from_headers(websocket)
    if not token:
        token = websocket.query_params.get("token")

    if not token:
        await websocket.close(code=4401)
        return

    SessionLocal = websocket.app.state.SessionLocal
    db = SessionLocal()

    try:
        from ..models import ApiKey as ApiKeyModel
        api_key = db.get(ApiKeyModel, token)
        if not api_key:
            await websocket.close(code=4401)
            return

        # Get actual container to resolve name/ID 
        try:
            container_obj = docker_client._client().containers.get(container_name)
            actual_name = container_obj.name.lstrip("/")
        except Exception:
            await websocket.close(code=4404)
            return

        try:
            check_container_permission(api_key, actual_name)
        except Exception:
            await websocket.close(code=4403)
            return

        # ACCEPT ONLY AFTER AUTH AND PERMISSION CHECKS
        await websocket.accept()

        try:
            async for line in docker_client.stream_logs(actual_name, tail=tail_val):
                await websocket.send_text(json.dumps({"type": "log", "line": line}))
        except NotFound:
            await websocket.close(code=4404)
        except docker_client.DockerUnavailable:
            await websocket.close(code=1011)
        except APIError:
            await websocket.close(code=1011)
        except WebSocketDisconnect:
            pass
        except Exception:
            await websocket.close(code=1011)
    finally:
        db.close()
