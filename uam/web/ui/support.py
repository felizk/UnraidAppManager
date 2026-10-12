"""Shared plumbing for the HTML pages: templates, error/redirect handling, auth guard, view helpers."""

from __future__ import annotations

import html
import re
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.routing import APIRoute
from markupsafe import Markup
from starlette.templating import Jinja2Templates

from uam.contracts import DockerEngine
from uam.domain import LABEL_UNRAID_ICON, LABEL_UNRAID_WEBUI, App, AppState, PortAllocation
from uam.errors import (
    AuthError,
    ConflictError,
    NotFound,
    PortRangeExhausted,
    RateLimited,
    RemoteError,
    TemplateParseError,
    UamError,
    ValidationError,
)
from uam.logging import redact
from uam.web.auth import COOKIE_NAME, AuthManager, CsrfError
from uam.web.deps import ApiServices, get_auth, get_services

WEB_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

TRANSITIONAL = frozenset({AppState.INSTALLING, AppState.UPDATING, AppState.REMOVING})
Services = Annotated[ApiServices, Depends(get_services)]


class UiRedirect(Exception):  # noqa: N818 - control flow, not an error
    """Raised by guards to send the browser elsewhere."""

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.url = url


def is_htmx(request: Request) -> bool:
    """True for requests issued by HTMX."""
    return request.headers.get("hx-request") == "true"


