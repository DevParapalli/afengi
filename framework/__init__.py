"""A small, readable framework for building and deploying ADK agents.

To build the agent you only touch files under agent/ -- never the framework:
- config.py: the agent's settings (API base URL, MCP subset, model),
- prompts.py: the system prompt (domain knowledge only),
- tools.py: the API tools,
- agent.py: wire config + prompt + tools + MCP subset together.

Everything an agent (or the main.py entrypoint) needs is re-exported here. The
persistence, telemetry, security and database internals are used by the framework
itself; import them directly from their submodules (e.g. from framework.telemetry
import start_request).
"""

from .agent import CardInfo, build_agent
from .attachments import FilePart, ToolAttachment
from .auth import make_authenticated_request
from .config import settings
from .connection import Connection
from .decision import Decision
from .delegation import remote_subagent
from .llm import custom_llm_provider
from .mcp_connector import mcp_toolset
from .runner import Resolution, resolve, run
from .server import serve
from .tools import current_datetime, http_request, make_http_request_tool

__all__ = [
    "CardInfo",
    "Connection",
    "Decision",
    "FilePart",
    "Resolution",
    "ToolAttachment",
    "build_agent",
    "current_datetime",
    "custom_llm_provider",
    "http_request",
    "make_authenticated_request",
    "make_http_request_tool",
    "mcp_toolset",
    "remote_subagent",
    "resolve",
    "run",
    "serve",
    "settings",
]
