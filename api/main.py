"""FastAPI application: composition and routing for the platform API.

Routes:
    * ``GET /health``: process liveness. No I/O.
    * ``GET /health/ready``: infrastructure readiness. Coarse status only.
    * ``GET /metrics``: operational metrics. Open outside production, protected or absent in production.
    * ``POST /payments/{provider}/checkout`` and ``POST /payments/{provider}/webhook``: the
      provider-neutral payment boundary. No provider is live.

This module contains no SQL, no payment-provider implementation and no business logic. It delegates to
``postgres`` for the database and to ``payments`` for providers.

Environment variables read:
    * ``APP_ENV``: ``production`` (the default), ``development`` or ``test``.
    * ``METRICS_TOKEN``: the bearer token that unlocks ``/metrics`` in production. A secret.
    * ``DATABASE_URL`` and ``DATABASE_POOL_MAX_SIZE``: read by ``postgres``.

Constraints:
    * Importing this module and calling :func:`create_api` perform no network I/O.
    * Website reachability is not part of liveness or readiness.
"""

import hmac
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Annotated, Protocol

from fastapi import FastAPI, Header, HTTPException, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

from api.metrics import Metrics
from payments.base import (
    ORDER_REFERENCE_PATTERN,
    PaymentProvider,
    PaymentRegistry,
    ProviderNotLiveError,
    UnknownProviderError,
    WebhookRejectedError,
)
from payments.paymob import PaymobProvider
from payments.stub import StubProvider
from postgres import Database, DatabaseConfigError, DatabaseSettings

LOGGER = logging.getLogger(__name__)

API_VERSION = "0.1.0"
APP_ENV_VAR = "APP_ENV"
METRICS_TOKEN_ENV = "METRICS_TOKEN"
ENVIRONMENTS = ("production", "development", "test")
MIN_METRICS_TOKEN_LENGTH = 32
METRICS_MEDIA_TYPE = "text/plain; version=0.0.4; charset=utf-8"

ProviderName = Annotated[str, Path(pattern=r"^[a-z0-9_-]{1,32}$")]
"""A provider name as it appears in a request path."""


class ApiConfigError(RuntimeError):
    """Raised when the API configuration is invalid. The message never contains a secret value."""


class ReadinessProbe(Protocol):
    """What the API needs from the database boundary: a health answer and a way to release it."""

    def is_healthy(self) -> bool:
        """Report whether the dependency is usable.

        Returns:
            ``True`` if it is usable.
        """
        ...

    def close(self) -> None:
        """Release the dependency's resources."""
        ...


class StatusOut(BaseModel):
    """A coarse status answer.

    Attributes:
        status: ``"ok"``, ``"ready"`` or ``"not_ready"``. Never a reason.
    """

    status: str


class CheckoutBody(BaseModel):
    """The body of a checkout request.

    It carries an order reference and nothing else. An amount sent by a caller is refused, because the
    price is decided on the server.

    Attributes:
        order_reference: 6 to 64 letters, digits, hyphens or underscores.
    """

    model_config = ConfigDict(extra="forbid")

    order_reference: Annotated[str, StringConstraints(pattern=ORDER_REFERENCE_PATTERN)]


class CheckoutOut(BaseModel):
    """The answer to a checkout request.

    Attributes:
        provider: The provider that produced the session.
        order_reference: The order the session is for.
        checkout_url: Where to send the payer, or ``None``.
        live: ``True`` only when real money can move.
    """

    provider: str
    order_reference: str
    checkout_url: str | None
    live: bool


class WebhookOut(BaseModel):
    """The answer to a provider notification.

    Attributes:
        accepted: The notification was parsed and trusted by the provider adapter.
        provider: The provider that parsed it.
        live: ``True`` only when the notification describes real money.
        recorded: Whether anything was stored. Always ``False``: there is no payment store yet.
    """

    accepted: bool
    provider: str
    live: bool
    recorded: bool = False


