"""Deterministic single-hop delegation: hand a request to ONE downstream agent, or
resolve it yourself.

Each declared subagent compiles into one auto-generated `handoff(to, task)` tool.
The tool only RECORDS the routing decision in session state (one hop, enforced);
the runner performs the A2A call after the model turn -- so it can forward the
original message alongside the model's scoped objective -- and returns the
downstream reply verbatim. Flow telemetry (parent / flow_id / hop) propagates
automatically via telemetry.propagation_metadata(); a hop-depth guard stops
runaway chains. The topology is deterministic (an agent may ONLY hand to a
declared subagent); only the routing choice is the model's.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import httpx
from a2a import types as pb  # a2a.types == the SDK's types, same objects the server uses
from a2a.client import ClientConfig, ClientFactory
from a2a.client.card_resolver import parse_agent_card
from google.adk.tools.tool_context import ToolContext
from google.protobuf import json_format

from .auth import get_bearer
from .connection import Connection
from .telemetry import propagation_metadata

log = logging.getLogger(__name__)

# Session-state key the handoff tool writes and the runner reads.
_STATE_KEY = "_handoff"
# Runaway-chain / cycle guard: refuse to hand off past this handoff depth.
_MAX_HOPS = 8

# agent name -> its declared downstream agents (so the runner can resolve `to`).
_registry: dict[str, list[Subagent]] = {}


@dataclass(frozen=True)
class Subagent:
    name: str
    description: str
    connection: (
        Connection  # .url = downstream agent-card url; .invoke_url overrides the call endpoint
    )


def remote_subagent(name: str, description: str, connection: Connection) -> Subagent:
    """Declare a downstream agent this agent may hand off to. description is what
    the model routes on; connection.url is the downstream's agent-card url."""
    return Subagent(name=name, description=description, connection=connection)


def register_subagents(agent_name: str, subagents: list[Subagent]) -> None:
    _registry[agent_name] = list(subagents)


def subagents_for(agent_name: str) -> list[Subagent]:
    return _registry.get(agent_name, [])


def make_handoff_tool(subagents: list[Subagent]):
    """Build the single `handoff` tool from the declared subagents. It records the
    routing decision in session state (one hop only); the runner executes it."""
    names = [s.name for s in subagents]
    catalog = "\n".join(f"- {s.name}: {s.description}" for s in subagents)

    def handoff(to: str, task: str, tool_context: ToolContext) -> dict:
        if to not in names:
            return {"error": f"unknown downstream {to!r}; choose one of {names}"}
        if tool_context.state.get(_STATE_KEY):
            return {"error": "already handed off; only one handoff is allowed"}
        tool_context.state[_STATE_KEY] = {"to": to, "task": task}
        return {"status": "handed_off", "to": to}

    handoff.__doc__ = (
        "Hand this request to exactly ONE downstream agent when it falls squarely in "
        "that agent's domain. Call at most once; if you can resolve it yourself, do "
        "not call this at all.\n\n"
        "Downstream agents (use the name in `to`):\n" + catalog + "\n\n"
        "Args:\n"
        "    to: which downstream agent to hand to (one of the names above).\n"
        "    task: a self-contained objective for that agent (what it must do)."
    )
    return handoff


async def execute_handoff(
    subagents: list[Subagent], state, original_message: str, correlation_id: str
) -> A2AReply | None:
    """If the model recorded a handoff, perform it and return the downstream reply.

    Forwards the original message + the model's scoped objective, propagates flow
    telemetry, and guards against runaway hop chains. Returns None when no handoff
    was recorded (the agent resolved the request itself).
    """
    decision = state.get(_STATE_KEY) if state else None
    if not decision:
        return None
    sub = next((s for s in subagents if s.name == decision.get("to")), None)
    if sub is None:
        return None
    meta = propagation_metadata()
    if int(meta.get("hop", 0)) > _MAX_HOPS:
        log.warning("handoff to %s refused: max hop depth %d exceeded", sub.name, _MAX_HOPS)
        return A2AReply(outcome="unavailable", text="max handoff depth exceeded")
    log.info("routed %s -> %s", meta.get("callerApp") or "?", sub.name)
    return await a2a_send(
        sub.connection,
        original_message,
        correlation_id=correlation_id,
        objective=decision.get("task", ""),
        metadata=meta,
    )


@dataclass(frozen=True)
class A2AReply:
    text: str = ""
    outcome: str = "success"  # success (COMPLETED) | failed | unavailable (fail-safe)


def _extract(reply: pb.StreamResponse | None) -> A2AReply:
    """Pull the final text + outcome out of the agent's reply."""
    if reply is None:
        return A2AReply(outcome="unavailable")
    if reply.HasField("message"):
        return A2AReply(text="\n".join(p.text for p in reply.message.parts if p.text))
    if reply.HasField("task"):
        task = reply.task
        text = "\n".join(
            p.text for art in task.artifacts for p in art.parts if p.text
        ) or "\n".join(p.text for p in task.status.message.parts if p.text)
        ok = task.status.state == pb.TaskState.TASK_STATE_COMPLETED
        return A2AReply(text=text, outcome="success" if ok else "failed")
    return A2AReply(outcome="failed")


async def a2a_send(
    connection: Connection,
    message: str,
    *,
    correlation_id: str,
    objective: str = "",
    metadata: dict | None = None,
) -> A2AReply:
    """Send one message to the downstream agent at connection.url; return its final
    reply. Sends the scoped objective first, then the original message; metadata (if
    given) rides on the message for flow-telemetry propagation. Fail-safe: any error
    is logged and returned as A2AReply(outcome="unavailable")."""
    headers = {"A2A-Version": "1.0"}
    token = get_bearer(connection)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if connection.api_key:
        headers[connection.api_key_header] = connection.api_key
    if connection.user_id:
        headers["x-user-id"] = connection.user_id

    msg = pb.Message(
        message_id=uuid.uuid4().hex,
        context_id=correlation_id,
        role=pb.Role.ROLE_USER,
        parts=[pb.Part(text=t) for t in (objective, message) if t],
    )
    if metadata:
        json_format.ParseDict(metadata, msg.metadata)
    request = pb.SendMessageRequest(message=msg)
    try:
        async with httpx.AsyncClient(timeout=connection.timeout, headers=headers) as http:
            resp = await http.get(connection.url)
            resp.raise_for_status()
            card = parse_agent_card(resp.json())
            if connection.invoke_url:
                for iface in card.supported_interfaces:
                    if iface.protocol_binding == "JSONRPC":
                        iface.url = connection.invoke_url
                        break
            client = ClientFactory(ClientConfig(streaming=False, httpx_client=http)).create(
                card
            )
            reply = None
            async for _reply in client.send_message(request):
                reply = _reply
    except Exception as exc:
        log.warning("A2A send to %s failed: %s", connection.url, exc)
        return A2AReply(outcome="unavailable")
    return _extract(reply)
