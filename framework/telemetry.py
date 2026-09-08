"""Lightweight telemetry: one record per request, one per model call.

Deliberately instrumentation, not tracing. Two Pydantic models capture what
happened; four helpers persist them insert-on-start / update-on-end:
- start_request / end_request: the inbound API request span.
- start_model_call / end_model_call: an individual model call.

Every DB write is best-effort: if persistence is disabled or the write fails, the
helpers log and return the record unchanged so telemetry can never break request
handling. Recorded REGARDLESS of auth -- an auth failure still produces a request
record with its outcome and error set.

The active request id is carried in a ContextVar so the LLM layer can attach
model-call telemetry to the triggering request without threading the id through
every call.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid7

from pydantic import BaseModel, Field

from .config import settings
from .db import session_scope
from .models import AiModelCallTelemetryRow, ApiRequestTelemetryRow

log = logging.getLogger(__name__)

# The request currently being served, so model calls can link back to it.
_current_request_id: ContextVar[UUID | None] = ContextVar("current_request_id", default=None)

# (flow_id, hop) of the active span, so an outbound handoff can propagate them.
_current_flow: ContextVar[tuple[str, int] | None] = ContextVar("current_flow", default=None)


def current_request_id() -> UUID | None:
    """The id of the request currently being served, if any."""
    return _current_request_id.get()


def propagation_metadata() -> dict[str, Any]:
    """Flow context to attach to an outbound A2A handoff so the downstream agent's
    span links back here: the flow id, THIS span as the parent, our app name, and
    the next hop number. Empty when no request span is active."""
    rid = _current_request_id.get()
    flow = _current_flow.get()
    if rid is None or flow is None:
        return {}
    flow_id, hop = flow
    return {
        "flowId": flow_id,
        "parentRequestId": str(rid),
        "callerApp": settings.app_name,
        "hop": hop + 1,
    }


class Outcome(StrEnum):
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILURE = "failure"
    ERROR = "error"
    TIMEOUT = "timeout"


class ApiRequestTelemetry(BaseModel):
    id: UUID  # uuid v7
    created_at: datetime  # start time
    app_name: str  # our application name.
    environment: str = "prod"  # obviously
    correlation_id: str  # contextId / caller's request id (not a UUID)
    trace_id: str | None = None  # reserved for an OTel trace id (mapped in later)
    parent_request_id: UUID | None = None  # calling agent's request span (NULL = root)
    caller_app: str | None = None  # calling agent's app_name (the parent node)
    caller_identity: str | None = None  # client_id or auth_user or an X-Api-Key
    method: str  # POST / GET / etc
    url: str  # the endpoint being called
    http_status: int | None = None  # number that we returned
    outcome: Outcome | None = None  # success / partial_success / failure / error / timeout
    error_type: str | None = None  # class of error, works for Java, Python, Go anything.
    error_message: str | None = None  # stack trace or message, whatever is available.
    duration_ms: int | None = None  # runtime
    app_meta: dict[str, Any] = Field(default_factory=dict)  # anything extra.


class AiModelCallTelemetry(BaseModel):
    id: UUID  # uuid v7
    request_id: UUID  # soft-FK into ApiRequestTelemetry.id
    created_at: datetime  # start time
    identifier: str  # generic caller identifier (e.g. app key / tenant) paired with client_id
    model_name: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    duration_ms: int | None = None
    finish_reason: str | None = None
    ai_meta: dict[str, Any] = Field(default_factory=dict)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _elapsed_ms(start: datetime) -> int:
    now = datetime.now(start.tzinfo or UTC)
    return int((now - start).total_seconds() * 1000)


def _save(rec: ApiRequestTelemetry | AiModelCallTelemetry) -> None:
    """Persist a telemetry record, best-effort.

    Telemetry must never break request handling, so every failure is swallowed.
    When the record can't be persisted -- storage disabled or write failed -- it
    is logged as JSON so token usage and outcomes stay visible on the console
    rather than being silently dropped.
    """
    row = _to_request_row(rec) if isinstance(rec, ApiRequestTelemetry) else _to_model_row(rec)
    try:
        with session_scope() as session:
            if session is None:
                log.info(
                    "telemetry storage disabled; not persisted: %s",
                    rec.model_dump_json(),
                )
                return
            session.merge(row)
    except Exception as exc:  # never let telemetry break the request
        log.warning(
            "telemetry write failed; not persisted: %s | %s",
            exc,
            rec.model_dump_json(),
        )


# Request span.


def start_request(
    *,
    method: str,
    url: str,
    correlation_id: str,
    caller_identity: str | None = None,
    caller_app: str | None = None,
    parent_request_id: UUID | None = None,
    trace_id: str | None = None,
    app_meta: dict[str, Any] | None = None,
) -> ApiRequestTelemetry:
    """Record the start of an API request and mark it as the active request."""
    rec = ApiRequestTelemetry(
        id=uuid7(),
        created_at=_utcnow(),
        app_name=settings.app_name,
        environment=settings.environment,
        correlation_id=correlation_id,
        trace_id=trace_id,
        parent_request_id=parent_request_id,
        caller_app=caller_app,
        caller_identity=caller_identity,
        method=method,
        url=url,
        app_meta=app_meta or {},
    )
    # Flow id groups one handoff chain: the root (no inherited flow_id) seeds it
    # with its own id; every hop propagates it (see propagation_metadata). Kept in
    # app_meta so trace_id stays reserved for OTel. hop = handoff depth.
    rec.app_meta.setdefault("flow_id", str(rec.id))
    _current_request_id.set(rec.id)
    _current_flow.set((rec.app_meta["flow_id"], int(rec.app_meta.get("hop", 0))))
    _save(rec)
    return rec


def end_request(
    rec: ApiRequestTelemetry,
    *,
    http_status: int,
    outcome: Outcome,
    error_type: str | None = None,
    error_message: str | None = None,
    app_meta: dict[str, Any] | None = None,
) -> ApiRequestTelemetry:
    """Finalize an API request record with its status, outcome and duration."""
    rec.http_status = http_status
    rec.outcome = outcome
    rec.error_type = error_type
    rec.error_message = error_message
    if app_meta:
        rec.app_meta = {**rec.app_meta, **app_meta}
    rec.duration_ms = _elapsed_ms(rec.created_at)
    _save(rec)
    return rec


# Model-call span.


def start_model_call(
    *,
    model_name: str,
    request_id: UUID | None = None,
    identifier: str | None = None,
    ai_meta: dict[str, Any] | None = None,
) -> AiModelCallTelemetry | None:
    """Record the start of a model call, linked to the active request.

    Returns None when there is no request to link to (e.g. a CLI run); the caller
    then simply skips model-call telemetry.
    """
    request_id = request_id or _current_request_id.get()
    if request_id is None:
        return None
    rec = AiModelCallTelemetry(
        id=uuid7(),
        request_id=request_id,
        created_at=_utcnow(),
        identifier=identifier or "",
        model_name=model_name,
        ai_meta=ai_meta or {},
    )
    _save(rec)
    return rec


def end_model_call(
    rec: AiModelCallTelemetry | None,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    finish_reason: str | None = None,
    ai_meta: dict[str, Any] | None = None,
) -> AiModelCallTelemetry | None:
    """Finalize a model-call record with token counts, finish reason, duration."""
    if rec is None:
        return None
    rec.input_tokens = input_tokens
    rec.output_tokens = output_tokens
    rec.finish_reason = finish_reason
    if ai_meta:
        rec.ai_meta = {**rec.ai_meta, **ai_meta}
    rec.duration_ms = _elapsed_ms(rec.created_at)
    _save(rec)
    return rec


# ADK agent-lifecycle callbacks.
#
# The request span above is opened by the HTTP layer (server.py). When an agent
# is instead driven by the ADK CLI (adk run), the dev UI (adk web) or the REST
# server (adk api_server) -- or by main.py -- that HTTP layer is bypassed, so no
# span exists and the per-model-call telemetry in llm.py has nothing to attach
# to (it silently no-ops).
#
# These two callbacks close that gap. Attached to every agent built by
# build_agent, they open a span when the agent run starts and close it when it
# ends -- but only when one isn't already active. Going through server.py the
# guard sees the HTTP span and does nothing (no double-counting); under the ADK
# runners it synthesizes the span so model calls are recorded either way.

# A span this module opened from before_agent_telemetry (so that
# after_agent_telemetry can finalize it). Kept separate from a span opened by
# the HTTP layer, which the callbacks deliberately leave alone.
_agent_span: ContextVar[ApiRequestTelemetry | None] = ContextVar("agent_span", default=None)


def before_agent_telemetry(callback_context) -> None:
    """ADK before-agent callback: open a request span for this invocation.

    No-ops when a span is already active (e.g. one opened by server.py), so
    telemetry is never double-counted. This makes model-call telemetry work
    under adk web / adk run / adk api_server, which bypass the HTTP layer. The
    parameter must be named callback_context -- ADK passes it by keyword.
    """
    if current_request_id() is not None:
        return  # the HTTP layer already owns a span for this request
    try:
        session = getattr(callback_context, "session", None)
        correlation_id = getattr(session, "id", None) or callback_context.invocation_id
        # No HTTP layer here, so there's no authenticated caller: mark the span as
        # a CLI/local invocation (retaining the ADK user id when one is present).
        user_id = getattr(callback_context, "user_id", None)
        rec = start_request(
            method="ADK",
            url=f"adk://{callback_context.agent_name}",
            correlation_id=correlation_id,
            caller_identity=f"cli:{user_id}" if user_id else "cli",
            trace_id=callback_context.invocation_id,
            app_meta={"transport": "adk"},
        )
        _agent_span.set(rec)
    except Exception as exc:  # telemetry must never break the run
        log.warning("before_agent telemetry failed: %s", exc)
    return


def after_agent_telemetry(callback_context) -> None:
    """ADK after-agent callback: close the span opened by ``before_agent_telemetry``.

    Does nothing when this module didn't open the span (the HTTP layer finalizes
    its own with the real status and outcome).
    """
    rec = _agent_span.get()
    if rec is None:
        return
    _agent_span.set(None)
    try:
        end_request(rec, http_status=200, outcome=Outcome.SUCCESS)
    except Exception as exc:  # telemetry must never break the run
        log.warning("after_agent telemetry failed: %s", exc)
    return


# Pydantic -> ORM row.


def _to_request_row(rec: ApiRequestTelemetry) -> ApiRequestTelemetryRow:
    data = rec.model_dump()
    data["outcome"] = rec.outcome.value if rec.outcome else None
    return ApiRequestTelemetryRow(**data)


def _to_model_row(rec: AiModelCallTelemetry) -> AiModelCallTelemetryRow:
    return AiModelCallTelemetryRow(**rec.model_dump())
