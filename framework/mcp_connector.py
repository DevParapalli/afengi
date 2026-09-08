"""MCPConnector: expose an MCP server's tools to an agent.

tool_filter (a list of tool names) narrows the exposed set client-side.
"""

from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

from .auth import _get_valid_token
from .connection import Connection


def mcp_toolset(
    connection: Connection,
    *,
    tool_filter: list[str] | None = None,
) -> McpToolset:
    """McpToolset for the MCP server at connection.url.

    Call once per server to expose several MCP servers (each with own connection)
    to one agent. Every header degrades, sent only when configured: x-user-id /
    x-api-key (from creds), and a client-credentials bearer (minted only when
    creds carry a client_id).
    """
    creds = connection
    url = connection.url
    base_headers: dict[str, str] = {}
    if creds.user_id:
        base_headers["x-user-id"] = creds.user_id
    if creds.api_key:
        base_headers[creds.api_key_header] = creds.api_key

    def headers(_ctx=None) -> dict:
        # Called per request: add a fresh (memoized) bearer token when the MCP
        # client-credentials are configured; otherwise send only the base headers.
        if not creds.client_id:
            return {**base_headers}
        token = _get_valid_token(
            creds.client_id,
            creds.client_secret,
            creds.token_url,
            basic_auth=creds.basic_auth,
        )
        return {**base_headers, "Authorization": f"Bearer {token}"}

    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=url, headers=base_headers),
        header_provider=headers,
        tool_filter=tool_filter,
    )
