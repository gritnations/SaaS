"""Booking page look routes: the owner's editor and what the customer pages read.

Owner routes, added to the owner router so they share its sign-in check (``Authorization: Bearer <token>``):
    * ``GET /owner/businesses/{business}/look``: whether branding is included, the draft and the published look.
    * ``PUT /owner/businesses/{business}/look/draft``: save the draft.
    * ``POST /owner/businesses/{business}/look/publish``: make the draft live.
    * ``POST /owner/businesses/{business}/look/reset``: back to the default look.
    * ``POST /owner/businesses/{business}/look/logo``: upload a logo; the body is the image itself.

Public routes, for the customer booking and manage pages:
    * ``GET /businesses/{business}/look``: the look to apply, or ``null`` for the default.
    * ``GET /businesses/{business}/logos/{logo}``: a logo's image.

A business the account does not manage answers 404, the same as one that does not exist.
"""

from collections.abc import Callable
from typing import Annotated, Protocol, TypeVar

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, Response
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from api.booking import Slug
from booking.look import LOGO_ID_PATTERN, Colour, Corners, Font, Logo, LogoId, Look, LookState, on_accent
from booking.owner import InvalidChangeError
from booking.service import NotFoundError
from postgres import DatabaseUnavailableError

T = TypeVar("T")


class LookBackend(Protocol):
    """What the look routes need from the look service."""

    def state(self, account: str, slug: str) -> LookState:
        """Read the draft and published looks.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.
        """

    def save_draft(self, account: str, slug: str, look: Look) -> LookState:
        """Save the draft.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.
            look: The draft.
        """

    def publish(self, account: str, slug: str) -> LookState:
        """Make the draft live.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.
        """

    def reset(self, account: str, slug: str) -> LookState:
        """Go back to the default look.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.
        """

    def upload_logo(self, account: str, slug: str, content_type: str, data: bytes) -> str:
        """Store a logo.

        Args:
            account: The verified account identifier.
            slug: The business's public identifier.
            content_type: The declared type.
            data: The image's bytes.
        """

    def public_look(self, slug: str) -> Look | None:
        """Give the look customers see.

        Args:
            slug: The business's public identifier.
        """

    def logo(self, slug: str, logo_id: str) -> Logo:
        """Give a logo.

        Args:
            slug: The business's public identifier.
            logo_id: The logo's identifier.
        """


class LookBody(BaseModel):
    """A draft look as the editor sends it.

    Attributes:
        accent: Buttons and highlights, ``#rrggbb`` in lower case.
        background: Page background.
        text: Text colour.
        font: ``modern``, ``classic``, ``rounded`` or ``geometric``.
        corners: ``sharp``, ``soft`` or ``round``.
        logo_id: An uploaded logo, or ``null`` for the business name.
    """

    model_config = ConfigDict(extra="forbid")

    accent: Colour
    background: Colour
    text: Colour
    font: Font
    corners: Corners
    logo_id: LogoId | None = None


class LookOut(BaseModel):
    """A look, with the button text colour worked out.

    Attributes:
        accent: Buttons and highlights.
        background: Page background.
        text: Text colour.
        on_accent: Text colour on buttons.
        font: Font choice.
        corners: Corner style.
        logo_id: The logo, or ``None``.
    """

    accent: str
    background: str
    text: str
    on_accent: str
    font: str
    corners: str
    logo_id: str | None


class LookStateOut(BaseModel):
    """The editor's state.

    Attributes:
        allowed: The plan or trial includes branding now; publishing needs it.
        draft: The saved draft, or ``None``.
        published: The published look, or ``None`` for the default.
    """

    allowed: bool
    draft: LookOut | None
    published: LookOut | None


class PublicLookOut(BaseModel):
    """What the customer pages apply.

    Attributes:
        look: The look, or ``None`` for the default look.
    """

    look: LookOut | None


class LogoOut(BaseModel):
    """An uploaded logo.

    Attributes:
        logo_id: Its identifier, to put in the draft.
    """

    logo_id: str


def _look_out(look: Look | None) -> LookOut | None:
    """Give the public form of a look.

    Args:
        look: The look, or ``None``.

    Returns:
        Its public form, or ``None``.
    """
    if look is None:
        return None
    return LookOut(
        accent=look.accent,
        background=look.background,
        text=look.text,
        on_accent=on_accent(look.accent),
        font=look.font,
        corners=look.corners,
        logo_id=look.logo_id,
    )


def _state_out(state: LookState) -> LookStateOut:
    """Give the public form of the editor state.

    Args:
        state: The state.

    Returns:
        Its public form.
    """
    return LookStateOut(allowed=state.allowed, draft=_look_out(state.draft), published=_look_out(state.published))


