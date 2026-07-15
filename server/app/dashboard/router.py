import logging
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth import brute_force
from app.auth.deps import resolve_active_session_user
from app.auth.passwords import verify_password
from app.auth.sessions import create_session, delete_session
from app.config import get_settings
from app.db import get_session
from app.models.auth import User

_settings = get_settings()
_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
log = logging.getLogger(__name__)


def _asset_url(path: str) -> str:
    """Append the static file's mtime as a ``?v=`` cache-buster.

    The static handler sends no ``Cache-Control``, so a browser that
    cached an earlier ``app.css`` keeps showing the old design after a
    redesign ships (PR-165 dogfood: the operator saw the pre-redesign
    stylesheet). Keying the URL on mtime makes the browser refetch the
    moment the file changes, with no manual version bump."""
    name = path.rsplit("/", 1)[-1]
    try:
        v = int((_STATIC_DIR / name).stat().st_mtime)
    except OSError:
        return path
    return f"{path}?v={v}"


templates.env.globals["asset_url"] = _asset_url

router = APIRouter(tags=["dashboard"])


async def _user_from_cookie(token: str | None, db: AsyncSession) -> User | None:
    return await resolve_active_session_user(token, db)


@router.get("/forgot", response_class=HTMLResponse)
async def forgot_page(request: Request):
    """Anonymous password-reset request landing page (PR-45)."""
    return templates.TemplateResponse(request, "forgot.html", {})


@router.get("/reset", response_class=HTMLResponse)
async def reset_page(request: Request):
    """Anonymous password-reset consume landing page (PR-45).

    The token is read from the URL fragment ``#token=…`` by the
    client-side JS — fragments aren't sent to the server, so a
    paste of the reset URL into chat doesn't surface the token in
    a referrer log.
    """
    return templates.TemplateResponse(request, "reset.html", {})


@router.get("/invite", response_class=HTMLResponse)
async def invite_accept_page(request: Request):
    """Anonymous invite-acceptance landing page (PR-45). Same
    fragment-only token convention as ``/reset``."""
    return templates.TemplateResponse(request, "invite.html", {})


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    session_token: str | None = Cookie(default=None, alias=_settings.session_cookie_name),
    db: AsyncSession = Depends(get_session),
):
    user = await _user_from_cookie(session_token, db)
    if user is not None:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_session),
):
    if await brute_force.is_locked(username):
        await audit_record(
            actor="anonymous",
            action="auth.login_blocked_lockout",
            target=username,
        )
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Account temporarily locked. Try again later."},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )
    # Serialize local-password session issuance with password reset on the
    # same User row.  ``populate_existing`` is required when this request's
    # identity map saw the user before waiting for a concurrent reset.
    user = (
        await db.execute(
            select(User)
            .where(User.username == username)
            .with_for_update(of=User)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        actor = f"user:{user.id}" if user else "anonymous"
        await db.rollback()
        await brute_force.record_failure(username)
        await audit_record(
            actor=actor,
            action="auth.login_failed",
            target=username,
        )
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid username or password."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    if user.disabled_at is not None:
        actor = f"user:{user.id}"
        await db.rollback()
        await audit_record(
            actor=actor,
            action="auth.login_blocked_disabled",
            target=username,
        )
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid username or password."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    token: str | None = None
    try:
        await brute_force.clear(username)
        token = await create_session(user.id)
        # This explicit commit is the lock-release boundary.  It deliberately
        # follows Redis session creation so a reset that waits on this row will
        # always see and revoke the newly issued credential.
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            log.exception("auth.dashboard_login_rollback_failed")
        if token is not None:
            try:
                await delete_session(token)
            except Exception:  # noqa: BLE001
                log.exception("auth.dashboard_login_session_cleanup_failed")
        raise

    redirect = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    redirect.set_cookie(
        key=_settings.session_cookie_name,
        value=token,
        max_age=_settings.session_max_age_sec,
        httponly=True,
        samesite="lax",
        secure=_settings.session_cookie_secure,
        path="/",
    )
    return redirect


@router.post("/logout")
async def logout(
    session_token: str | None = Cookie(default=None, alias=_settings.session_cookie_name),
):
    if session_token:
        await delete_session(session_token)
    redirect = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    redirect.delete_cookie(_settings.session_cookie_name, path="/")
    return redirect


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    session_token: str | None = Cookie(default=None, alias=_settings.session_cookie_name),
    db: AsyncSession = Depends(get_session),
):
    user = await _user_from_cookie(session_token, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"user": user, "active_tab": "dashboard"},
    )


@router.get("/{tab}", response_class=HTMLResponse)
async def tab_page(
    tab: str,
    request: Request,
    session_token: str | None = Cookie(default=None, alias=_settings.session_cookie_name),
    db: AsyncSession = Depends(get_session),
):
    valid = {
        "projects",
        "analysis",
        "data",
        "graph",
        "ask",
        "report",
        "plans",
        "diffs",
        "findings",
        "audit",
        "settings",
        "profile",
        "users",
        "organizations",
        "gdpr",
        "sso",
        "health",
        "docs",
        "chat",
    }
    if tab not in valid:
        raise HTTPException(status_code=404)
    user = await _user_from_cookie(session_token, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request,
        f"{tab}.html",
        {"user": user, "active_tab": tab},
    )
