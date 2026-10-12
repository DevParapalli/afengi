"""MCPConnector: expose an MCP server's tools to an agent.

tool_filter (a list of tool names) narrows the exposed set client-side.
"""

from google.adk.features import FeatureName, is_feature_enabled
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

from .auth import _get_valid_token
from .connection import Connection


def mcp_toolset(
    connection: Connection,
    *,
    tool_filter: list[str] | None = None,
    timeout: float | None = None,
    sse_read_timeout: float = 300.0,
) -> McpToolset:
    """McpToolset for the MCP server at connection.url.

    Call once per server to expose several MCP servers (each with own connection)
    to one agent. Every header degrades, sent only when configured: x-user-id /
    x-api-key (from creds), the ADK session id under connection.session_header,
    and a client-credentials bearer (minted only when creds carry a client_id). timeout overrides connection.timeout. Raise
    sse_read_timeout for servers that hold a response open (e.g. awaiting a human
    approval); 300 matches ADK's own default.
    """
    # ADK keeps 2026-07-28 behind an off-by-default flag and otherwise silently
    # negotiates 2025-11-25, which servers that require stateless MCP refuse. Looked up
    # by value: the enum member is private, and a rename should fail loudly here.
    if not is_feature_enabled(FeatureName("MCP_MODERN_PROTOCOL")):
        raise RuntimeError(
            "mcp_toolset needs ADK_ENABLE_MCP_MODERN_PROTOCOL=1 in the process environment "
            "(.env is not exported; locally use `uv run --env-file .env main.py`)"
        )
    creds = connection
    url = connection.url
    base_headers: dict[str, str] = {}
    if creds.user_id:
        base_headers["x-user-id"] = creds.user_id
    if creds.api_key:
        base_headers[creds.api_key_header] = creds.api_key

    def headers(ctx=None) -> dict:
        # Called per request: add the session id and a fresh (memoized) bearer token
        # when configured. ADK pools MCP sessions by header set, so a session header
        # also gives each chat session its own pooled MCP session.
        out = {**base_headers}
        if creds.session_header and ctx is not None:
            out[creds.session_header] = ctx.session.id
        if creds.client_id:
            token = _get_valid_token(
                creds.client_id,
                creds.client_secret,
                creds.token_url,
                basic_auth=creds.basic_auth,
            )
            out["Authorization"] = f"Bearer {token}"
        return out

    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=url,
            headers=base_headers,
            timeout=connection.timeout if timeout is None else timeout,
            sse_read_timeout=sse_read_timeout,
        ),
        header_provider=headers,
        tool_filter=tool_filter,
    )