def resolve_environment(environ: Mapping[str, str]) -> str:
    """Read and validate ``APP_ENV``.

    Args:
        environ: The mapping to read.

    Returns:
        ``"production"``, ``"development"`` or ``"test"``. An absent or empty value means
        ``"production"``, so that a missing setting can never loosen security.

    Raises:
        ApiConfigError: If the value is anything else.
    """
    value = environ.get(APP_ENV_VAR, "").strip().lower() or "production"
    if value not in ENVIRONMENTS:
        raise ApiConfigError(f"{APP_ENV_VAR} must be one of: {', '.join(ENVIRONMENTS)}.")
    return value


def resolve_metrics_token(environ: Mapping[str, str]) -> str | None:
    """Read and validate ``METRICS_TOKEN``.

    Args:
        environ: The mapping to read.

    Returns:
        The token, or ``None`` if it is not set.

    Raises:
        ApiConfigError: If a token is set but is shorter than 32 characters.
    """
    token = environ.get(METRICS_TOKEN_ENV, "").strip()
    if not token:
        return None
    if len(token) < MIN_METRICS_TOKEN_LENGTH:
        raise ApiConfigError(f"{METRICS_TOKEN_ENV} must be at least {MIN_METRICS_TOKEN_LENGTH} characters.")
    return token


def build_registry(environment: str) -> PaymentRegistry:
    """Choose the payment providers for an environment.

    Args:
        environment: The value returned by :func:`resolve_environment`.

    Returns:
        A registry holding Paymob everywhere, and the stub only outside production. The stub accepts
        unsigned notifications, so it is never registered in production.
    """
    providers: list[PaymentProvider] = [PaymobProvider()]
    if environment != "production":
        providers.append(StubProvider())
    return PaymentRegistry(providers)


def database_from_environment(environ: Mapping[str, str]) -> Database | None:
    """Build the database boundary, or report that it is not configured.

    A missing or unsafe configuration does not stop the API from starting. Liveness stays up and
    readiness reports ``not_ready`` until the configuration is corrected.

    Args:
        environ: The mapping to read.

    Returns:
        The database boundary, or ``None`` if its configuration is absent or unsafe. No connection is
        opened here.
    """
    try:
        return Database(DatabaseSettings.from_env(environ))
    except DatabaseConfigError as exc:
        LOGGER.error("database is not configured: %s", exc)
        return None


