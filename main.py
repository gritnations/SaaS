"""Root application: composition and the platform's ingress security boundary.

Trust path: ``Internet -> main.py -> api/main.py -> bounded service -> postgres.py -> PostgreSQL``.

This module builds the root application, applies the global ingress controls and attaches the API. It
contains no SQL, no booking logic, no payment-provider code, no tenant logic and no credential.
Endpoint authorisation belongs to the API, business rules to bounded services, and data permissions to
the database.

Ingress controls, outermost first:
    1. :func:`ingress_guard`: body size limit, controlled errors and security headers.
    2. Trusted hosts: requests for any other ``Host`` are refused.
    3. CORS: only listed origins may call from a browser.

Environment variables read:
    * ``ALLOWED_HOSTS``: comma-separated host names. Default ``localhost,127.0.0.1``.
    * ``CORS_ALLOWED_ORIGINS``: comma-separated origins such as ``https://app.example.com``. Default none.

Run it with ``uvicorn main:app --no-server-header``.
"""

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from api.main import create_api

LOGGER = logging.getLogger(__name__)

ALLOWED_HOSTS_ENV = "ALLOWED_HOSTS"
CORS_ORIGINS_ENV = "CORS_ALLOWED_ORIGINS"
DEFAULT_ALLOWED_HOSTS = ("localhost", "127.0.0.1")
MAX_BODY_BYTES = 65_536
"""The largest request body accepted, in bytes."""

BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


class IngressConfigError(RuntimeError):
    """Raised when the host or CORS configuration is unsafe. The application refuses to start."""


def read_list(environ: Mapping[str, str], name: str) -> list[str]:
    """Read a comma-separated environment variable.

    Args:
        environ: The mapping to read.
        name: The variable name.

    Returns:
        The non-empty items, with surrounding spaces removed. An absent variable gives an empty list.
    """
    return [item.strip() for item in environ.get(name, "").split(",") if item.strip()]


def error_response(status_code: int, detail: str) -> JSONResponse:
    """Build the one error shape the ingress boundary returns.

    Args:
        status_code: The HTTP status code.
        detail: A fixed, public description. Never text from an exception.

    Returns:
        A JSON response of the form ``{"detail": "..."}``.
    """
    return JSONResponse({"detail": detail}, status_code=status_code)


async def on_unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    """Turn an unexpected error inside the API into a generic 500.

    :func:`create_app` registers this on the attached API application. Without it the API would answer
    with the framework's own error page before this boundary could act.

    Args:
        request: The request being handled.
        exc: The unexpected error. Only its type is logged, because its text may hold sensitive detail.

    Returns:
        ``{"detail": "Internal server error."}`` with status 500.
    """
    LOGGER.error("unhandled %s on %s %s", type(exc).__name__, request.method, request.url.path)
    return error_response(500, "Internal server error.")


async def ingress_guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Apply the global request limits, controlled error handling and security headers.

    POST, PUT and PATCH requests without ``Content-Length`` receive 411. On any method, a
    non-digit length receives 400 and a declared length above :data:`MAX_BODY_BYTES` receives 413.
    Only the declared length is checked; the body is not measured. Exceptions from ``call_next``
    become a generic 500. Errors raised inside the API are handled by :func:`on_unhandled_error`.

    Args:
        request: The incoming request.
        call_next: The next handler in the chain.

    Returns:
        The response, with :data:`SECURITY_HEADERS` set.
    """
    length = request.headers.get("content-length")
    if request.method in BODY_METHODS and length is None:
        response: Response = error_response(411, "A Content-Length header is required.")
    elif length is not None and not length.isdigit():
        response = error_response(400, "Invalid Content-Length.")
    elif length is not None and int(length) > MAX_BODY_BYTES:
        response = error_response(413, "Request body is too large.")
    else:
        try:
            response = await call_next(request)
        except Exception as exc:
            LOGGER.error("unhandled %s on %s %s", type(exc).__name__, request.method, request.url.path)
            response = error_response(500, "Internal server error.")
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


def create_app(environ: Mapping[str, str] | None = None, api: FastAPI | None = None) -> FastAPI:
    """Build the root application.

    Args:
        environ: The configuration mapping. ``None`` means the process environment.
        api: The API application to attach. ``None`` means build it from ``environ``.

    Returns:
        The root application, with the API mounted at ``/``.

    Raises:
        IngressConfigError: If a bare wildcard host is configured, or an origin contains a wildcard,
            lacks an ``http://`` or ``https://`` prefix, or ends with a slash.
        api.main.ApiConfigError: If no API is supplied and ``APP_ENV`` or ``METRICS_TOKEN`` is invalid.
    """
    env = os.environ if environ is None else environ
    hosts = read_list(env, ALLOWED_HOSTS_ENV) or list(DEFAULT_ALLOWED_HOSTS)
    origins = read_list(env, CORS_ORIGINS_ENV)
    if "*" in hosts:
        raise IngressConfigError(f"{ALLOWED_HOSTS_ENV} must list host names. A bare wildcard is refused.")
    for origin in origins:
        if not origin.startswith(("https://", "http://")) or origin.endswith("/") or "*" in origin:
            raise IngressConfigError(f"{CORS_ORIGINS_ENV} must list exact origins such as https://app.example.com.")
    api_app = api if api is not None else create_api(env)
    api_app.add_exception_handler(Exception, on_unhandled_error)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Release the database boundary when the process stops.

        Nothing is opened at start-up. The database connects on first use.

        Args:
            _: The root application. Unused.

        Yields:
            Control to the running application.
        """
        try:
            yield
        finally:
            database = getattr(api_app.state, "database", None)
            if database is not None:
                database.close()

    root = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    root.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )
    root.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    root.add_middleware(BaseHTTPMiddleware, dispatch=ingress_guard)
    root.mount("/", api_app)
    return root


app = create_app()
