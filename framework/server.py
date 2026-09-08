"""Serve one agent over HTTP with these endpoints:

- GET  /.well-known/agent.json: the agent card (discovery, public),
- POST /a2a: the native Agent2Agent protocol, verbatim (see a2a_native.py) --
  the stock A2A v1.0 JSON-RPC served by the SDK reference handler, and the
  primary contract: the agent's structured Decision picks the terminal task
  state and rides on the task's metadata,
- POST /decide: the same structured decision over plain JSON, for callers that
  do not speak A2A. A thin front door onto the identical resolve() path.

Every /decide request is authenticated (see security.py) and recorded
as telemetry (see telemetry.py). Telemetry is written regardless of the auth
result -- a rejected request still produces a record with its outcome and error
message set. Decision state is persisted when a DB is
configured. /a2a is authenticated by a path-scoped middleware and records run
telemetry in its executor. Everything here is written once; agents never touch it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated
from uuid import uuid7

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    APIKeySecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from google.adk.agents import LlmAgent
from google.protobuf.json_format import MessageToDict

from .a2a_native import RPC_PATH, mount_native_a2a
from .agent import CardInfo
from .attachments import strip_attachments
from .config import settings
from .db import check_connection, init_db
from .decision import build_decision_instruction
from .runner import resolve
from .security import authenticate
from .telemetry import Outcome, end_request, start_request

log = logging.getLogger(__name__)

# Emit an SSE keep-alive if the agent produces no event for this long (seconds).
_STREAM_HEARTBEAT = 15.0

DECIDE_EXAMPLES: dict = {
    "default": {
        "summary": "A request with free-form metadata",
        "value": {
            "text": "Invoice not generated for order 117795496",
            "metadata": {"category": "Billing", "reference": "REQ-1201"},
        },
    },
    "streaming": {
        "summary": "Stream progress over SSE",
        "value": {"text": "What is the status of order 117795496?", "stream": True},
    },
    "followUp": {
        "summary": "Resume an INPUT_REQUIRED task",
        "value": {
            "text": "The accounting period is MAR-26.",
            "contextId": "01a08d4e-9b21-7c3a-8f10-2be7c4d9a015",
            "taskId": "01a08d4e-9b22-7f5d-b481-6e0a3c17d9f4",
        },
    },
}


def _sse(event: str, data: dict) -> str:
    """Format one Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _build_agent_card(agent: LlmAgent, card_info: CardInfo, base_url: str) -> AgentCard:
    """The public A2A agent card. Advertises three transports for the same agent:
    native A2A (JSONRPC /a2a), the structured decision over plain JSON (/decide),
    base_url is the caller-visible origin (no trailing slash)."""
    base = base_url.rstrip("/")
    description = agent.description or f"{agent.name} functional agent."
    # Only advertise the API-key scheme when keys are configured.
    security_schemes = None
    security_requirements = None
    if settings.valid_api_keys:
        security_schemes = {
            "apiKey": SecurityScheme(
                api_key_security_scheme=APIKeySecurityScheme(
                    location="header", name="X-API-KEY"
                )
            )
        }
        security_requirements = [SecurityRequirement(schemes={"apiKey": StringList(list=[])})]
    return AgentCard(
        name=agent.name,
        description=description,
        version=settings.app_version,
        documentation_url=card_info.documentation_url,
        supported_interfaces=[
            AgentInterface(protocol_binding="JSONRPC", url=f"{base}{RPC_PATH}"),
            AgentInterface(protocol_binding="HTTP+JSON", url=f"{base}/decide"),
        ],
        provider=AgentProvider(organization=settings.app_name, url=base),
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=False,
        ),
        security_schemes=security_schemes,
        security_requirements=security_requirements,
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
        skills=[
            AgentSkill(
                id=f"{agent.name}_decision",
                name=f"{agent.name} structured resolution",
                description=(
                    # Agent-owned functional description (or a generic
                    # fallback) followed by the fixed A2A contract guarantee.
                    (
                        card_info.skill_description
                        or "Resolves a request for this functional domain."
                    )
                    + " Returns a COMPLETED / INPUT_REQUIRED / REJECTED / FAILED"
                    " task per the A2A contract."
                ),
                tags=card_info.tags,
                examples=card_info.examples,
                input_modes=["text/plain", "application/json"],
                output_modes=["text/plain", "application/json"],
            ),
        ],
    )


