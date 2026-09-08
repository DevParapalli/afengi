"""Native A2A endpoint: the public Agent2Agent protocol, verbatim.

The wire format is the a2a-sdk reference handler, not our code, so conformance
follows the SDK. We supply only the AgentExecutor.

A Decision's state picks the terminal task state and the decision rides on the
final status message's metadata, so a client reads it without a side channel.
Progress and decision data use the `afw.` prefix; a plain A2A client that ignores
metadata still sees status updates, one text artifact and a terminal state.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any
from uuid import UUID

from a2a.helpers import new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import add_a2a_routes_to_fastapi, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCard, Part, TaskState
from fastapi import FastAPI
from google.adk.agents import LlmAgent
from google.protobuf import json_format

from .runner import Resolution, resolve
from .telemetry import Outcome, end_request, start_request

log = logging.getLogger(__name__)

# JSON-RPC mount path for the native protocol (advertised on the agent card).
RPC_PATH = "/a2a"

# Metadata key prefix for everything this framework adds to A2A messages. Bare
# keys would collide with other extensions.
_NS = "afw."

# Answer tokens are coalesced for this long before being flushed as one status
# update. One update per token is correct but chatty -- every update is a queue
# round-trip and a lock acquisition, even for a non-streaming message/send.
_COALESCE_SECONDS = 0.05

# Message-metadata keys the framework owns; everything else on an incoming
# message is treated as caller-supplied request metadata and fed to the prompt.
_FLOW_KEYS = {"parentRequestId", "flowId", "hop", "callerApp"}


def _struct_safe(value: Any) -> Any:
    """Coerce a value into something a protobuf Struct accepts."""
    # Message.metadata is a Struct: JSON scalars/lists/dicts only, and bytes or
    # datetimes raise. Tool results are arbitrary agent output, so round-trip.
    try:
        return json.loads(json.dumps(value, default=str))
    except TypeError, ValueError:
        log.warning("Progress metadata is not JSON-encodable; sending its repr instead.")
        return str(value)


def _incoming_meta(message) -> tuple[dict, dict, list[str]]:
    """Split incoming message metadata into (flow kwargs, request metadata, warnings)."""
    warnings: list[str] = []

    def warn(text: str, **kw) -> None:
        log.warning(text, **kw)
        warnings.append(text)

    meta: dict = {}
    try:
        if message is not None:
            meta = json_format.MessageToDict(message.metadata)
    except Exception:
        log.warning("Unreadable A2A message metadata; flow context lost.", exc_info=True)
        warnings.append("Unreadable A2A message metadata; flow context lost.")
        meta = {}

    parent = meta.get("parentRequestId")
    try:
        parent_id = UUID(parent) if parent else None
    except ValueError, TypeError:
        warn(f"Ignored malformed parentRequestId {parent!r}; flow trace breaks here.")
        parent_id = None
    app_meta: dict = {}
    if meta.get("flowId"):
        app_meta["flow_id"] = meta["flowId"]
    if "hop" in meta:
        try:
            app_meta["hop"] = int(meta["hop"])
        except ValueError, TypeError:
            warn(f"Ignored non-numeric hop {meta['hop']!r}; depth guard restarts.")

    flow = {
        "parent_request_id": parent_id,
        "caller_app": meta.get("callerApp"),
        "app_meta": app_meta,
    }
    request_meta = {k: v for k, v in meta.items() if k not in _FLOW_KEYS}
    return flow, request_meta, warnings


def _progress_text(kind: str, payload: dict) -> str:
    """A human-readable line for one runner progress tuple."""
    if kind in ("thought", "token"):
        return payload.get("text", "")
    if kind == "tool_call":
        return f"Calling {payload.get('name', 'tool')}..."
    if kind == "tool_result":
        return f"Received result from {payload.get('name', 'tool')}"
    return kind


class _ProgressEmitter:
    """Turns runner progress tuples into A2A WORKING status updates."""

    def __init__(self, updater: TaskUpdater) -> None:
        self._updater = updater
        self._buffer: list[str] = []
        self._last_flush = time.monotonic()

    async def emit(self, kind: str, payload: dict) -> None:
        if kind == "token":
            self._buffer.append(payload.get("text", ""))
            if time.monotonic() - self._last_flush >= _COALESCE_SECONDS:
                await self.flush()
            return
        await self.flush()  # keep ordering: buffered tokens precede this event
        await self._status(kind, payload)

    async def flush(self) -> None:
        """Emit any buffered answer tokens as one status update."""
        if not self._buffer:
            return
        text = "".join(self._buffer)
        self._buffer.clear()
        self._last_flush = time.monotonic()
        await self._status("token", {"text": text})

    async def _status(self, kind: str, payload: dict) -> None:
        metadata: dict[str, Any] = {f"{_NS}kind": kind}
        if kind == "tool_call":
            metadata[f"{_NS}tool"] = payload.get("name", "")
            metadata[f"{_NS}args"] = payload.get("args", {})
        elif kind == "tool_result":
            metadata[f"{_NS}tool"] = payload.get("name", "")
            metadata[f"{_NS}response"] = payload.get("response")
        await self._updater.update_status(
            TaskState.TASK_STATE_WORKING,
            self._updater.new_agent_message(
                [Part(text=_progress_text(kind, payload))],
                metadata=_struct_safe(metadata),
            ),
        )


class _AgentExecutor(AgentExecutor):
    """Adapts an ADK agent onto the A2A Task lifecycle."""

    # Task first, then status updates and artifacts, then a terminal state: the
    # only shape the SDK accepts for message/stream.

    def __init__(self, agent: LlmAgent) -> None:
        self._agent = agent

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # Task first (spec/SDK requirement); reuse an existing one on follow-ups.
        task = context.current_task
        if task is None:
            if context.message is None:  # message/send always carries a message
                raise ValueError("A2A request has neither a task nor a message.")
            task = new_task_from_user_message(context.message)
        await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        flow, request_meta, warnings = _incoming_meta(context.message)
        rec = start_request(
            method="A2A",
            url=f"a2a://{self._agent.name}",
            correlation_id=task.context_id,
            parent_request_id=flow["parent_request_id"],
            caller_app=flow["caller_app"],
            app_meta=flow["app_meta"],
        )
        await updater.start_work()

        emitter = _ProgressEmitter(updater)
        try:
            # resolve() owns the run and closes rec with the mapped outcome.
            result = await resolve(
                self._agent,
                text=context.get_user_input(),
                metadata=request_meta,
                context_id=task.context_id,
                task_id=task.id,
                rec=rec,
                user_id="a2a",
                warnings=warnings,
                on_progress=emitter.emit,
            )
        except asyncio.CancelledError:
            # rec is still open here, so close it rather than leaking an
            # unfinished telemetry row; cancel() emits the terminal CANCELED.
            log.warning("A2A task %s cancelled by the caller.", task.id)
            end_request(
                rec,
                http_status=499,
                outcome=Outcome.ERROR,
                error_type="CancelledError",
                error_message="task cancelled by the caller",
            )
            raise

        await emitter.flush()
        await self._deliver(updater, result)

    async def _deliver(self, updater: TaskUpdater, result: Resolution) -> None:
        """Emit the answer and any files as artifacts, then the terminal state."""
        decision = result.decision
        await updater.add_artifact([Part(text=decision.message)], name="response")

        # Tool-produced files (stripped from the model's context) ride along as
        # file parts; b64 is decoded to raw bytes for the protobuf Part.
        for f in result.attachments:
            name = f.get("filename") or "attachment"
            try:
                data = base64.b64decode(f.get("raw", ""))
            except ValueError, TypeError:
                data = b""
            if not data:
                warning = f"Dropped attachment {name!r}: not decodable base64, or empty."
                log.warning(warning)
                result.warnings.append(warning)
                continue
            await updater.add_artifact(
                [
                    Part(
                        raw=data,
                        media_type=f.get("mediaType", ""),
                        filename=f.get("filename", ""),
                    )
                ],
                name=name,
            )

        metadata: dict[str, Any] = {
            f"{_NS}kind": "decision",
            f"{_NS}decision": decision.model_dump(),
        }
        if result.warnings:
            metadata[f"{_NS}warnings"] = result.warnings
        message = updater.new_agent_message(
            [Part(text=decision.message)], metadata=_struct_safe(metadata)
        )
        if decision.state == "COMPLETED":
            await updater.complete(message)
        elif decision.state == "INPUT_REQUIRED":
            await updater.requires_input(message)
        elif decision.state == "REJECTED":
            await updater.reject(message)
        else:
            await updater.failed(message)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Emit the terminal CANCELED; the SDK cancels the producer task itself."""
        # on_cancel_task rejects the cancellation unless the aggregated result is
        # CANCELED, so this must emit it even though the SDK owns the task kill.
        if not context.task_id or not context.context_id:
            return
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()


def mount_native_a2a(app: FastAPI, agent: LlmAgent, card: AgentCard) -> None:
    """Mount the stock A2A JSON-RPC routes at RPC_PATH on app."""
    handler = DefaultRequestHandler(
        agent_executor=_AgentExecutor(agent),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    add_a2a_routes_to_fastapi(app, jsonrpc_routes=create_jsonrpc_routes(handler, RPC_PATH))
