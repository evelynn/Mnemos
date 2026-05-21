from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.passwords import verify_password
from app.auth.sessions import create_session, delete_session, read_session
from app.config import get_settings
from app.db import get_session
from app.models.auth import User

_settings = get_settings()
_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter(tags=["dashboard"])


async def _user_from_cookie(token: str | None, db: AsyncSession) -> User | None:
    if not token:
        return None
    user_id = await read_session(token)
    if user_id is None:
        return None
    return (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()


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
    return templates.TemplateResponse(
        request, "login.html", {"error": None}
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_session),
):
    user = (
        await db.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid username or password."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    token = await create_session(user.id)
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
