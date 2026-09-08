"""A pragmatic identity check for the HTTP endpoints.

Identity is resolved in order:

0. Authorization: Bearer <JWT> -- verified cryptographically when OIDC is
   configured (see settings.oidc_*). A valid token yields the caller identity
   from its claims; an invalid one is a hard 401. When OIDC is off, no bearer is
   present, or the JWKS is unreachable, this step is skipped and we DEGRADE to
   the header-trust sources below (the trusted ingress already validated it).
1. X-API-KEY: our own mechanism, validated against a comma-separated allowlist
   in the env (settings.api_keys). A key present but not on the allowlist is an
   authentication FAILURE (not "anonymous").
2. CLIENT_ID / CLIENTID: a machine (client-credentials) caller.
3. AUTH_USER / AUTHUSER / REMOTE_USER / EMAIL: a user caller.

A trusted ingress is assumed to strip inbound identity headers and re-insert them
only after authenticating, so headers 2 and 3 are trustworthy ONLY IF all traffic
enters through such an ingress. Header names are normalized for case and for the
underscore/dash variants proxies may introduce. Bearer verification (step 0) is
the defense-in-depth that removes the "trust the ingress" assumption for tokens.

authenticate returns None for a fully anonymous request and an Identity with
authenticated=False for a presented-but-invalid credential; callers reject both
(401) but can report them differently. When NOTHING is configured to authenticate
against (no API keys, no OIDC issuer) endpoint auth is off and an anonymous
request is allowed -- serve() warns loudly about it at startup.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

import jwt
from jwt import PyJWKClient
from jwt.exceptions import (
    InvalidTokenError,
    PyJWKClientConnectionError,
    PyJWKClientError,
)

from .config import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Identity:
    """The resolved caller identity for a request."""

    kind: Literal["api_key", "machine", "user", "anonymous"]
    id: str
    authenticated: bool = True
    reason: str = ""


def _normalize(headers: Mapping[str, str]) -> dict[str, str]:
    """Lower-case header keys and index both dash and underscore variants."""
    out: dict[str, str] = {}
    for key, value in headers.items():
        lk = key.lower()
        for variant in (lk, lk.replace("_", "-"), lk.replace("-", "_")):
            out[variant] = value
    return out


def _first(norm: dict[str, str], *names: str) -> str | None:
    """Return the first present, non-empty header among names.

    _normalize already indexed every dash/underscore spelling of each incoming
    header, so one lookup per candidate name is enough.
    """
    for name in names:
        value = norm.get(name.lower())
        if value and value.strip():
            return value.strip()
    return None


@lru_cache(maxsize=8)
def _jwk_client(jwks_url: str) -> PyJWKClient:
    """One PyJWKClient per JWKS URL. Fetches the IdP's public keys lazily and
    caches them in memory (TTL + refetch on an unknown kid, so key rotation is
    transparent). Short timeout so an IdP blip degrades fast instead of hanging."""
    return PyJWKClient(jwks_url, cache_keys=True, lifespan=600, timeout=10)


def _verify_bearer(norm: dict[str, str]) -> Identity | None:
    """Verify an `Authorization: Bearer <JWT>` when OIDC is configured.

    Returns an authenticated Identity for a valid token; an Identity with
    authenticated=False for a DEFINITIVE rejection (bad signature / expired /
    wrong aud|iss / missing scope / unknown key); or None to DEGRADE to header
    trust -- when OIDC is off, no bearer is present, or the JWKS is unreachable
    (logged loudly; the ingress already validated the token upstream).
    """
    jwks_url = settings.oidc_jwks
    if not jwks_url:
        return None  # feature off -> header trust

    scheme, _, token = norm.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None  # no bearer -> header trust

    try:
        signing_key = _jwk_client(jwks_url).get_signing_key_from_jwt(token)
    except PyJWKClientConnectionError as exc:
        # IdP/JWKS unreachable: do NOT fail the request -- degrade + log loudly.
        log.error(
            "OIDC JWKS unreachable at %s; DEGRADING to header trust for this request: %s",
            jwks_url,
            exc,
        )
        return None
    except (PyJWKClientError, InvalidTokenError) as exc:
        # Unknown signing key or an unparseable token -> definitive reject.
        return Identity(
            kind="machine", id="", authenticated=False, reason=f"bearer key error: {exc}"
        )

    try:
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.oidc_audience or None,
            issuer=settings.oidc_issuer or None,
            options={
                "require": ["exp", "iss", "sub"],
                "verify_aud": bool(settings.oidc_audience),
            },
        )
    except InvalidTokenError as exc:
        return Identity(
            kind="machine", id="", authenticated=False, reason=f"invalid bearer: {exc}"
        )

    scope = settings.oidc_required_scope
    if scope and scope not in claims.get("scp", []):
        return Identity(
            kind="machine", id="", authenticated=False, reason="missing required scope"
        )

    # Classify from claims: a client-credentials token has sub == cid (a machine);
    # anything else is treated as a user (per its user claim, else sub).
    cid = claims.get("cid") or claims.get("azp") or ""
    sub = claims.get("sub", "")
    if cid and sub == cid:
        return Identity(kind="machine", id=sub, authenticated=True)
    principal = claims.get("email") or claims.get("preferred_username") or sub
    return Identity(kind="user", id=principal, authenticated=True)


def authenticate(headers: Mapping[str, str]) -> Identity | None:
    """Resolve the caller identity from request headers (see module docstring)."""
    norm = _normalize(headers)

    # Step 0: verify a bearer JWT when OIDC is configured. A decision here (auth ok
    # or a hard reject) wins; None means degrade to the header-trust sources below.
    bearer = _verify_bearer(norm)
    if bearer is not None:
        return bearer

    api_key = _first(norm, "x-api-key", "apikey")
    if api_key is not None:
        # Constant-time compare against every configured key: match outcome must
        # not leak via response timing (a plain `in` short-circuits per byte).
        if any(hmac.compare_digest(api_key, k) for k in settings.valid_api_keys):
            return Identity(kind="api_key", id=api_key, authenticated=True)
        return Identity(
            kind="api_key", id=api_key, authenticated=False, reason="invalid API key"
        )

    client_id = _first(norm, "client-id", "clientid")
    if client_id:
        return Identity(kind="machine", id=client_id, authenticated=True)

    user = _first(norm, "auth-user", "authuser", "remote-user", "email", "x-user-id")
    if user:
        return Identity(kind="user", id=user, authenticated=True)

    if not settings.auth_configured:
        return Identity(kind="anonymous", id="anonymous", authenticated=True)
    return None
