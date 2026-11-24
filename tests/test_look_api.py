"""Tests for the booking page look routes. A fake look service stands in for the database.

What these tests establish: the owner routes need sign-in; a draft that is not exactly a valid look is refused
before the service is called; a logo upload passes its bytes and declared type to the service and a body over the
API's limit never reaches it; the service's outcomes map to 404, 422 and 503; the public look answers ``null`` for
the default; and a logo is served with its stored type, ``nosniff`` and a restrictive content policy. The rules
themselves are covered by ``test_look.py`` and ``test_look_postgres.py``.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from helpers import ORIGIN, TRUSTED_HOST, FakeDatabase
from test_owner_api import ACCOUNT, GOOD, FakeOwners, FakeVerifier

from api.main import create_api
from booking.look import LOGO_MAX_BYTES, Logo, Look, LookState
from booking.owner import InvalidChangeError
from booking.service import NotFoundError
from main import LOGO_UPLOAD_MAX_BODY_BYTES, create_app
from postgres import DatabaseUnavailableError

OWNER = "/owner/businesses/example/look"
LOGO_ID = "0b6c3f0e-6a8e-4d3e-9a51-0f3c1f2d9a10"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
DRAFT: dict[str, Any] = {
    "accent": "#e0aa55",
    "background": "#0c1726",
    "text": "#f3f5f8",
    "font": "classic",
    "corners": "round",
    "logo_id": LOGO_ID,
}


class FakeLooks:
    """Records calls and answers with fixed values, or raises a chosen error.

    Attributes:
        calls: Each call as ``(name, arguments)``.
        error: Raised by every call when set.
        published: What :meth:`public_look` gives.
    """

    def __init__(self) -> None:
        """Create the fake."""
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.error: Exception | None = None
        self.published: Look | None = Look(**DRAFT)

    def _record(self, name: str, *args: Any) -> None:
        """Record a call, then raise the chosen error if any.

        Args:
            name: The method.
            *args: Its arguments.

        Raises:
            Exception: :attr:`error`, when set.
        """
        self.calls.append((name, args))
        if self.error is not None:
            raise self.error

    def _state(self) -> LookState:
        """Give a fixed editor state.

        Returns:
            Allowed, with the draft also published.
        """
        return LookState(allowed=True, draft=Look(**DRAFT), published=Look(**DRAFT))

    def state(self, account: str, slug: str) -> LookState:
        """Record and answer.

        Args:
            account: The account.
            slug: The business.

        Returns:
            The fixed state.
        """
        self._record("state", account, slug)
        return self._state()

    def save_draft(self, account: str, slug: str, look: Look) -> LookState:
        """Record and answer.

        Args:
            account: The account.
            slug: The business.
            look: The draft.

        Returns:
            The fixed state.
        """
        self._record("save_draft", account, slug, look)
        return self._state()

    def publish(self, account: str, slug: str) -> LookState:
        """Record and answer.

        Args:
            account: The account.
            slug: The business.

        Returns:
            The fixed state.
        """
        self._record("publish", account, slug)
        return self._state()

    def reset(self, account: str, slug: str) -> LookState:
        """Record and answer.

        Args:
            account: The account.
            slug: The business.

        Returns:
            An empty state.
        """
        self._record("reset", account, slug)
        return LookState(allowed=True)

    def upload_logo(self, account: str, slug: str, content_type: str, data: bytes) -> str:
        """Record and answer.

        Args:
            account: The account.
            slug: The business.
            content_type: The declared type.
            data: The bytes.

        Returns:
            A fixed identifier.
        """
        self._record("upload_logo", account, slug, content_type, data)
        return LOGO_ID

    def public_look(self, slug: str) -> Look | None:
        """Record and answer.

        Args:
            slug: The business.

        Returns:
            :attr:`published`.
        """
        self._record("public_look", slug)
        return self.published

    def logo(self, slug: str, logo_id: str) -> Logo:
        """Record and answer.

        Args:
            slug: The business.
            logo_id: The logo.

        Returns:
            A PNG.
        """
        self._record("logo", slug, logo_id)
        return Logo(content_type="image/png", data=PNG)


def client(looks: FakeLooks | None) -> TestClient:
    """Build the full application around the fake.

    Args:
        looks: The fake, or ``None`` for no look service.

    Returns:
        A client that reports server errors as responses.
    """
    environ = {"APP_ENV": "test", "ALLOWED_HOSTS": TRUSTED_HOST, "CORS_ALLOWED_ORIGINS": ORIGIN}
    api = create_api(environ, database=FakeDatabase(), owners=FakeOwners(), verifier=FakeVerifier(), looks=looks)
    return TestClient(create_app(environ, api=api), raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", OWNER),
        ("put", f"{OWNER}/draft"),
        ("post", f"{OWNER}/publish"),
        ("post", f"{OWNER}/reset"),
        ("post", f"{OWNER}/logo"),
    ],
)
def test_the_owner_routes_need_sign_in(method: str, path: str) -> None:
    """No token or a wrong token: 401, and the service is never called."""
    looks = FakeLooks()
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        response = client(looks).request(method, path, headers=headers, json=DRAFT if method == "put" else None)
        assert response.status_code == 401
    assert looks.calls == []


def test_the_editor_state_carries_the_button_text_colour() -> None:
    """The state comes back with ``on_accent`` worked out for each look."""
    looks = FakeLooks()
    response = client(looks).get(OWNER, headers=GOOD)
    assert response.status_code == 200
    body = response.json()
    assert body["allowed"] is True
    assert body["draft"] == {**DRAFT, "on_accent": "#111111"}
    assert looks.calls == [("state", (ACCOUNT, "example"))]


def test_a_valid_draft_reaches_the_service_as_sent() -> None:
    """Every field arrives unchanged."""
    looks = FakeLooks()
    response = client(looks).put(f"{OWNER}/draft", headers=GOOD, json=DRAFT)
    assert response.status_code == 200
    assert looks.calls == [("save_draft", (ACCOUNT, "example", Look(**DRAFT)))]


@pytest.mark.parametrize(
    "change",
    [
        {"accent": "#E0AA55"},
        {"accent": "red"},
        {"background": "#000;}*{display:none"},
        {"font": "Papyrus"},
        {"corners": "pill"},
        {"logo_id": "not-a-logo"},
        {"css": "body{}"},
    ],
)
def test_an_invalid_draft_is_refused_before_the_service_is_called(change: dict[str, Any]) -> None:
    """Anything outside the fixed sets, and any extra field, is 422."""
    looks = FakeLooks()
    response = client(looks).put(f"{OWNER}/draft", headers=GOOD, json={**DRAFT, **change})
    assert response.status_code == 422
    assert looks.calls == []


def test_publish_and_reset_reach_the_service() -> None:
    """Both are plain POSTs with no body."""
    looks = FakeLooks()
    assert client(looks).post(f"{OWNER}/publish", headers=GOOD).status_code == 200
    reset = client(looks).post(f"{OWNER}/reset", headers=GOOD)
    assert reset.status_code == 200 and reset.json() == {"allowed": True, "draft": None, "published": None}
    assert looks.calls == [("publish", (ACCOUNT, "example")), ("reset", (ACCOUNT, "example"))]


def test_a_logo_upload_passes_its_bytes_and_declared_type() -> None:
    """The body is the image; parameters after the type are dropped and the type is lower-cased."""
    looks = FakeLooks()
    response = client(looks).post(
        f"{OWNER}/logo", headers={**GOOD, "Content-Type": "Image/PNG; charset=binary"}, content=PNG
    )
    assert response.status_code == 201 and response.json() == {"logo_id": LOGO_ID}
    assert looks.calls == [("upload_logo", (ACCOUNT, "example", "image/png", PNG))]


def test_a_logo_over_the_logo_route_limit_never_reaches_the_service() -> None:
    """The ingress boundary refuses a declared length above the logo route's own limit with 413."""
    looks = FakeLooks()
    response = client(looks).post(
        f"{OWNER}/logo",
        headers={**GOOD, "Content-Type": "image/png"},
        content=PNG + bytes(LOGO_UPLOAD_MAX_BODY_BYTES - len(PNG) + 1),
    )
    assert response.status_code == 413
    assert looks.calls == []


