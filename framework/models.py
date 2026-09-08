"""SQLAlchemy models and the load/save helpers over them.

Tables are deliberately independent (no cross-FKs) so each evolves separately:
DecisionState (the last decision for a task) and the two telemetry rows.

Every JSON payload uses PortableJSON (JSONB on PostgreSQL, JSON-as-text on
Oracle). All String columns carry explicit lengths (Oracle VARCHAR2 requires it).
The helpers use session_scope, so they no-op when no DB is configured and never
raise into request handling; writes upsert via Session.merge (portable across
PostgreSQL and Oracle -- no ON CONFLICT / MERGE).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .config import settings
from .db import Base, PortableJSON, session_scope

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# Decision persistence: the last decision for a task.


class DecisionState(Base):
    """Persisted decision state for one task.

    Keyed by (context_id, task_id) so a follow-up request can resume a task a
    prior INPUT_REQUIRED decision left open. decision stores the last Decision;
    request_details the flattened incoming request (text + free-form metadata).
    """

    __tablename__ = settings.table(settings.decision_state_table)

    context_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    state: Mapped[str] = mapped_column(String(32))
    decision: Mapped[dict] = mapped_column(PortableJSON, default=dict)
    request_details: Mapped[dict] = mapped_column(PortableJSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


# Telemetry: one row per API request.


class ApiRequestTelemetryRow(Base):
    """One row per inbound API request (mirrors ApiRequestTelemetry)."""

    __tablename__ = settings.table(settings.telemetry_api_request_table)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    app_name: Mapped[str] = mapped_column(String(128))
    environment: Mapped[str] = mapped_column(String(32), default="prod")
    correlation_id: Mapped[str] = mapped_column(String(255))
    trace_id: Mapped[str | None] = mapped_column(String(255))  # reserved for an OTel trace id
    parent_request_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True)
    )  # calling agent's span (NULL = root)
    caller_app: Mapped[str | None] = mapped_column(String(255))
    caller_identity: Mapped[str | None] = mapped_column(String(255))
    method: Mapped[str] = mapped_column(String(16))
    url: Mapped[str] = mapped_column(String(1024))
    http_status: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str | None] = mapped_column(String(32))
    error_type: Mapped[str | None] = mapped_column(String(255))
    error_message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    app_meta: Mapped[dict] = mapped_column(PortableJSON, default=dict)


# Telemetry: one row per model call (span).


class AiModelCallTelemetryRow(Base):
    """One row per model call (mirrors AiModelCallTelemetry).

    request_id is a soft FK into telemetry_api_request.id -- no DB-level
    constraint, so a model call can be recorded even if the request row write
    was skipped.
    """

    __tablename__ = settings.table(settings.telemetry_model_call_table)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    identifier: Mapped[str] = mapped_column(String(128))
    model_name: Mapped[str] = mapped_column(String(128))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    finish_reason: Mapped[str | None] = mapped_column(String(64))
    ai_meta: Mapped[dict] = mapped_column(PortableJSON, default=dict)


def load_decision_state(context_id: str, task_id: str) -> dict[str, Any] | None:
    """Return the persisted state for a task, or None if there is none."""
    if not context_id or not task_id:
        return None
    try:
        with session_scope() as session:
            if session is None:
                return None
            row = session.get(DecisionState, (context_id, task_id))
            if row is None:
                return None
            return {
                "context_id": row.context_id,
                "task_id": row.task_id,
                "state": row.state,
                "decision": dict(row.decision or {}),
                "request_details": dict(row.request_details or {}),
            }
    except Exception as exc:
        log.warning("load_decision_state failed: %s", exc)
        return None


def save_decision_state(
    *,
    context_id: str,
    task_id: str,
    state: str,
    decision: dict[str, Any],
    request_details: dict[str, Any],
) -> None:
    """Upsert the state for a task (keyed by contextId + taskId)."""
    if not context_id or not task_id:
        return
    try:
        with session_scope() as session:
            if session is not None:
                session.merge(
                    DecisionState(
                        context_id=context_id,
                        task_id=task_id,
                        state=state,
                        decision=decision,
                        request_details=request_details,
                    )
                )
    except Exception as exc:
        log.warning("save_decision_state failed: %s", exc)