def serve(agent: LlmAgent, *, card_info: CardInfo | None = None) -> FastAPI:
    """Build the FastAPI app exposing agent over /a2a, /decide and the card.

    The card's domain flavour (skill tags, examples, description, docs link) comes
    from card_info; the framework owns the structure and the A2A contract wording.
    """
    card_info = card_info or CardInfo()
    if not settings.auth_configured:
        log.warning(
            "AUTH DISABLED: neither API_KEYS nor OIDC_ISSUER is set, so every "
            "endpoint accepts unauthenticated requests. Set API_KEYS (or "
            "OIDC_ISSUER) before exposing %s outside a trusted network.",
            agent.name,
        )
    app = FastAPI(title=agent.name)

    @app.middleware("http")
    async def _limit_body_size(request: Request, call_next):
        """Reject an over-large body early (413) by Content-Length, before it is
        buffered. Bounds per-request memory; see settings.max_request_bytes."""
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                too_big = int(content_length) > settings.max_request_bytes
            except ValueError:
                too_big = False
            if too_big:
                return JSONResponse(
                    status_code=413, content={"detail": "request body too large"}
                )
        return await call_next(request)

    # Create tables if a database is configured; never block serving on it.
    try:
        init_db()
    except Exception as exc:  # pragma: no cover - environment dependent
        log.warning("init_db failed (persistence/telemetry disabled): %s", exc)

    # Structured sibling for the decision contract: same brain, but its final
    # answer is a single JSON object parsed into a Decision (see decision.py).
    decision_agent = LlmAgent(
        name=f"{agent.name}_decision",
        model=agent.model,
        instruction=build_decision_instruction(str(agent.instruction)),
        tools=agent.tools,
        after_tool_callback=strip_attachments,
    )

    @app.get("/.well-known/agent.json")
    def agent_card(request: Request) -> dict:
        base = str(request.base_url).rstrip("/")
        return MessageToDict(_build_agent_card(agent, card_info, base))

    @app.get("/health")
    def health() -> dict:
        """Liveness: the process is up and serving. No dependency checks -- a DB
        blip must not restart the pod. For k8s livenessProbe / Docker HEALTHCHECK."""
        return {
            "status": "healthy",
            "service": settings.app_name,
            "version": settings.app_version,
        }

    @app.get("/ready")
    def ready():
        """Readiness: dependencies are serviceable. Pings the DB when configured
        (503 while it's down so k8s stops routing traffic until it recovers); when
        no DB is configured there is nothing to wait on, so always ready."""
        db_state = check_connection()  # True healthy / False down / None disabled
        if db_state is False:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "database": "disconnected"},
            )
        return {
            "status": "ready",
            "database": "connected" if db_state else "not_configured",
        }

    @app.post("/decide")
    async def decide(
        request: Request,
        body: Annotated[dict, Body(openapi_examples=DECIDE_EXAMPLES)],
    ):
        """Structured decision over plain JSON: the same resolve() path as /a2a."""
        text = str(body.get("text") or body.get("message") or "")
        warnings: list[str] = []
        metadata = body.get("metadata") or {}
        if not isinstance(metadata, dict):
            warning = f"Ignored non-object metadata of type {type(metadata).__name__}."
            log.warning(warning)
            warnings.append(warning)
            metadata = {}
        context_id = str(body.get("contextId") or body.get("context_id") or "")
        generated_context_id = not context_id
        if generated_context_id:
            context_id = str(uuid7())
        task_id = str(body.get("taskId") or body.get("task_id") or "") or str(uuid7())

        identity = authenticate(request.headers)
        rec = start_request(
            method="POST",
            url=str(request.url),
            correlation_id=context_id,
            caller_identity=identity.id if identity else None,
        )
        if generated_context_id:
            log.info(
                "No contextId supplied; generated correlation id '%s' for this request.",
                context_id,
            )
            rec.app_meta = {**rec.app_meta, "generated_correlation_id": True}

        if identity is None or not identity.authenticated:
            reason = identity.reason if identity else "no identity presented"
            end_request(
                rec,
                http_status=401,
                outcome=Outcome.ERROR,
                error_type="AuthError",
                error_message=reason,
            )
            raise HTTPException(status_code=401, detail=reason)

        def envelope(result) -> dict:
            return {
                "contextId": context_id,
                "taskId": task_id,
                "state": result.decision.state,
                "decision": result.decision.model_dump(),
                "warnings": result.warnings,
                "artifacts": result.attachments,
                "agent_name": agent.name,
            }

        flag = str(body.get("stream") or body.get("streaming") or "").strip().lower()
        if flag in {"1", "true", "yes", "on"}:

            async def event_stream():
                yield _sse("started", {"contextId": context_id, "taskId": task_id})
                queue: asyncio.Queue = asyncio.Queue()

                async def on_progress(kind: str, payload: dict) -> None:
                    await queue.put((kind, payload))

                async def produce():
                    try:
                        return await resolve(
                            decision_agent,
                            text=text,
                            metadata=metadata,
                            context_id=context_id,
                            task_id=task_id,
                            rec=rec,
                            user_id="decide",
                            warnings=warnings,
                            on_progress=on_progress,
                        )
                    finally:
                        await queue.put((None, None))

                worker = asyncio.create_task(produce())
                try:
                    while True:
                        try:
                            kind, payload = await asyncio.wait_for(
                                queue.get(), timeout=_STREAM_HEARTBEAT
                            )
                        except TimeoutError:
                            yield _sse("keep_alive", {"contextId": context_id})
                            continue
                        if kind is None:
                            break
                        yield _sse("progress", {"type": kind, **payload})
                    result = await worker
                except BaseException:
                    worker.cancel()
                    raise
                yield _sse("final", envelope(result))

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "close",
                    "X-Accel-Buffering": "no",
                },
            )

        result = await resolve(
            decision_agent,
            text=text,
            metadata=metadata,
            context_id=context_id,
            task_id=task_id,
            rec=rec,
            user_id="decide",
            warnings=warnings,
        )
        return envelope(result)

    # Native A2A: stock Agent2Agent protocol served by the SDK's reference handler
    # (see a2a_native). Wraps the structured decision brain, so an A2A task ends in
    # the state the agent's Decision selected; the card carries the public agent
    # identity, not the internal _decision sibling name.
    host = "localhost" if settings.host in ("", "0.0.0.0") else settings.host
    native_card = _build_agent_card(agent, card_info, f"http://{host}:{settings.port}")
    mount_native_a2a(app, decision_agent, native_card)

    @app.middleware("http")
    async def _guard_native_a2a(request: Request, call_next):
        # The SDK owns the /a2a routes, so authentication is gated here for parity
        # with /decide (run telemetry is recorded inside the executor).
        # authenticate() self-disables when nothing is configured to check against.
        if request.url.path.startswith(RPC_PATH):
            identity = authenticate(request.headers)
            if identity is None or not identity.authenticated:
                reason = identity.reason if identity else "no identity presented"
                return JSONResponse(
                    status_code=401,
                    content={
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32001, "message": reason},
                    },
                )
        return await call_next(request)

    return app
