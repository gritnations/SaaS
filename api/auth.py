"""Endpoint authentication: verifying the sign-in tokens that owners present.

Owners sign in with the identity provider (Supabase Auth) directly from the browser and send the token
it issues as ``Authorization: Bearer <token>``. This module checks that token and returns the account's
identifier. It never sees a password.

A token is accepted only when its signature verifies against the provider's published public keys, it
has not expired, its issuer and audience match, and it names a non-anonymous account. Symmetric
algorithms are not accepted, so no shared secret is needed or held.

Which business an account may manage is not decided here. That is the booking service's check against
``business_members``.

Environment variables read:
    * ``SUPABASE_URL``: the project URL, such as ``https://abcdefghijklmnopqrst.supabase.co``. Not a
      secret. When it is absent, owner routes answer 503.
"""

import re
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

import jwt

SUPABASE_URL_ENV = "SUPABASE_URL"
AUDIENCE = "authenticated"
ALGORITHMS = ["ES256", "RS256"]
"""Asymmetric algorithms only. A token signed with a shared secret is refused."""

LEEWAY_SECONDS = 30
_URL_PATTERN = re.compile(r"^https://[a-z0-9-]+(\.[a-z0-9-]+)+$")


class AuthConfigError(RuntimeError):
    """Raised when the identity-provider configuration is unusable."""


class TokenError(Exception):
    """Raised when a token is missing, malformed, expired or otherwise not acceptable."""


class TokenVerifier(Protocol):
    """What the owner routes need: a way to turn a token into an account identifier."""

    def verify(self, token: str) -> str:
        """Check a token.

        Args:
            token: The bearer token.

        Returns:
            The account identifier, as a UUID string.
        """
        ...


def account_from_claims(claims: Mapping[str, Any]) -> str:
    """Read the account identifier from verified claims.

    Args:
        claims: The token's claims, after signature, expiry, issuer and audience checks.

    Returns:
        The ``sub`` claim as a canonical UUID string.

    Raises:
        TokenError: If the subject is missing or not a UUID, or the account is anonymous.
    """
    if claims.get("is_anonymous") is True:
        raise TokenError("Anonymous accounts cannot manage a business.")
    try:
        return str(uuid.UUID(str(claims.get("sub", ""))))
    except ValueError:
        raise TokenError("The token names no account.") from None


class JwksTokenVerifier:
    """Verifies tokens against the identity provider's published public keys.

    Keys are fetched on first use and cached for the life of the process.
    """

    def __init__(self, issuer: str, jwks_url: str, timeout_seconds: float = 5.0) -> None:
        """Create the verifier. Performs no I/O.

        Args:
            issuer: The expected ``iss`` claim.
            jwks_url: Where the provider publishes its public keys.
            timeout_seconds: Network timeout for fetching the keys.
        """
        self._issuer = issuer
        self._client = jwt.PyJWKClient(jwks_url, cache_keys=True, timeout=int(max(timeout_seconds, 1)))

    def verify(self, token: str) -> str:
        """Check a token's signature and claims.

        Args:
            token: The bearer token.

        Returns:
            The account identifier.

        Raises:
            TokenError: If the token is not acceptable for any reason, including when the keys cannot be
                fetched. The message never contains the token.
        """
        try:
            key = self._client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=ALGORITHMS,
                audience=AUDIENCE,
                issuer=self._issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except (jwt.PyJWTError, jwt.PyJWKClientError):
            raise TokenError("The token is not valid.") from None
        return account_from_claims(claims)


def verifier_from_environment(environ: Mapping[str, str]) -> TokenVerifier | None:
    """Build the verifier from ``SUPABASE_URL``, or report that sign-in is not configured.

    Args:
        environ: The mapping to read.

    Returns:
        The verifier, or ``None`` when the variable is absent.

    Raises:
        AuthConfigError: If the variable is set but is not an ``https://`` host URL.
    """
    raw = environ.get(SUPABASE_URL_ENV, "").strip().rstrip("/")
    if not raw:
        return None
    if not _URL_PATTERN.match(raw):
        raise AuthConfigError(f"{SUPABASE_URL_ENV} must be an https:// host URL such as https://project.supabase.co.")
    issuer = f"{raw}/auth/v1"
    return JwksTokenVerifier(issuer, f"{issuer}/.well-known/jwks.json")
