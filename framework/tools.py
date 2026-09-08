"""Optional ready-made tools an agent can opt into.

These are plain ADK function tools -- the docstring IS the model's description.
They are NOT wired in automatically; import and list them in agent.py to use:

    from framework import http_request, current_datetime
    root_agent = build_agent(..., tools=[http_request, current_datetime, ...])

http_request is gated by settings.tool_url_allowlist (fail-closed: an empty
allowlist reaches nothing) so the model can only touch hosts a deployment opted
in. For calls that need auth, build a per-agent variant with
make_http_request_tool([Connection(url=..., ...), ...]) -- it attaches the
matching bearer automatically.

SECURITY: URL/allowlist/connection matching is host-boundary aware (parsed
scheme+host+path), never a raw string prefix, so a token is never sent to a
look-alike host like https://api.example.com.evil.com. Bearer tokens are only
attached over HTTPS.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx2

from .auth import _send, get_bearer
from .config import settings
from .connection import Connection

log = logging.getLogger(__name__)

# HTTP methods the generic tool will issue (kept explicit so the model can't be
# talked into an odd verb).
_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _parse(ref: str) -> tuple[str | None, str, str]:
    """Split a URL or allowlist/registry entry into (scheme|None, host, path).

    Entries without '://' are treated as host[/path] with no scheme constraint.
    Host is lower-cased; the path's trailing slash is trimmed so '/v1' and '/v1/'
    compare equal. scheme is None for a bare-host entry (matches http or https).
    """
    if "://" in ref:
        p = urlparse(ref)
        return p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/")
    host, _, rest = ref.partition("/")
    return None, host.lower(), (f"/{rest}".rstrip("/") if rest else "")


def _matches(url: str, entry: str) -> bool:
    """True if url falls under entry, matched on parsed boundaries (NOT a raw
    string prefix): host must be equal, scheme must agree when the entry pins one,
    and any entry path must match url's path at a segment boundary."""
    u_scheme, u_host, u_path = _parse(url)
    e_scheme, e_host, e_path = _parse(entry)
    if e_host != u_host:
        return False
    if e_scheme is not None and e_scheme != u_scheme:
        return False
    return not e_path or u_path == e_path or u_path.startswith(f"{e_path}/")


def _is_allowed(url: str) -> bool:
    """True if url is covered by settings.allowed_tool_urls. Empty allowlist =
    reach nothing (fail closed): a deployment opts each host in via
    TOOL_URL_ALLOWLIST."""
    return any(_matches(url, entry) for entry in settings.allowed_tool_urls)


def _select_connection(url: str, connections: Sequence[Connection]) -> Connection | None:
    """The connection whose url matches url, most specific (longest matching
    path) winning. None when nothing matches."""
    best: Connection | None = None
    best_specificity = -1
    for conn in connections:
        if _matches(url, conn.url):
            specificity = len(_parse(conn.url)[2])
            if specificity > best_specificity:
                best, best_specificity = conn, specificity
    return best