def create_api(environ: Mapping[str, str] | None = None, database: ReadinessProbe | None = None) -> FastAPI:
    """Build the API application.

    Args:
        environ: The configuration mapping. ``None`` means the process environment.
        database: The database boundary to use. ``None`` means build it from ``environ``.

    Returns:
        The FastAPI application. The database boundary, or ``None``, is stored on
        ``app.state.database`` so that the root application can close it at shutdown.

    Raises:
        ApiConfigError: If ``APP_ENV`` or ``METRICS_TOKEN`` is invalid.
    """
    env = os.environ if environ is None else environ
    environment = resolve_environment(env)
    metrics_token = resolve_metrics_token(env)
    registry = build_registry(environment)
    probe = database if database is not None else database_from_environment(env)
    metrics = Metrics(API_VERSION)

    api = FastAPI(
        title="Platform API",
        version=API_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json" if environment == "development" else None,
    )
    api.state.database = probe
    api.state.environment = environment

    @api.middleware("http")
    async def count_responses(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """Count every response for ``/metrics``.

        Args:
            request: The incoming request.
            call_next: The next handler in the chain.

        Returns:
            The response, unchanged.
        """
        response = await call_next(request)
        metrics.record_response(response.status_code)
        return response

    @api.exception_handler(RequestValidationError)
    async def on_invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Answer an invalid request without echoing what was sent.

        Args:
            request: The incoming request.
            exc: The validation failure.

        Returns:
            A 422 response naming each field and the kind of problem, never the submitted value.
        """
        problems = [
            {"field": ".".join(str(part) for part in error["loc"][1:])[:64], "problem": str(error["type"])}
            for error in exc.errors()[:10]
        ]
        return JSONResponse({"detail": "The request is not valid.", "problems": problems}, status_code=422)

    def provider_for(name: str) -> PaymentProvider:
        """Look up a provider, turning an unknown name into a 404.

        Args:
            name: The provider name from the request path.

        Returns:
            The registered adapter.

        Raises:
            HTTPException: 404 if no provider has that name in this environment.
        """
        try:
            return registry.get(name)
        except UnknownProviderError:
            raise HTTPException(status_code=404, detail="Unknown payment provider.") from None

    @api.get("/health", response_model=StatusOut)
    def health() -> StatusOut:
        """Report process liveness.

        Returns:
            ``{"status": "ok"}``. It touches no database, no provider and no website.
        """
        return StatusOut(status="ok")

    @api.get("/health/ready", response_model=StatusOut)
    def ready(response: Response) -> StatusOut:
        """Report whether required infrastructure is usable.

        Args:
            response: Used to set the 503 status when not ready.

        Returns:
            ``{"status": "ready"}`` with 200, or ``{"status": "not_ready"}`` with 503. The reason is
            logged on the server and never returned.
        """
        if probe is not None and probe.is_healthy():
            return StatusOut(status="ready")
        response.status_code = 503
        return StatusOut(status="not_ready")

    @api.get("/metrics", response_class=PlainTextResponse)
    def metrics_endpoint(authorization: Annotated[str | None, Header()] = None) -> PlainTextResponse:
        """Expose operational metrics.

        Outside production the endpoint is open. In production it does not exist unless
        ``METRICS_TOKEN`` is set, and then it requires ``Authorization: Bearer <token>``.

        Args:
            authorization: The ``Authorization`` request header, if any.

        Returns:
            The metrics in Prometheus text format.

        Raises:
            HTTPException: 404 in production with no token configured. 401 when the token is missing
                or wrong.
        """
        if environment == "production":
            if metrics_token is None:
                raise HTTPException(status_code=404, detail="Not Found")
            supplied = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
            if not hmac.compare_digest(supplied.encode(), metrics_token.encode()):
                raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Bearer"})
        return PlainTextResponse(metrics.render(), media_type=METRICS_MEDIA_TYPE)

    @api.post("/payments/{provider}/checkout", response_model=CheckoutOut)
    def checkout(provider: ProviderName, body: CheckoutBody) -> CheckoutOut:
        """Start a checkout through a provider.

        Args:
            provider: The provider name from the path.
            body: The order reference. No amount is accepted.

        Returns:
            The provider's checkout session.

        Raises:
            HTTPException: 404 for an unknown provider. 501 when the provider is not live.
        """
        adapter = provider_for(provider)
        try:
            session = adapter.create_checkout(body.order_reference)
        except ProviderNotLiveError:
            raise HTTPException(status_code=501, detail="This payment provider is not live.") from None
        return CheckoutOut(
            provider=session.provider,
            order_reference=session.order_reference,
            checkout_url=session.checkout_url,
            live=session.live,
        )

    @api.post("/payments/{provider}/webhook", response_model=WebhookOut, status_code=202)
    async def webhook(provider: ProviderName, request: Request) -> WebhookOut:
        """Receive a provider notification and hand it to the provider adapter.

        Args:
            provider: The provider name from the path.
            request: The raw request. The adapter receives the exact bytes and the headers.

        Returns:
            Confirmation that the adapter accepted the notification. Nothing is stored.

        Raises:
            HTTPException: 404 for an unknown provider. 501 when the provider is not live. 400 when the
                adapter rejects the notification.
        """
        adapter = provider_for(provider)
        headers = {name.lower(): value for name, value in request.headers.items()}
        try:
            event = adapter.parse_webhook(headers, await request.body())
        except ProviderNotLiveError:
            raise HTTPException(status_code=501, detail="This payment provider is not live.") from None
        except WebhookRejectedError:
            raise HTTPException(status_code=400, detail="Webhook rejected.") from None
        LOGGER.info("payment notification accepted provider=%s live=%s", event.provider, event.live)
        return WebhookOut(accepted=True, provider=event.provider, live=event.live)

    return api
