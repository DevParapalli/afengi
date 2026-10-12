"""Verify mcp_toolset() against a real mcp 2.x server over streamable HTTP.

Serves an MCPServer on localhost via uvicorn, plus a fake OAuth2 token endpoint, and
records every request's headers. No LLM is involved: tools are listed and invoked
directly through ADK's McpToolset.

Run from the project root (framework/ must be importable):
    PYTHONPATH=. uv run scripts/.verification/mcp_toolset.py
"""

import asyncio
import json
import os
import socket
import threading
import time

import mcp.types
import uvicorn
from google.adk.agents import LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.sessions import InMemorySessionService
from google.adk.tools.tool_context import ToolContext
from mcp.server.mcpserver import MCPServer

from framework import mcp_toolset
from framework.connection import Connection

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if got == want else 'FAIL'} {label}")


server = MCPServer("verification")


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@server.tool()
def hidden() -> str:
    """Must not be exposed when tool_filter excludes it."""
    return "leaked"


mcp_app = server.streamable_http_app(stateless_http=True)
seen: list[dict[str, str]] = []
token_hits = 0


async def app(scope, receive, send):
    global token_hits
    if scope["type"] == "http":
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        if scope["path"] == "/token":
            token_hits += 1
            body = json.dumps({"access_token": "tok-123", "expires_in": 3600}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        seen.append(headers)
    await mcp_app(scope, receive, send)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


port = free_port()
uv_server = uvicorn.Server(
    uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
)
threading.Thread(target=uv_server.run, daemon=True).start()
deadline = time.monotonic() + 10
while not uv_server.started:
    if time.monotonic() > deadline:
        raise SystemExit("uvicorn did not start within 10s")
    time.sleep(0.05)

base = f"http://127.0.0.1:{port}"


async def tool_context() -> ToolContext:
    agent = LlmAgent(name="verification_agent", model="gpt-4o-mini", instruction="-")
    sessions = InMemorySessionService()
    session = await sessions.create_session(app_name="verify", user_id="u1")
    ctx = InvocationContext(
        session_service=sessions, invocation_id="inv-1", agent=agent, session=session
    )
    return ToolContext(ctx)


async def main() -> None:
    print("Modern-protocol guard")
    # ADK reads the flag at call time, so toggling the process env is enough.
    os.environ.pop("ADK_ENABLE_MCP_MODERN_PROTOCOL", None)
    try:
        mcp_toolset(Connection(url=f"{base}/mcp"))
        raised = False
    except RuntimeError:
        raised = True
    check("refuses to build without the flag", raised, True)
    os.environ["ADK_ENABLE_MCP_MODERN_PROTOCOL"] = "1"

    print("Protocol + tool listing")
    plain = mcp_toolset(
        Connection(url=f"{base}/mcp", user_id="alice", api_key="k-1"),
        tool_filter=["add"],
        timeout=7.0,
        sse_read_timeout=900.0,
    )
    params = plain._connection_params
    check("timeout kwarg reaches ADK", params.timeout, 7.0)
    check("sse_read_timeout kwarg reaches ADK", params.sse_read_timeout, 900.0)
    tools = await plain.get_tools()
    check("tool_filter narrows the exposed set", [t.name for t in tools], ["add"])
    check(
        "client speaks the latest protocol",
        mcp.types.LATEST_PROTOCOL_VERSION,
        "2026-07-28",
    )
    versions = {h.get("mcp-protocol-version") for h in seen} - {None}
    check("server saw protocol 2026-07-28", versions, {"2026-07-28"})
    check("x-user-id sent", seen[-1].get("x-user-id"), "alice")
    check("x-api-key sent", seen[-1].get("x-api-key"), "k-1")
    check("no bearer without client_id", "authorization" in seen[-1], False)

    print("Tool invocation")
    result = await tools[0].run_async(args={"a": 2, "b": 3}, tool_context=await tool_context())
    check("add(2, 3) round-trips", result.get("structuredContent"), {"result": 5})
    check("call reported no error", result.get("isError"), False)
    await plain.close()

    print("Client-credentials bearer")
    seen.clear()
    authed = mcp_toolset(
        Connection(
            url=f"{base}/mcp",
            client_id="cid",
            client_secret="secret",
            token_url=f"{base}/token",
        )
    )
    check("timeout falls back to connection.timeout", authed._connection_params.timeout, 60.0)
    # ADK calls header_provider only when given a context, as it is during a real run.
    await authed.get_tools(ReadonlyContext((await tool_context())._invocation_context))
    check("token endpoint hit", token_hits >= 1, True)
    check(
        "bearer sent on every MCP request",
        {h.get("authorization") for h in seen},
        {"Bearer tok-123"},
    )
    await authed.close()


asyncio.run(main())
uv_server.should_exit = True

if failures:
    print(f"\n{len(failures)} check(s) failed:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("\nall checks passed")