def _request(
    url: str,
    method: str,
    json_body: dict | None,
    params: dict | None,
    connections: Sequence[Connection],
) -> dict:
    """Shared core for the generic HTTP tool (see http_request for the contract).

    When a connection in connections matches url (and url is HTTPS), its bearer is
    attached and refreshed once on a 401.
    """
    verb = method.upper()
    if verb not in _ALLOWED_METHODS:
        return {"error": f"unsupported method: {method!r}"}
    if not url.lower().startswith(("http://", "https://")):
        return {"error": f"unsupported URL scheme: {url!r}"}
    if not _is_allowed(url):
        return {"error": f"URL not allowed by tool_url_allowlist: {url!r}"}

    creds = _select_connection(url, connections) if connections else None
    if creds is not None and not url.lower().startswith("https://"):
        # Never leak a bearer over plaintext; fall back to unauthenticated.
        log.warning("not attaching credentials to non-HTTPS URL %s", url)
        creds = None

    # Auth headers degrade independently (same rule as the rest of the framework):
    # a bearer when client-creds are set, the api_key under its configured header,
    # and x-user-id when present. All only over HTTPS (creds cleared above on http).
    headers = {"Accept": "application/json"}
    if creds is not None:
        token = get_bearer(creds)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if creds.api_key:
            headers[creds.api_key_header] = creds.api_key
        if creds.user_id:
            headers["x-user-id"] = creds.user_id

    log.info("http_request tool: %s %s", verb, url)
    try:
        resp = _send(verb, url, headers=headers, json=json_body, params=params, timeout=60)
        # A rejected token may be stale: refresh once and retry.
        if resp.status_code == 401 and creds is not None and creds.client_id:
            token = get_bearer(creds, force_refresh=True)
            if token:
                headers["Authorization"] = f"Bearer {token}"
                resp = _send(
                    verb,
                    url,
                    headers=headers,
                    json=json_body,
                    params=params,
                    timeout=60,
                )
    except httpx2.HTTPError as exc:
        log.warning("http_request failed: %s %s: %s", verb, url, exc)
        return {"error": str(exc)}

    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    return {"status": resp.status_code, "body": body}


def http_request(
    url: str,
    method: str = "GET",
    json_body: dict | None = None,
    params: dict | None = None,
) -> dict:
    """Call an HTTP(S) URL and return its response. Use to read from or post to a
    service the agent is permitted to reach. Sends no authentication.

    Only URLs the deployment allowlisted are reachable; anything else is refused.
    Returns {"status": <int>, "body": <parsed JSON or raw text>}, or
    {"error": <reason>} when the URL is not allowed or the request fails.

    Args:
        url: Full http(s) URL to call. Required.
        method: HTTP method -- GET, POST, PUT, PATCH or DELETE. Default GET.
        json_body: Optional JSON object to send as the request body.
        params: Optional query-string parameters.
    """
    return _request(url, method, json_body, params, [])


def make_http_request_tool(connections: Sequence[Connection]):
    """Build an http_request tool that auto-authenticates by URL.

    connections is a list of Connection objects; a request whose URL matches a
    connection's url uses that connection's credentials, the most specific
    (longest path) match winning. Matching is host-boundary aware, so a connection
    for ``https://api.example.com`` is never used for
    ``https://api.example.com.evil.com``, and a bearer is only attached over
    HTTPS. Non-matching URLs are called unauthenticated. Every URL is still
    subject to TOOL_URL_ALLOWLIST -- add each connection's host there too, or it
    stays unreachable.

    Add the returned function to an agent's tools list:

        api = Connection(url="https://api.example.com", client_id=..., ...)
        tool = make_http_request_tool([api])
        root_agent = build_agent(..., tools=[tool])
    """
    registry = list(connections)

    def http_request(
        url: str,
        method: str = "GET",
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        """Call an HTTP(S) URL and return its response, authenticating
        automatically when the URL matches a configured service.

        Only URLs the deployment allowlisted are reachable; anything else is
        refused. Returns {"status": <int>, "body": <parsed JSON or raw text>}, or
        {"error": <reason>} when the URL is not allowed or the request fails.

        Args:
            url: Full http(s) URL to call. Required.
            method: HTTP method -- GET, POST, PUT, PATCH or DELETE. Default GET.
            json_body: Optional JSON object to send as the request body.
            params: Optional query-string parameters.
        """
        return _request(url, method, json_body, params, registry)

    return http_request


def current_datetime() -> dict:
    """Return the current date and time in UTC. Use whenever you need today's date
    or the current time -- never guess it.

    Returns {"iso": <ISO-8601 UTC>, "date": <YYYY-MM-DD>, "epoch": <unix seconds>}.
    """
    now = datetime.now(UTC)
    return {
        "iso": now.isoformat(),
        "date": now.date().isoformat(),
        "epoch": int(now.timestamp()),
    }
