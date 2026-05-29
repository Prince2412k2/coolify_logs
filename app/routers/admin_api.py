"""Admin write endpoints — restart, redeploy, audit log.

All routes:
  * require an ApiKey with `is_admin = True`
  * additionally require the target resource's project to be in the key's
    `allowed_projects` set (project-scoped admin)
  * write a row to admin_audit on every attempt (success OR failure)
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from docker.errors import APIError, NotFound
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import docker_client
from ..auth import check_admin_permission, get_api_key
from ..coolify_api import CoolifyAPI, CoolifyAPIError
from ..coolify_db import coolify_db
from ..database import get_db
from ..models import AdminAudit, ApiKey


router = APIRouter(prefix="/api/admin", tags=["admin"])


# ── helpers ───────────────────────────────────────────────────────────


def _resolve(resource_uuid: str):
    """(container_name, type, project_id) — same as the read-side resolver."""
    if not coolify_db.is_configured or not resource_uuid:
        return None, None, None
    for p in coolify_db.get_detailed_projects():
        pid = str(p.get("project_id", ""))
        for stage in p.get("stages", []):
            for s in stage.get("services", []):
                if s.get("uuid") == resource_uuid:
                    return (
                        s.get("container_name") or None,
                        s.get("type") or "application",
                        pid,
                    )
    return None, None, None


def _audit(
    db: Session,
    api_key: ApiKey,
    action: str,
    target: str,
    result: str,
    detail: Optional[str] = None,
) -> None:
    row = AdminAudit(
        ts=datetime.utcnow(),
        key_prefix=api_key.key[:8],
        key_name=api_key.name,
        action=action,
        target=target,
        result=result,
        detail=detail,
    )
    db.add(row)
    db.commit()
    # FIFO trim — keep last 5000 entries.
    cnt = db.query(AdminAudit).count()
    if cnt > 5000:
        excess = cnt - 5000
        oldest = (
            db.query(AdminAudit.id)
            .order_by(AdminAudit.id.asc())
            .limit(excess)
            .all()
        )
        ids = [r[0] for r in oldest]
        db.query(AdminAudit).filter(AdminAudit.id.in_(ids)).delete(
            synchronize_session=False
        )
        db.commit()


# ── routes ────────────────────────────────────────────────────────────


@router.post("/services/{resource_uuid}/restart")
def restart_service(
    resource_uuid: str,
    api_key: ApiKey = Depends(get_api_key),
    db: Session = Depends(get_db),
):
    container_name, _, project_id = _resolve(resource_uuid)
    check_admin_permission(api_key, project_id)
    if not container_name:
        _audit(db, api_key, "restart", resource_uuid, "error", "no container on this gateway")
        raise HTTPException(status_code=404, detail="Container not reachable on this gateway")

    try:
        cli = docker_client._client()
        cli.containers.get(container_name).restart(timeout=10)
    except NotFound:
        _audit(db, api_key, "restart", resource_uuid, "error", "container not found")
        raise HTTPException(status_code=404, detail="container not found")
    except APIError as e:
        _audit(db, api_key, "restart", resource_uuid, "error", str(e))
        raise HTTPException(status_code=502, detail=f"docker error: {e}")
    except docker_client.DockerUnavailable:
        _audit(db, api_key, "restart", resource_uuid, "error", "docker unavailable")
        raise HTTPException(status_code=503, detail="docker socket unavailable")

    _audit(db, api_key, "restart", resource_uuid, "ok", container_name)
    return {"ok": True, "action": "restart", "container_name": container_name}


@router.post("/services/{resource_uuid}/redeploy")
def redeploy_service(
    resource_uuid: str,
    force: bool = Query(False, description="force-rebuild (skip image cache)"),
    api_key: ApiKey = Depends(get_api_key),
    db: Session = Depends(get_db),
):
    _, resource_type, project_id = _resolve(resource_uuid)
    check_admin_permission(api_key, project_id)
    if resource_type != "application":
        _audit(db, api_key, "redeploy", resource_uuid, "error", "not an application")
        raise HTTPException(status_code=400, detail="redeploy only supported for applications")

    api = CoolifyAPI.instance()
    if not api.configured:
        _audit(db, api_key, "redeploy", resource_uuid, "error", "coolify api not configured")
        raise HTTPException(
            status_code=503,
            detail="Coolify API not configured on the gateway (COOLIFY_API_URL + COOLIFY_API_TOKEN)",
        )
    try:
        resp = api.redeploy_application(resource_uuid, force=force)
    except CoolifyAPIError as e:
        _audit(db, api_key, "redeploy", resource_uuid, "error", str(e))
        raise HTTPException(status_code=e.status_code or 502, detail=str(e))

    detail = f"force={force}"
    _audit(db, api_key, "redeploy", resource_uuid, "ok", detail)
    return {"ok": True, "action": "redeploy", "force": force, "coolify": resp}


@router.get("/audit")
def audit_log(
    limit: int = Query(50, ge=1, le=500),
    api_key: ApiKey = Depends(get_api_key),
    db: Session = Depends(get_db),
):
    # Audit log readable by any admin key (no project scope on read — admins
    # see all admin activity on the gateway).
    if not api_key.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    rows = (
        db.query(AdminAudit)
        .order_by(AdminAudit.id.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "ts": r.ts.isoformat() + "Z",
            "key_prefix": r.key_prefix,
            "key_name": r.key_name,
            "action": r.action,
            "target": r.target,
            "result": r.result,
            "detail": r.detail,
        }
        for r in rows
    ]
