"""Running an agent: the ADK seam, and the single resolution path on top of it.

run() and stream() are the raw ADK calls (run() is for scripts and the CLI).
resolve() is what the server uses: prompt -> stream -> Decision, with persistence
and telemetry, shared by both front doors (/a2a and /decide) so they behave
identically.

Degrades safely but never silently: a recoverable failure is logged at WARN and
surfaced on Resolution.warnings rather than failing the request. Anything worse
becomes a FAILED Decision (real detail to the log and telemetry, never to the
caller); cancellation propagates untouched.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import InMemoryRunner
from google.genai import types

from .attachments import collect_attachments
from .decision import DECISION_OUTCOME, Decision, build_decision_prompt, parse_decision
from .delegation import execute_handoff, subagents_for
from .models import load_decision_state, save_decision_state
from .telemetry import ApiRequestTelemetry, Outcome, end_request

log = logging.getLogger(__name__)

# Sent to the caller on any unhandled failure; real detail stays in log+telemetry.
GENERIC_ERROR = "internal error processing the request"

ProgressHook = Callable[[str, dict], Awaitable[None]]


async def _apply_handoff(
    runner: InMemoryRunner, agent, user_id: str, session_id: str, message: str, reply: str
) -> str:
    """After the model turn, perform a recorded handoff (if any) and return the
    downstream reply verbatim; otherwise return the model's own reply."""
    subs = subagents_for(agent.name)
    if not subs:
        return reply
    session = await runner.session_service.get_session(
        app_name=agent.name, user_id=user_id, session_id=session_id
    )
    handoff = await execute_handoff(subs, session.state if session else {}, message, session_id)
    if handoff is None:
        return reply
    if handoff.outcome == "unavailable":
        return handoff.text or "Downstream agent unavailable; manual review required."
    return handoff.text


async def _drain_attachments(
    runner: InMemoryRunner, app_name: str, user_id: str, session_id: str
) -> list[dict]:
    """Read the file attachments stashed in the finished session's state."""
    session = await runner.session_service.get_session(
        app_name=app_name, user_id=user_id, session_id=session_id
    )
    return collect_attachments(session.state) if session else []


async def run(
    agent,
    message: str,
    user_id: str = "local",
    session_id: str = "local",
    *,
    attachments: list | None = None,
) -> str:
    """Send one message to agent and return its final text reply.

    When attachments is provided, any files the tools produced (stripped from the
    model's context by strip_attachments) are appended to it after the run.
    """
    runner = InMemoryRunner(agent=agent, app_name=agent.name)
    # ADK generates a fresh id when session_id is empty/None, so use the id it
    # actually assigned -- otherwise run_async can't find the session.
    session = await runner.session_service.create_session(
        app_name=agent.name, user_id=user_id, session_id=session_id
    )
    session_id = session.id
    content = types.Content(role="user", parts=[types.Part(text=message)])

    reply = ""
    async for event in runner.run_async(
        user_id=user_id, session_id=session_id, new_message=content
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text and not part.thought:
                    reply = part.text
    reply = await _apply_handoff(runner, agent, user_id, session_id, message, reply)
    if attachments is not None:
        attachments.extend(await _drain_attachments(runner, agent.name, user_id, session_id))
    return reply


async def stream(
    agent,
    message: str,
    user_id: str = "local",
    session_id: str = "local",
    *,
    attachments: list | None = None,
):
    """Run agent and yield (kind, payload) progress tuples as it works.

    Emits ("thought", {...}), ("tool_call", {...}), ("tool_result", {...}) and
    ("token", {"text": <chunk>}) as they happen, then a final ("final_text",
    {"text": <reply>}) with the same text run would return. Runs in ADK SSE
    streaming mode so the model's answer arrives as incremental token chunks;
    partial function-call chunks are skipped (we emit the aggregated tool_call).
    Lets callers stream progress (e.g. over SSE).

    When attachments is provided, any files the tools produced are appended to it
    before the final ("final_text", ...) tuple is yielded.
    """
    runner = InMemoryRunner(agent=agent, app_name=agent.name)
    # ADK generates a fresh id when session_id is empty/None, so use the id it
    # actually assigned -- otherwise run_async can't find the session.
    session = await runner.session_service.create_session(
        app_name=agent.name, user_id=user_id, session_id=session_id
    )
    session_id = session.id
    content = types.Content(role="user", parts=[types.Part(text=message)])

    reply = ""
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=content,
        run_config=RunConfig(streaming_mode=StreamingMode.SSE),
    ):
        if not (event.content and event.content.parts):
            continue
        for part in event.content.parts:
            if part.function_call:
                # SSE emits progressive arg chunks (partial) then one aggregated
                # call; only the aggregated one carries the full args.
                if event.partial:
                    continue
                args = dict(part.function_call.args) if part.function_call.args else {}
                yield "tool_call", {"name": part.function_call.name, "args": args}
            elif part.function_response:
                yield (
                    "tool_result",
                    {
                        "name": part.function_response.name,
                        "response": part.function_response.response,
                    },
                )
            elif part.text and part.thought:
                # Model reasoning: stream on the aggregated event only (skip the
                # partial chunks) to keep it distinct from answer tokens.
                if not event.partial:
                    yield "thought", {"text": part.text}
            elif part.text:
                # Partial text = an answer chunk (token); the aggregated non-partial
                # text is the full reply (kept, not re-emitted, to avoid duplication).
                if event.partial:
                    yield "token", {"text": part.text}
                else:
                    reply = part.text
    reply = await _apply_handoff(runner, agent, user_id, session_id, message, reply)
    if attachments is not None:
        attachments.extend(await _drain_attachments(runner, agent.name, user_id, session_id))
    yield "final_text", {"text": reply}


