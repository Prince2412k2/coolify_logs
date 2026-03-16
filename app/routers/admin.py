from __future__ import annotations

import json
from typing import List

from docker.errors import NotFound
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import docker_client
from .. import rate_limit
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
            {"request": request, "error": "Invalid credentials"},
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


@router.get("", response_class=HTMLResponse)
def admin_index(request: Request, admin_user: str = Depends(require_admin)):
    try:
        containers = [c.as_dict() for c in docker_client.list_containers()]
    except docker_client.DockerUnavailable:
        containers = []
        docker_error = "Docker socket unavailable"
    else:
        docker_error = None

    return _templates(request).TemplateResponse(
        "admin_index.html",
        {
            "request": request,
            "admin_user": admin_user,
            "containers": containers,
            "docker_error": docker_error,
        },
    )


def _render_keys_partial(request: Request, db: Session):
    keys = db.query(ApiKey).order_by(ApiKey.created_at.desc()).all()
    try:
        running = [c.as_dict() for c in docker_client.list_containers()]
    except docker_client.DockerUnavailable:
        running = []
    running_names = [c.get("name") for c in running if c.get("name")]
    return _templates(request).TemplateResponse(
        "admin_keys_partial.html",
        {
            "request": request,
            "keys": keys,
            "running_names": running_names,
        },
    )


@router.get("/keys", response_class=HTMLResponse)
def keys_get(
    request: Request,
    admin_user: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin_user
    keys = db.query(ApiKey).order_by(ApiKey.created_at.desc()).all()
    try:
        running = [c.as_dict() for c in docker_client.list_containers()]
    except docker_client.DockerUnavailable:
        running = []
    running_names = [c.get("name") for c in running if c.get("name")]
    return _templates(request).TemplateResponse(
        "admin_keys.html",
        {
            "request": request,
            "keys": keys,
            "running_names": running_names,
        },
    )


@router.post("/keys/create", response_class=HTMLResponse)
def keys_create(
    request: Request,
    name: str = Form(""),
    allowed_containers: List[str] = Form(default=[]),
    admin_user: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin_user
    name = (name or "").strip() or "Unnamed"
    k = generate_api_key()
    row = ApiKey(
        key=k,
        name=name,
        allowed_containers=json.dumps([str(x) for x in allowed_containers]),
    )
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
    allowed_containers: List[str] = Form(default=[]),
    admin_user: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ = admin_user
    row = db.get(ApiKey, key)
    if not row:
        raise HTTPException(status_code=404, detail="Key not found")
    row.allowed_containers = json.dumps([str(x) for x in allowed_containers])
    db.commit()
    return _render_keys_partial(request, db)
