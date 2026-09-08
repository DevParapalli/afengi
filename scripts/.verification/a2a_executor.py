"""Verify the A2A executor: task lifecycle, terminal states, coalescing, cancellation.

Drives the real _AgentExecutor against a real EventQueue with the runner stubbed,
so no gateway or model is involved.

Run from the project root (framework/ must be importable):
    PYTHONPATH=. uv run scripts/.verification/a2a_executor.py
"""

import asyncio
import json

from a2a.server.events.event_queue import EventQueueLegacy
from a2a.types import Message, Part, Role, TaskState
from google.protobuf.json_format import MessageToDict

import framework.runner as runner_mod
from framework.a2a_native import _AgentExecutor

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if got == want else 'FAIL'} {label}")


class _FakeAgent:
    name = "verification_agent"


class _Ctx:
    """Minimal RequestContext stand-in: a fresh user message, no current task."""

    current_task = None
    task_id = None
    context_id = None

    def __init__(self) -> None:
        self.message = Message(
            role=Role.ROLE_USER,
            message_id="m1",
            context_id="ctx-1",
            parts=[Part(text="hi")],
        )

    def get_user_input(self) -> str:
        return "hi"


def _stub(final_text: str, *, progress=(), hang: bool = False):
    async def stream(agent, prompt, *, user_id, session_id, attachments=None):
        for kind, payload in progress:
            yield kind, payload
        if hang:
            await asyncio.sleep(3600)
        yield "final_text", {"text": final_text}

    return stream


async def _drain(q: EventQueueLegacy) -> list:
    """Read every queued event. Never close: close() clears the queue."""
    events = []
    while True:
        try:
            events.append(await asyncio.wait_for(q.dequeue_event(), timeout=0.2))
            q.task_done()
        except TimeoutError, asyncio.QueueShutDown, asyncio.QueueEmpty:
            return events


async def _run(stream, ctx=None) -> list:
    runner_mod.stream = stream
    q = EventQueueLegacy()
    await _AgentExecutor(_FakeAgent()).execute(ctx or _Ctx(), q)
    return await _drain(q)


def _meta(msg) -> dict:
    return MessageToDict(msg.metadata) if msg.metadata else {}


async def main() -> None:
    print(
        "Task lifecycle (thought -> tool_call -> tool_result -> tokens -> artifact -> terminal)"
    )
    progress = [
        ("thought", {"text": "picking a tool"}),
        ("tool_call", {"name": "gl_report", "args": {"period": None}}),
        ("tool_result", {"name": "gl_report", "response": {"rows": 0, "blob": b"\x00"}}),
        ("token", {"text": "Which "}),
        ("token", {"text": "accounting "}),
        ("token", {"text": "period?"}),
    ]
    decision = {
        "state": "INPUT_REQUIRED",
        "message": "Which accounting period?",
        "missing_fields": ["transaction_period"],
    }
    events = await _run(_stub(json.dumps(decision), progress=progress))
    kinds = [
        _meta(e.status.message).get("afw.kind")
        for e in events
        if hasattr(e, "status") and e.status.HasField("message")
    ]
    artifacts = [e for e in events if type(e).__name__ == "TaskArtifactUpdateEvent"]

    check("first event is the Task", type(events[0]).__name__, "Task")
    check("thought forwarded", "thought" in kinds, True)
    check("tool_call forwarded", "tool_call" in kinds, True)
    check("tool_result survives bytes payload", "tool_result" in kinds, True)
    check("3 tokens coalesced into 1 update", kinds.count("token"), 1)
    check("response artifact emitted", [a.artifact.name for a in artifacts], ["response"])
    check(
        "terminal state", TaskState.Name(events[-1].status.state), "TASK_STATE_INPUT_REQUIRED"
    )

    print("\nDecision selects the terminal state and rides on its metadata")
    for state, extra, want in [
        ("COMPLETED", {"root_cause": "stale cache"}, "TASK_STATE_COMPLETED"),
        ("INPUT_REQUIRED", {"missing_fields": ["period"]}, "TASK_STATE_INPUT_REQUIRED"),
        ("REJECTED", {"delegate_to": "billing-agent"}, "TASK_STATE_REJECTED"),
        ("FAILED", {"error_code": "E502"}, "TASK_STATE_FAILED"),
    ]:
        events = await _run(_stub(json.dumps({"state": state, "message": "m", **extra})))
        last = events[-1]
        md = _meta(last.status.message)
        check(f"{state} -> {want}", TaskState.Name(last.status.state), want)
        check(f"{state} decision in metadata", md.get("afw.decision", {}).get("state"), state)

    print("\nUnparseable reply fails the task without leaking internals")
    events = await _run(_stub("I could not produce JSON, sorry."))
    last = events[-1]
    check("state", TaskState.Name(last.status.state), "TASK_STATE_FAILED")
    check(
        "generic message",
        "".join(p.text for p in last.status.message.parts),
        "internal error processing the request",
    )

    print("\nCancellation propagates and closes the telemetry span")
    closed: list[str] = []
    import framework.a2a_native as a2a_native

    original = a2a_native.end_request

    def spy(rec, **kw):
        closed.append(str(kw.get("error_type")))
        return original(rec, **kw)

    a2a_native.end_request = spy
    try:
        runner_mod.stream = _stub("{}", progress=[("token", {"text": "x"})], hang=True)
        q = EventQueueLegacy()
        task = asyncio.create_task(_AgentExecutor(_FakeAgent()).execute(_Ctx(), q))
        await asyncio.sleep(0.3)
        task.cancel()
        raised = ""
        try:
            await task
        except asyncio.CancelledError:
            raised = "CancelledError"
        check("CancelledError propagates", raised, "CancelledError")
        check("telemetry span closed", closed, ["CancelledError"])
    finally:
        a2a_native.end_request = original

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("all checks passed")


asyncio.run(main())
