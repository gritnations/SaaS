"""Tests for sign-in token verification. No network: keys are generated locally.

What these tests establish: only tokens signed with the provider's asymmetric key, unexpired, with the
right issuer and audience, and naming a non-anonymous account are accepted, and failures never echo the
token. What they do not establish: fetching the provider's real published keys.
"""

import time
import uuid
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from api.auth import AuthConfigError, JwksTokenVerifier, TokenError, account_from_claims, verifier_from_environment

ISSUER = "https://project.supabase.co/auth/v1"
ACCOUNT = str(uuid.uuid4())
KEY = ec.generate_private_key(ec.SECP256R1())
OTHER_KEY = ec.generate_private_key(ec.SECP256R1())


class LocalKeyClient:
    """Stands in for the provider's key endpoint, always returning one public key."""

    def __init__(self, private_key: ec.EllipticCurvePrivateKey) -> None:
        """Create the stand-in.

        Args:
            private_key: The key whose public half is published.
        """
        self.key = private_key.public_key()

    def get_signing_key_from_jwt(self, token: str) -> Any:
        """Return the published public key, whatever the token.

        Args:
            token: Ignored.

        Returns:
            An object with a ``key`` attribute, like ``jwt.PyJWK``.
        """
        return type("Key", (), {"key": self.key})()


def verifier(published: ec.EllipticCurvePrivateKey = KEY) -> JwksTokenVerifier:
    """Build a verifier that trusts one locally generated key.

    Args:
        published: The key the stand-in endpoint publishes.

    Returns:
        The verifier.
    """
    checker = JwksTokenVerifier(ISSUER, "https://project.supabase.co/auth/v1/.well-known/jwks.json")
    checker._client = LocalKeyClient(published)  # type: ignore[assignment]
    return checker


def token(key: Any = KEY, algorithm: str = "ES256", **overrides: Any) -> str:
    """Sign a token with sensible default claims.

    Args:
        key: The signing key.
        algorithm: The signing algorithm.
        **overrides: Claims to replace, or set to ``None`` to drop.

    Returns:
        The encoded token.
    """
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": ACCOUNT,
        "iss": ISSUER,
        "aud": "authenticated",
        "iat": now,
        "exp": now + 600,
        "role": "authenticated",
        "is_anonymous": False,
    }
    claims.update(overrides)
    claims = {name: value for name, value in claims.items() if value is not None}
    return jwt.encode(claims, key, algorithm=algorithm)


def test_a_valid_token_gives_the_account() -> None:
    """A correctly signed, current token for the right audience names its account."""
    assert verifier().verify(token()) == ACCOUNT


@pytest.mark.parametrize(
    "bad",
    [
        {"exp": int(time.time()) - 3600},
        {"iss": "https://other.supabase.co/auth/v1"},
        {"aud": "anon"},
        {"sub": None},
        {"exp": None},
        {"sub": "not-a-uuid"},
        {"is_anonymous": True},
    ],
)
def test_tokens_with_bad_claims_are_refused(bad: dict[str, Any]) -> None:
    """Expired, foreign, wrong-audience, subjectless, non-UUID or anonymous tokens are refused."""
    with pytest.raises(TokenError):
        verifier().verify(token(**bad))


def test_a_token_signed_by_another_key_is_refused() -> None:
    """A token not signed by the published key fails the signature check."""
    with pytest.raises(TokenError):
        verifier().verify(token(key=OTHER_KEY))


def test_a_shared_secret_token_is_refused() -> None:
    """Symmetric algorithms are never accepted, so a forged HS256 token fails."""
    forged = token(key="a-guessable-secret-of-sufficient-length-123", algorithm="HS256")
    with pytest.raises(TokenError):
        verifier().verify(forged)


@pytest.mark.parametrize("garbage", ["", "abc", "a.b.c", "x" * 2000])
def test_malformed_tokens_are_refused_without_echoing_them(garbage: str) -> None:
    """Garbage is refused, and the error message never contains the token."""
    with pytest.raises(TokenError) as caught:
        verifier().verify(garbage)
    assert garbage not in str(caught.value) or garbage == ""


def test_account_is_returned_in_canonical_form() -> None:
    """An upper-case UUID subject comes back lower-case, so membership lookups match."""
    assert account_from_claims({"sub": ACCOUNT.upper()}) == ACCOUNT


def test_the_verifier_is_absent_without_configuration() -> None:
    """With no ``SUPABASE_URL`` there is no verifier, and owner routes answer 503."""
    assert verifier_from_environment({}) is None


@pytest.mark.parametrize("url", ["http://project.supabase.co", "project.supabase.co", "https://", "https://a b.co"])
def test_an_unsafe_url_is_refused(url: str) -> None:
    """Only an ``https://`` host URL is accepted."""
    with pytest.raises(AuthConfigError):
        verifier_from_environment({"SUPABASE_URL": url})


def test_the_issuer_is_derived_from_the_project_url() -> None:
    """A trailing slash is tolerated, and the issuer is the project's auth endpoint."""
    built = verifier_from_environment({"SUPABASE_URL": "https://project.supabase.co/"})
    assert isinstance(built, JwksTokenVerifier)
    built._client = LocalKeyClient(KEY)  # type: ignore[assignment]
    assert built.verify(token()) == ACCOUNT
