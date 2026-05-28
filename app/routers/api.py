from __future__ import annotations

import json
import re
import asyncio
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

from ..coolify_db import coolify_db
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
    if not coolify_db.is_configured:
        return []

    allowed = set(api_key.allowed_list())
    results = coolify_db.get_detailed_projects()

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


def _resolve_resource(resource_uuid: str):
    """Return (container_name, resource_type) for a known UUID, or (None, None)."""
    if not coolify_db.is_configured or not resource_uuid:
        return None, None
    for p in coolify_db.get_detailed_projects():
        for stage in p.get("stages", []):
            for s in stage.get("services", []):
                if s.get("uuid") == resource_uuid:
                    return s.get("container_name") or None, s.get("type") or "application"
    return None, None


def _scope_check(resource_uuid: str, api_key: ApiKey):
    """Return (container_name, type) if the caller may see this resource;
    raise 404 otherwise (404 — not 403 — so existence doesn't leak)."""
    container_name, resource_type = _resolve_resource(resource_uuid)
    if not container_name or container_name not in set(api_key.allowed_list()):
        raise HTTPException(status_code=404, detail="Not found")
    return container_name, resource_type


@router.get("/services/{resource_uuid}/deployments")
def deployments(
    resource_uuid: str,
    api_key: ApiKey = Depends(get_api_key),
):
    _, resource_type = _scope_check(resource_uuid, api_key)
    if resource_type != "application":
        return []
    return coolify_db.get_deployments(resource_uuid)


@router.get("/services/{resource_uuid}/build-log")
def build_log(
    resource_uuid: str,
    api_key: ApiKey = Depends(get_api_key),
):
    _, resource_type = _scope_check(resource_uuid, api_key)
    if resource_type != "application":
        return {"lines": [], "status": "", "deployment_uuid": ""}
    return coolify_db.get_build_log(resource_uuid)


@router.get("/services/{resource_uuid}/config")
def service_config(
    resource_uuid: str,
    api_key: ApiKey = Depends(get_api_key),
):
    _, resource_type = _scope_check(resource_uuid, api_key)
    if resource_type == "application":
        return coolify_db.get_application_config(resource_uuid)
    if resource_type == "service":
        return coolify_db.get_service_config(resource_uuid)
    return {}


@router.get("/services/{resource_uuid}/env")
def env_vars(
    resource_uuid: str,
    api_key: ApiKey = Depends(get_api_key),
):
    _, resource_type = _scope_check(resource_uuid, api_key)
    return coolify_db.get_environment_variables(resource_uuid, resource_type or "")


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

    token = _ws_bearer_from_headers(websocket)
    if not token:
        token = websocket.query_params.get("token")

    await websocket.accept()

    if not token:
        try:
            # If the client didn't provide Authorization/token in the handshake,
            # expect an immediate first-message auth. Don't leave sockets hanging.
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=3.0)
            msg = json.loads(raw)
            if isinstance(msg, dict) and msg.get("type") == "auth":
                token = str(msg.get("token") or "").strip() or None
                if msg.get("tail") is not None:
                    tail_val = int(msg.get("tail"))
        except asyncio.TimeoutError:
            token = None
        except WebSocketDisconnect:
            return
        except Exception:
            token = None

    if not token:
        await _ws_send_error(websocket, "Missing API key", code=4401)
        return


    # Authorise once up front, then release the DB connection. The WS can live
    # for hours; holding a pool slot for that long exhausts the pool under load.
    SessionLocal = websocket.app.state.SessionLocal
    db = SessionLocal()
    try:
        from ..models import ApiKey as ApiKeyModel
        api_key = db.get(ApiKeyModel, token)
        if not api_key:
            await _ws_send_error(websocket, "Invalid API key", code=4401)
            return
        # Snapshot the permission set so we don't need the ORM object after close.
        allowed_set = set(api_key.allowed_list())
    finally:
        db.close()

    # Resolve provided identifier to an actual Docker container name.
    try:
        container_obj = docker_client._client().containers.get(container_name)
        actual_name = container_obj.name.lstrip("/")
    except Exception:
        await _ws_send_error(websocket, "Container not found", code=4404)
        return

    if actual_name not in allowed_set:
        await _ws_send_error(websocket, "API key not allowed for container", code=4403)
        return

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