def redirect(request: Request, url: str) -> Response:
    """Navigate to `url`: `HX-Redirect` for HTMX requests, a 303 for ordinary form posts."""
    if is_htmx(request):
        return Response(status_code=200, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def status_for(exc: UamError) -> int:
    """HTTP status for a `UamError` (same mapping as the REST API)."""
    if isinstance(exc, CsrfError):
        return 403
    if isinstance(exc, AuthError):
        return 401
    if isinstance(exc, NotFound):
        return 404
    if isinstance(exc, ConflictError | PortRangeExhausted):
        return 409
    if isinstance(exc, ValidationError | TemplateParseError):
        return 422
    if isinstance(exc, RateLimited):
        return 429
    if isinstance(exc, RemoteError):
        return 502
    return 500


def error_text(exc: BaseException) -> str:
    """A user-facing, secret-free message."""
    return redact(str(exc)) or exc.__class__.__name__


class UiRoute(APIRoute):
    """Turns `UiRedirect` and `UamError` into redirects and HTML error responses."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap the normal handler."""
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except UiRedirect as r:
                return redirect(request, r.url)
            except UamError as exc:
                return error_page(request, exc)

        return handler


def error_page(request: Request, exc: UamError) -> Response:
    """HTMX requests get a flash message retargeted into `#flash`; others get a full error page."""
    status = status_for(exc)
    message = error_text(exc)
    if is_htmx(request):
        body = f'<article class="flash error" role="alert">{html.escape(message)}</article>'
        return HTMLResponse(
            body, status_code=status, headers={"HX-Retarget": "#flash", "HX-Reswap": "innerHTML"}
        )
    return render(request, "error.html", status_code=status, message=message, status=status)


# ---------------------------------------------------------------- templates
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def _ago(value: datetime | None) -> str:
    if value is None:
        return "never"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    seconds = int((datetime.now(UTC) - value).total_seconds())
    if seconds < 0:
        return value.strftime("%Y-%m-%d %H:%M")
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def _stamp(value: datetime | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.strftime("%Y-%m-%d %H:%M:%S")


def diff_html(text: str) -> Markup:
    """A unified diff with `+`/`-` lines coloured (escaped; safe to inline in `<pre>`)."""
    out: list[str] = []
    for line in text.splitlines():
        cls = "ctx"
        if line.startswith(("+++", "---")):
            cls = "meta"
        elif line.startswith("@@"):
            cls = "hunk"
        elif line.startswith("+"):
            cls = "add"
        elif line.startswith("-"):
            cls = "del"
        out.append(f'<span class="d-{cls}">{html.escape(line)}</span>')
    return Markup("\n".join(out))  # noqa: S704 - every line is escaped above


templates.env.filters["ago"] = _ago
templates.env.filters["stamp"] = _stamp
templates.env.filters["diff"] = diff_html


def render(request: Request, name: str, status_code: int = 200, **context: Any) -> Response:
    """Render a template with the request in its context."""
    return templates.TemplateResponse(request, name, context, status_code=status_code)


# ---------------------------------------------------------------- auth guard
async def require_page(request: Request, auth: Annotated[AuthManager, Depends(get_auth)]) -> None:
    """Redirect to `/setup` or `/login` unless a valid session (or bearer token) is present."""
    if auth.bearer_valid(request):
        return
    if await auth.session_valid(request.cookies.get(COOKIE_NAME)):
        return
    raise UiRedirect("/login" if await auth.is_setup() else "/setup")


# ---------------------------------------------------------------- busy tracking
def busy_set(request: Request) -> set[int]:
    """App ids with an operation running in the background (kept on the app state)."""
    state = request.app.state
    if not hasattr(state, "ui_busy"):
        state.ui_busy = set()
    busy: set[int] = state.ui_busy
    return busy


def run_in_background(
    request: Request, services: ApiServices, app_id: int, label: str, call: Callable[[], Awaitable[object]]
) -> Callable[[], Awaitable[None]]:
    """Mark `app_id` busy and return the coroutine function to hand to `BackgroundTasks`."""
    busy = busy_set(request)
    busy.add(app_id)

    async def run() -> None:
        try:
            await call()
        except Exception as exc:  # noqa: BLE001 - background task boundary
            try:
                await services.events.record(
                    app_id, f"ui_{label}_failed", redact(f"{label} failed: {exc}"), "error"
                )
            except Exception:  # noqa: BLE001, S110
                pass
        finally:
            busy.discard(app_id)

    return run


# ---------------------------------------------------------------- app views
_PORT_TOKEN = re.compile(r"\[PORT:(\d+)\]")


@dataclass
class AppView:
    """Everything a card or the detail page shows about one app."""

    app: App
    icon: str = ""
    webui_url: str = ""
    npm_url: str = ""
    ports: list[PortAllocation] = field(default_factory=list)
    busy: bool = False

    @property
    def polling(self) -> bool:
        """True while something is in progress (the card polls faster)."""
        return self.busy or self.app.state in TRANSITIONAL

    @property
    def update_template(self) -> bool:
        """A newer template commit is available."""
        a = self.app
        return bool(
            a.available_template_commit and a.available_template_commit != a.installed_template_commit
        )

    @property
    def update_image(self) -> bool:
        """A newer image digest is available."""
        a = self.app
        return bool(a.available_image_digest and a.available_image_digest != a.installed_image_digest)

    @property
    def installed(self) -> bool:
        """The app has (or had) a container."""
        return self.app.state not in (AppState.NOT_INSTALLED, AppState.INSTALLING)


def _resolve_webui(url: str, host: str, ports: list[PortAllocation]) -> str:
    def port(m: re.Match[str]) -> str:
        want = int(m.group(1))
        for p in ports:
            if p.container_port == want:
                return str(p.host_port)
        return m.group(1)

    return _PORT_TOKEN.sub(port, url.replace("[IP]", host))


async def _host(services: ApiServices, configured: str | None) -> str:
    if configured:
        return configured
    try:
        return await services.docker.host_ip()
    except Exception:  # noqa: BLE001
        return "localhost"


async def _container_labels(docker: DockerEngine, app: App) -> dict[str, str]:
    if not app.container_id:
        return {}
    try:
        info = await docker.inspect(app.container_id)
    except Exception:  # noqa: BLE001 - a cosmetic lookup must not break the page
        return {}
    return info.labels if info else {}


async def build_views(request: Request, services: ApiServices, apps: list[App]) -> list[AppView]:
    """Card/detail data for `apps`, using container labels for icon and WebUI link."""
    settings = await services.settings.get()
    allocations = await services.ports.allocations()
    host = await _host(services, settings.host_ip) if apps else ""
    busy = busy_set(request)
    views: list[AppView] = []
    for app in apps:
        labels = await _container_labels(services.docker, app)
        ports = [a for a in allocations if a.app_id == app.id]
        webui = _resolve_webui(labels.get(LABEL_UNRAID_WEBUI, ""), host, ports) if labels else ""
        npm = ""
        if app.proxy.subdomain and settings.base_domain:
            scheme = "https" if app.proxy.ssl_forced else "http"
            npm = f"{scheme}://{app.proxy.subdomain}.{settings.base_domain}"
        views.append(
            AppView(
                app=app,
                icon=labels.get(LABEL_UNRAID_ICON, ""),
                webui_url=webui or npm,
                npm_url=npm,
                ports=ports,
                busy=app.id in busy,
            )
        )
    return views


def form_value(form: Any, key: str, default: str = "") -> str:
    """A string field from a parsed form (files and missing keys give `default`)."""
    value = form.get(key)
    return value if isinstance(value, str) else default


def optional_int(raw: str, label: str) -> int | None:
    """Parse an optional integer field; blank is None."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValidationError(f"{label} must be a number") from None


# ---------------------------------------------------------------- secrets in displays
_SECRETISH = re.compile(r"(?i)pass|secret|token|key|auth|credential")


def shown_value(key: str, value: str, masked_keys: frozenset[str] | set[str] = frozenset()) -> str:
    """`***` for values that are masked in the template or whose key looks like a secret."""
    if value and (key in masked_keys or _SECRETISH.search(key)):
        return "***"
    return value