@dataclass
class Resolution:
    """The outcome of one resolve() call."""

    decision: Decision
    attachments: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


async def resolve(
    agent,
    *,
    text: str,
    context_id: str,
    task_id: str = "",
    rec: ApiRequestTelemetry | None = None,
    metadata: dict[str, Any] | None = None,
    user_id: str = "agent",
    warnings: list[str] | None = None,
    on_progress: ProgressHook | None = None,
) -> Resolution:
    """Run one request through the agent and return its Decision.

    rec, when supplied, is closed here -- callers must not close it again.
    warnings seeds Resolution.warnings, so a caller can pass in its own.
    """
    warnings = list(warnings or [])
    prompt = build_decision_prompt(text, metadata)

    # Replay a prior decision so a follow-up can resume an open task.
    prior = load_decision_state(context_id, task_id) if task_id else None
    if prior and prior.get("decision"):
        prompt += f"\n\nPrior decision on this request: {prior['decision']}"

    attachments: list[dict] = []
    reply = ""
    try:
        async for kind, payload in stream(
            agent, prompt, user_id=user_id, session_id=context_id, attachments=attachments
        ):
            if kind == "final_text":
                reply = payload["text"]
                continue
            if on_progress is not None:
                await on_progress(kind, payload)
        decision = parse_decision(reply)
    except Exception as exc:  # CancelledError is BaseException: not caught here
        log.exception("resolve failed (context=%s task=%s)", context_id, task_id)
        if rec is not None:
            end_request(
                rec,
                http_status=200,
                outcome=Outcome.ERROR,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        failed = Decision(state="FAILED", message=GENERIC_ERROR, error_details=GENERIC_ERROR)
        return Resolution(failed, warnings=warnings)

    save_decision_state(
        context_id=context_id,
        task_id=task_id,
        state=decision.state,
        decision=decision.model_dump(),
        request_details={"text": text, "metadata": dict(metadata or {})},
    )
    if rec is not None:
        end_request(
            rec,
            http_status=200,
            outcome=DECISION_OUTCOME[decision.state],
            app_meta={"state": decision.state, "task_id": task_id},
        )
    return Resolution(decision, attachments, warnings)
