"""A small, transport-agnostic connection bundle: endpoint + auth + timeout.

Every framework connector/tool that reaches a remote service accepts one of
these. Carries the union of fields the built-in connectors use -- a primary
endpoint URL, an OAuth2 client-credentials pair (plus token endpoint), a
user-id / api-key header pair, and a call timeout -- so one type covers LLM
gateways, downstream APIs, MCP servers and downstream A2A agents alike.

url is the primary endpoint: the base URL for LLM/API/MCP connectors, or the
agent CARD url for a downstream agent. invoke_url is an optional override for
the concrete call endpoint when it differs from url (e.g. an A2A agent's JSON-RPC
invoke endpoint); left empty, a consumer derives it from url (for an agent, by
reading its card).

Each consumer uses only the fields it needs and degrades on empties (no
client_id -> no bearer minted; no api_key -> that header not sent). Framework
never reads a connection from global config; the agent builds these and passes
them in.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Connection:
    # URL
    url: str = ""  # primary endpoint: base URL, or agent CARD url for a downstream agent
    # Secondary URL
    invoke_url: str = (
        ""  # concrete call endpoint override; empty = derive from url (e.g. the card)
    )
    # OAuth2 client-credentials
    token_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    # API-Key if required
    api_key: str = ""
    # If anything here requires a user-id header, say api-key is mapped to a certain user.
    user_id: str = ""
    basic_auth: bool = False  # send client creds as HTTP Basic (gateways) vs form body (OAuth2)
    # Header name the api_key is sent under. Default matches the MCP convention;
    # override for APIs that use a different name (e.g. "Ocp-Apim-Subscription-Key",
    # "apikey", "X-API-Key").
    api_key_header: str = "x-api-key"
    # Per-call timeout in seconds for requests made over this connection.
    timeout: float = 60.0
