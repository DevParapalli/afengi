"""Shared authentication helpers.

Two functions cover every connector:
- _get_valid_token: fetch + memoize an OAuth2 client-credentials token.
- make_authenticated_request: call a downstream API with a valid bearer token.
"""

import base64
import logging
import threading
import time

import httpx2
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .connection import Connection

log = logging.getLogger(__name__)

# (token_url, client_id) -> (token, expires_at_epoch)
_token_cache: dict[tuple[str, str], tuple[str, float]] = {}
# Serializes cache refreshes so a burst of callers mints ONE token, not one each
# (thundering-herd guard). One global lock is enough: refreshes are rare (token
# lives ~30 min) and brief, so cross-key contention is negligible.
_token_lock = threading.Lock()

# Transient upstream failures worth a retry -- network blips + gateway/rate-limit.
# A 4xx (401/403/404) is terminal: it must surface immediately, never retried.
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


def _is_transient(exc: BaseException) -> bool:
    """True for a retryable failure: any network error, or a transient status."""
    if isinstance(exc, httpx2.HTTPStatusError):
        return exc.response.status_code in _TRANSIENT_STATUS
    return isinstance(exc, httpx2.RequestError)


@retry(
    retry=retry_if_exception(_is_transient),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, max=8),
    reraise=True,
)
def _send(method: str, url: str, **kwargs) -> httpx2.Response:
    """One HTTP request, retrying transient failures with exponential backoff.

    A transient status is re-raised (raise_for_status) so tenacity retries it;
    any other status (incl. 401) is returned as-is for the caller to handle.
    """
    resp = httpx2.request(method, url, **kwargs)
    if resp.status_code in _TRANSIENT_STATUS:
        resp.raise_for_status()
    return resp


def _get_valid_token(
    client_id: str,
    client_secret: str,
    token_url: str,
    basic_auth: bool = False,
) -> str:
    """Return a cached OAuth2 client-credentials token; refresh only when missing
    or near expiry.

    basic_auth=True sends creds as an HTTP Basic header (many OpenAI-compatible
    gateways); else they go in the form body (typical client-credentials flow).
    """
    key = (token_url, client_id)
    token, expires_at = _token_cache.get(key, (None, 0.0))
    if token and time.time() < expires_at:
        return token

    # Cache miss/expiry -> serialize the refresh. Double-checked locking: re-read
    # inside the lock first, since another thread may have refreshed it while we
    # waited -- if so, reuse that token instead of minting a second one.
    with _token_lock:
        token, expires_at = _token_cache.get(key, (None, 0.0))
        if token and time.time() < expires_at:
            return token

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {"grant_type": "client_credentials"}
        if basic_auth:
            creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
            headers["Authorization"] = f"Basic {creds}"
        else:
            data["client_id"] = client_id
            data["client_secret"] = client_secret

        resp = _send("POST", token_url, headers=headers, data=data, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        # Refresh 60s early so an in-flight request never uses an expired token.
        _token_cache[key] = (
            body["access_token"],
            time.time() + body.get("expires_in", 1800) - 60,
        )
        return body["access_token"]


def get_bearer(connection: Connection, *, force_refresh: bool = False) -> str | None:
    """Bearer token for connection, or None when no client_id (unauthenticated).

    Wraps the memoized client-credentials flow so callers other than
    make_authenticated_request (e.g. the generic http_request tool) can attach a
    token themselves. force_refresh drops any cached token first -- use it once
    after a 401 to recover from a token the server rejected.
    """
    if not connection.client_id:
        return None
    if force_refresh:
        _token_cache.pop((connection.token_url, connection.client_id), None)
    return _get_valid_token(
        connection.client_id,
        connection.client_secret,
        connection.token_url,
        basic_auth=connection.basic_auth,
    )


def make_authenticated_request(
    url: str,
    connection: Connection,
    method: str = "GET",
    json_body: dict | None = None,
    params: dict | None = None,
    timeout: float | None = None,
) -> dict:
    """Call a downstream API endpoint, return decoded JSON.

    url is the concrete endpoint (often connection.url plus a path); auth + the
    default timeout come from connection. Auth gates on connection.client_id:
    set -> mint bearer (memoized per (token_url, client_id), refreshed once on
    401); empty -> send unauthenticated. timeout overrides connection.timeout when
    given. Logged for observability (not billed spans).
    """
    timeout = connection.timeout if timeout is None else timeout

    def _headers() -> dict:
        headers = {"Accept": "application/json"}
        if connection.client_id:
            token = _get_valid_token(
                connection.client_id,
                connection.client_secret,
                connection.token_url,
                basic_auth=connection.basic_auth,
            )
            headers["Authorization"] = f"Bearer {token}"
        return headers

    log.info("downstream API call: %s %s", method, url)
    resp = _send(
        method, url, headers=_headers(), json=json_body, params=params, timeout=timeout
    )
    if resp.status_code == 401 and connection.client_id:
        _token_cache.pop((connection.token_url, connection.client_id), None)
        resp = _send(
            method,
            url,
            headers=_headers(),
            json=json_body,
            params=params,
            timeout=timeout,
        )
    resp.raise_for_status()
    return resp.json()