def test_a_logo_of_the_largest_size_reaches_the_service() -> None:
    """A logo of exactly the logo limit, well over the 64 KB every other route allows, is passed on in full."""
    looks = FakeLooks()
    content = PNG + bytes(LOGO_MAX_BYTES - len(PNG))
    response = client(looks).post(f"{OWNER}/logo", headers={**GOOD, "Content-Type": "image/png"}, content=content)
    assert response.status_code == 201
    assert looks.calls == [("upload_logo", (ACCOUNT, "example", "image/png", content))]
    assert len(content) == LOGO_MAX_BYTES > 65_536


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (NotFoundError("Business not found."), 404),
        (InvalidChangeError("Upload a PNG, JPEG or WebP image."), 422),
        (DatabaseUnavailableError("down"), 503),
    ],
)
def test_service_outcomes_map_to_http_answers(error: Exception, status: int) -> None:
    """Every owner route and the logo upload give the same mapping."""
    looks = FakeLooks()
    looks.error = error
    tested = client(looks)
    assert tested.get(OWNER, headers=GOOD).status_code == status
    assert tested.post(f"{OWNER}/publish", headers=GOOD).status_code == status
    upload = tested.post(f"{OWNER}/logo", headers={**GOOD, "Content-Type": "image/png"}, content=PNG)
    assert upload.status_code == status


def test_no_look_service_answers_503() -> None:
    """Without a database the routes answer 503, public and owner alike."""
    tested = client(None)
    assert tested.get(OWNER, headers=GOOD).status_code == 503
    assert tested.get("/businesses/example/look").status_code == 503


def test_the_public_look_needs_no_sign_in_and_can_be_the_default() -> None:
    """A published look comes back in full; the default look is ``null``."""
    looks = FakeLooks()
    response = client(looks).get("/businesses/example/look")
    assert response.status_code == 200 and response.json()["look"]["accent"] == "#e0aa55"
    looks.published = None
    assert client(looks).get("/businesses/example/look").json() == {"look": None}


def test_a_logo_is_served_with_its_type_and_safe_headers() -> None:
    """The stored type, inline, ``nosniff`` and a content policy that lets nothing run."""
    looks = FakeLooks()
    response = client(looks).get(f"/businesses/example/logos/{LOGO_ID}")
    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"].startswith("default-src 'none'")
    assert looks.calls == [("logo", ("example", LOGO_ID))]


def test_a_malformed_logo_identifier_is_refused() -> None:
    """Only a lower-case UUID reaches the service."""
    looks = FakeLooks()
    assert client(looks).get("/businesses/example/logos/..%2f..%2fetc").status_code in (404, 422)
    assert client(looks).get("/businesses/example/logos/" + LOGO_ID.upper()).status_code == 422
    assert looks.calls == []
