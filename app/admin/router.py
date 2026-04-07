from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, Cookie, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.template import Template
from app.models.report import Report
from app.services.otp_service import create_otp, get_otp_by_browser_token, expire_old_otps
from app.services.jwt_service import create_access_token, decode_access_token
from app.services.template_validator import validate_template
from app.config import settings

import pathlib

TEMPLATES_DIR = pathlib.Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/admin", tags=["admin"])

DbDep = Annotated[AsyncSession, Depends(get_db)]


def _get_admin_user(request: Request) -> dict | None:
    token = request.cookies.get("admin_token")
    if not token:
        return None
    return decode_access_token(token)


def _require_admin(request: Request) -> dict:
    user = _get_admin_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_index(request: Request):
    user = _get_admin_user(request)
    if user is None:
        return RedirectResponse(url="/admin/login", status_code=302)
    return RedirectResponse(url="/admin/dashboard", status_code=302)


@router.get("/login", response_class=HTMLResponse)
async def admin_login_page(request: Request, db: DbDep):
    await expire_old_otps(db)
    otp = await create_otp(db)
    await db.commit()
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "otp_code": otp.otp_code,
            "browser_token": otp.browser_token,
            "poll_interval": settings.ADMIN_CHAT_POLL_INTERVAL,
        },
    )


@router.get("/login/poll")
async def admin_login_poll(token: str, db: DbDep, response: Response):
    otp = await get_otp_by_browser_token(db, token)
    if otp is None:
        return JSONResponse({"status": "not_found"}, status_code=404)

    await expire_old_otps(db)
    await db.commit()

    # Re-fetch after potential expiry update
    otp = await get_otp_by_browser_token(db, token)
    if otp is None or otp.status == "expired":
        return JSONResponse({"status": "expired"})

    if otp.status == "pending":
        return JSONResponse({"status": "pending"})

    # verified
    jwt_token = create_access_token(
        {"sub": str(otp.telegram_user_id), "tid": otp.telegram_user_id}
    )
    resp = JSONResponse({"status": "verified", "redirect": "/admin/dashboard"})
    resp.set_cookie(
        "admin_token",
        jwt_token,
        httponly=True,
        max_age=settings.JWT_EXPIRE_MINUTES * 60,
        samesite="lax",
    )
    return resp


@router.get("/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request, db: DbDep):
    user = _get_admin_user(request)
    if user is None:
        return RedirectResponse(url="/admin/login", status_code=302)

    reports_result = await db.execute(
        select(Report).order_by(Report.created_at.desc()).limit(10)
    )
    recent_reports = reports_result.scalars().all()

    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "user": user, "recent_reports": recent_reports},
    )


@router.get("/templates", response_class=HTMLResponse)
async def admin_templates_list(request: Request, db: DbDep):
    user = _get_admin_user(request)
    if user is None:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(select(Template).order_by(Template.template_name))
    tmpl_list = result.scalars().all()
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "user": user, "templates": tmpl_list, "view": "templates"},
    )


@router.get("/templates/{key}/edit", response_class=HTMLResponse)
async def admin_template_edit(key: str, request: Request, db: DbDep):
    user = _get_admin_user(request)
    if user is None:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(select(Template).where(Template.template_key == key))
    tmpl = result.scalar_one_or_none()
    if tmpl is None:
        raise HTTPException(status_code=404, detail="Template not found")

    return templates.TemplateResponse(
        "template_editor.html",
        {"request": request, "user": user, "template": tmpl},
    )


@router.post("/templates/{key}/save")
async def admin_template_save(key: str, request: Request, db: DbDep):
    user = _get_admin_user(request)
    if user is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    body = await request.json()
    template_json = body.get("template_json", {})
    publish_template = body.get("publish_template_jinja2", "")

    errors = validate_template(template_json)
    if errors:
        return JSONResponse({"errors": errors}, status_code=422)

    result = await db.execute(select(Template).where(Template.template_key == key))
    tmpl = result.scalar_one_or_none()
    if tmpl is None:
        raise HTTPException(status_code=404, detail="Template not found")

    tmpl.template_json = template_json
    tmpl.publish_template_jinja2 = publish_template
    tmpl.template_name = template_json.get("template_name", tmpl.template_name)
    tmpl.description = template_json.get("description", tmpl.description)
    tmpl.enabled = template_json.get("enabled", tmpl.enabled)
    await db.commit()
    return JSONResponse({"ok": True})


@router.get("/templates/{key}/preview", response_class=HTMLResponse)
async def admin_template_preview(key: str, request: Request, db: DbDep):
    user = _get_admin_user(request)
    if user is None:
        return RedirectResponse(url="/admin/login", status_code=302)

    result = await db.execute(select(Template).where(Template.template_key == key))
    tmpl = result.scalar_one_or_none()
    if tmpl is None:
        raise HTTPException(status_code=404, detail="Template not found")

    return templates.TemplateResponse(
        "preview.html",
        {"request": request, "user": user, "template": tmpl},
    )


@router.get("/logout")
async def admin_logout(response: Response):
    resp = RedirectResponse(url="/admin/login", status_code=302)
    resp.delete_cookie("admin_token")
    return resp