def _answer(call: Callable[[], T]) -> T:
    """Run a look operation and turn its outcomes into HTTP answers.

    Args:
        call: The operation.

    Returns:
        Its result.

    Raises:
        HTTPException: 404 not found, 422 refused change, 503 database unreachable.
    """
    try:
        return call()
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except InvalidChangeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except DatabaseUnavailableError:
        raise HTTPException(status_code=503, detail="This is unavailable right now.") from None


def _require(backend: Callable[[], LookBackend | None]) -> LookBackend:
    """Get the look service, or answer 503 when there is none.

    Args:
        backend: Returns the look service, or ``None`` when the database is not configured.

    Returns:
        The look service.

    Raises:
        HTTPException: 503 when the database is not configured.
    """
    found = backend()
    if found is None:
        raise HTTPException(status_code=503, detail="This is unavailable right now.")
    return found


def add_owner_routes(router: APIRouter, account: Callable[..., str], backend: Callable[[], LookBackend | None]) -> None:
    """Add the owner's look routes to the owner router.

    Args:
        router: The owner router, whose prefix is ``/owner``.
        account: The owner router's dependency that verifies the bearer token and gives the account.
        backend: Returns the look service, or ``None`` when the database is not configured.
    """
    Account = Annotated[str, Depends(account)]

    @router.get("/businesses/{business}/look", response_model=LookStateOut)
    def get_look(who: Account, business: Slug) -> LookStateOut:
        """Read whether branding is included, the draft and the published look.

        Args:
            who: The verified account.
            business: Business slug.

        Returns:
            The editor state.
        """
        return _state_out(_answer(lambda: _require(backend).state(who, business)))

    @router.put("/businesses/{business}/look/draft", response_model=LookStateOut)
    def put_draft(who: Account, business: Slug, body: LookBody) -> LookStateOut:
        """Save the draft. Customers still see the published look.

        Args:
            who: The verified account.
            business: Business slug.
            body: The draft.

        Returns:
            The editor state.
        """
        look = Look(**body.model_dump())
        return _state_out(_answer(lambda: _require(backend).save_draft(who, business, look)))

    @router.post("/businesses/{business}/look/publish", response_model=LookStateOut)
    def publish(who: Account, business: Slug) -> LookStateOut:
        """Make the draft the look customers see.

        Args:
            who: The verified account.
            business: Business slug.

        Returns:
            The editor state.
        """
        return _state_out(_answer(lambda: _require(backend).publish(who, business)))

    @router.post("/businesses/{business}/look/reset", response_model=LookStateOut)
    def reset(who: Account, business: Slug) -> LookStateOut:
        """Go back to the default look.

        Args:
            who: The verified account.
            business: Business slug.

        Returns:
            The editor state.
        """
        return _state_out(_answer(lambda: _require(backend).reset(who, business)))

    @router.post("/businesses/{business}/look/logo", response_model=LogoOut, status_code=201)
    async def upload_logo(
        who: Account,
        business: Slug,
        request: Request,
        content_type: Annotated[str, Header()] = "",
    ) -> LogoOut:
        """Upload a logo. The body is the image itself, sent with its own ``Content-Type``.

        Args:
            who: The verified account.
            business: Business slug.
            request: The request, whose body is read.
            content_type: The ``Content-Type`` header.

        Returns:
            The logo's identifier.
        """
        data = await request.body()
        media = content_type.split(";", 1)[0].strip().lower()
        service = _require(backend)
        logo_id = await run_in_threadpool(_answer, lambda: service.upload_logo(who, business, media, data))
        return LogoOut(logo_id=logo_id)


LogoPath = Annotated[str, Path(pattern=LOGO_ID_PATTERN)]


def build_public_router(backend: Callable[[], LookBackend | None]) -> APIRouter:
    """Build the routes the customer pages read.

    Args:
        backend: Returns the look service, or ``None`` when the database is not configured.

    Returns:
        The router, to be included in the API.
    """
    router = APIRouter(prefix="/businesses/{business}")

    @router.get("/look", response_model=PublicLookOut)
    def public_look(business: Slug) -> PublicLookOut:
        """Give the look the customer pages apply.

        Args:
            business: Business slug.

        Returns:
            The look, or ``null`` for the default.
        """
        return PublicLookOut(look=_look_out(_answer(lambda: _require(backend).public_look(business))))

    @router.get("/logos/{logo}", response_class=Response)
    def logo(business: Slug, logo: LogoPath) -> Response:
        """Give a logo's image, for an ``<img>`` on the customer pages.

        Args:
            business: Business slug.
            logo: The logo's identifier.

        Returns:
            The image, with its stored type.
        """
        found = _answer(lambda: _require(backend).logo(business, logo))
        return Response(content=found.data, media_type=found.content_type, headers={"Content-Disposition": "inline"})

    return router
