"""Login, first-run setup and logout pages."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from uam.errors import AuthError, ConflictError, ValidationError
from uam.web.auth import COOKIE_NAME, SESSION_MAX_AGE, AuthManager
from uam.web.deps import get_auth
from uam.web.ui.support import UiRoute, error_text, form_value, redirect, render

router = APIRouter(route_class=UiRoute)
Auth = Annotated[AuthManager, Depends(get_auth)]


def _signed_in(request: Response, value: str) -> None:
    request.set_cookie(COOKIE_NAME, value, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax", path="/")


@router.get("/login")
async def login_page(request: Request, auth: Auth) -> Response:
    """The sign-in form (or a redirect to setup)."""
    if not await auth.is_setup():
        return redirect(request, "/setup")
    return render(request, "login.html", error=None)


@router.post("/login")
async def login_submit(request: Request, auth: Auth) -> Response:
    """Verify the password and start a session."""
    form = await request.form()
    try:
        if not await auth.is_setup():
            raise AuthError("setup required")
        if not await auth.verify_password(form_value(form, "password")):
            raise AuthError("invalid password")
    except AuthError as exc:
        return render(request, "login.html", status_code=401, error=error_text(exc))
    response = redirect(request, "/")
    _signed_in(response, await auth.issue_session())
    return response


@router.get("/setup")
async def setup_page(request: Request, auth: Auth) -> Response:
    """First-run form for choosing the admin password."""
    if await auth.is_setup():
        return redirect(request, "/login")
    return render(request, "setup.html", error=None)


@router.post("/setup")
async def setup_submit(request: Request, auth: Auth) -> Response:
    """Set the admin password and sign in."""
    form = await request.form()
    password = form_value(form, "password")
    try:
        if password != form_value(form, "confirm"):
            raise ValidationError("passwords do not match")
        await auth.set_password(password)
    except (ValidationError, ConflictError) as exc:
        status = 409 if isinstance(exc, ConflictError) else 422
        return render(request, "setup.html", status_code=status, error=error_text(exc))
    response = redirect(request, "/")
    _signed_in(response, await auth.issue_session())
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    """Clear the session cookie."""
    response = redirect(request, "/login")
    response.delete_cookie(COOKIE_NAME, path="/")
    return response
