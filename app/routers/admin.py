from __future__ import annotations

import json
from typing import List

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..coolify_db import coolify_db
from .. import docker_client
from ..auth import (
    ADMIN_SESSION_MAX_AGE_SECONDS,
    clear_admin_session,
    generate_api_key,
    require_admin,
    set_admin_session,
)
from ..database import get_db
from ..models import ApiKey


router = APIRouter(prefix="/admin", tags=["admin"])


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


# ── login / logout ──────────────────────────────────────────────────────


@router.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    return _templates(request).TemplateResponse(
        "admin_login.html",
        {"request": request, "max_age": ADMIN_SESSION_MAX_AGE_SECONDS},
        status_code=200,
    )


@router.post("/login")
def login_post(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
):
    import os
    from hmac import compare_digest

    admin_u = os.getenv("ADMIN_USERNAME", "admin")
    admin_p = os.getenv("ADMIN_PASSWORD", "changeme")
    if not (compare_digest(username, admin_u) and compare_digest(password, admin_p)):
        return _templates(request).TemplateResponse(
            "admin_login.html",
            {
                "request": request,
                "error": "Invalid credentials",
                "max_age": ADMIN_SESSION_MAX_AGE_SECONDS,
            },
            status_code=401,
        )
    resp = RedirectResponse(url="/admin", status_code=303)
    set_admin_session(resp, username)
    return resp


@router.post("/logout")
def logout_post(request: Request):
    _ = request
    resp = RedirectResponse(url="/admin/login", status_code=303)
    clear_admin_session(resp)
    return resp


# ── dashboard (existing — projects + others) ────────────────────────────


@router.get("", response_class=HTMLResponse)
def admin_index(request: Request, admin_user: str = Depends(require_admin)):
    try:
        projects = coolify_db.get_detailed_projects()
        all_containers = [c.as_dict() for c in docker_client.list_containers()]
        in_projects = set()
        for p in projects:
            for stage in p.get("stages", []):
                for s in stage.get("services", []):
                    if s.get("container_name"):
                        in_projects.add(s.get("container_name"))
                    if s.get("container_id"):
                        in_projects.add(s.get("container_id"))
        others = [
            c for c in all_containers
            if c.get("name") not in in_projects and c.get("id", "")[:12] not in in_projects
        ]
        docker_error = None
    except docker_client.DockerUnavailable:
        projects = []
        others = []
        docker_error = "Docker socket unavailable"
    except Exception as e:
        projects = []
        others = []
        docker_error = f"Error: {e}"

    return _templates(request).TemplateResponse(
        "admin_index.html",
        {
            "request": request,
            "admin_user": admin_user,
            "projects": projects,
            "others": others,
            "docker_error": docker_error,
        },
    )


# ── keys page ───────────────────────────────────────────────────────────


def _project_context() -> dict:
    """Build the shared template context for the keys page + partial."""
    try:
        projects = coolify_db.get_detailed_projects()
    except Exception:
        projects = []
    return {
        "projects": projects,
        "projects_by_id": {str(p.get("project_id")): p for p in projects},
    }


def _render_keys_partial(request: Request, db: Session):
    keys = db.query(ApiKey).order_by(ApiKey.created_at.desc()).all()
    ctx = {"request": request, "keys": keys, **_project_context()}
    return _templates(request).TemplateResponse("admin_keys_partial.html", ctx)


@router.get("/keys", response_class=HTMLResponse)
def keys_get(
    request: Request,
    admin_user: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin_user
    keys = db.query(ApiKey).order_by(ApiKey.created_at.desc()).all()
    ctx = {"request": request, "keys": keys, **_project_context()}
    return _templates(request).TemplateResponse("admin_keys.html", ctx)


def _normalize_project_ids(items: List[str]) -> List[str]:
    """Strip blanks, dedupe, sort. Returns canonical list."""
    seen = []
    for x in items:
        x = str(x).strip()
        if x and x not in seen:
            seen.append(x)
    return sorted(seen)


def _validate_project_ids(items: List[str]) -> tuple[List[str], str | None]:
    """Validate that every id refers to a real project. Returns (clean, err)."""
    ids = _normalize_project_ids(items)
    if not ids:
        return ids, "Keys must have at least one project assigned."
    known = {str(p.get("project_id")) for p in coolify_db.get_detailed_projects()}
    bogus = [x for x in ids if x not in known]
    if bogus:
        return ids, f"Unknown project id(s): {', '.join(bogus)}"
    return ids, None


@router.post("/keys/create", response_class=HTMLResponse)
def keys_create(
    request: Request,
    name: str = Form(""),
    allowed_projects: List[str] = Form(default=[]),
    admin_user: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin_user
    name = (name or "").strip() or "Unnamed"
    clean, err = _validate_project_ids(allowed_projects)
    if err:
        # 422 + same partial — htmx won't swap on non-2xx by default, the JS
        # form-side validator usually catches this first.
        raise HTTPException(status_code=422, detail=err)
    row = ApiKey(key=generate_api_key(), name=name, allowed_projects=json.dumps(clean))
    db.add(row)
    db.commit()
    return _render_keys_partial(request, db)


@router.post("/keys/{key}/delete", response_class=HTMLResponse)
def keys_delete(
    request: Request,
    key: str,
    admin_user: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin_user
    row = db.get(ApiKey, key)
    if row:
        db.delete(row)
        db.commit()
    return _render_keys_partial(request, db)


@router.post("/keys/{key}/update", response_class=HTMLResponse)
def keys_update(
    request: Request,
    key: str,
    allowed_projects: List[str] = Form(default=[]),
    admin_user: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin_user
    row = db.get(ApiKey, key)
    if not row:
        raise HTTPException(status_code=404, detail="Key not found")
    clean, err = _validate_project_ids(allowed_projects)
    if err:
        raise HTTPException(status_code=422, detail=err)
    row.set_allowed_projects(clean)
    db.commit()
    return _render_keys_partial(request, db)
