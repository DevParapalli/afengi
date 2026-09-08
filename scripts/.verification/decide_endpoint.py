"""Verify /decide (JSON + SSE), auth, and the agent card through the real FastAPI app.

The runner is stubbed, so no gateway or model is involved.

Run from the project root (framework/ must be importable):
    PYTHONPATH=. uv run scripts/.verification/decide_endpoint.py
"""

import json

import framework.runner as runner_mod

DECISION = {
    "state": "REJECTED",
    "message": "Belongs to billing",
    "delegate_to": "billing-agent",
}


async def _stub(agent, prompt, *, user_id, session_id, attachments=None):
    yield "thought", {"text": "considering"}
    yield "tool_call", {"name": "lookup", "args": {"id": "42"}}
    yield "token", {"text": "Belongs to billing"}
    yield "final_text", {"text": json.dumps(DECISION)}


runner_mod.stream = _stub

from fastapi.testclient import TestClient  # noqa: E402
from google.adk.agents import LlmAgent  # noqa: E402

from framework.agent import CardInfo  # noqa: E402
from framework.config import settings  # noqa: E402
from framework.server import serve  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if got == want else 'FAIL'} {label}")


agent = LlmAgent(
    name="verification_agent",
    model="gpt-4o-mini",
    description="Verification agent.",
    instruction="You are a verification agent.",
    tools=[],
)
app = serve(
    agent, card_info=CardInfo(tags=["demo"], skill_description="Resolves demo requests.")
)
client = TestClient(app)
key = {"X-API-KEY": sorted(settings.valid_api_keys)[0]}

print("Routes")
paths = sorted({r.path for r in app.routes if hasattr(r, "path")})
for want in ("/a2a", "/decide", "/health", "/ready", "/.well-known/agent.json"):
    check(f"{want} mounted", want in paths, True)
for gone in ("/caseiq", "/care"):
    check(f"{gone} gone", gone in paths, False)

print("\nAuth")
check("anonymous rejected", client.post("/decide", json={"text": "x"}).status_code, 401)
check("card is public", client.get("/.well-known/agent.json").status_code, 200)

print("\n/decide (JSON)")
resp = client.post(
    "/decide",
    json={"text": "invoice missing", "metadata": {"category": "Billing", "reference": "REQ-1"}},
    headers=key,
)
body = resp.json()
check("status", resp.status_code, 200)
check("state", body["state"], "REJECTED")
check("decision echoed", body["decision"]["delegate_to"], "billing-agent")
check("contextId minted", bool(body["contextId"]), True)
check("taskId minted", bool(body["taskId"]), True)

print("\n/decide (SSE)")
events = []
with client.stream(
    "POST", "/decide", json={"text": "invoice missing", "stream": True}, headers=key
) as r:
    events.extend(
        line.removeprefix("event: ") for line in r.iter_lines() if line.startswith("event: ")
    )
check("started first", events[0], "started")
check("progress frames", events.count("progress"), 3)
check("final last", events[-1], "final")

print("\nAgent card")
card = client.get("/.well-known/agent.json").json()
check(
    "interfaces",
    [i["url"].rsplit("/", 1)[-1] for i in card["supportedInterfaces"]],
    ["a2a", "decide"],
)
check(
    "skills",
    [s["id"] for s in card["skills"]],
    ["verification_agent_decision"],
)

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all checks passed")
