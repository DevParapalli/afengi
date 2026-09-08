"""The structured decision an agent returns, and the prompt/parser around it.

The four states map 1:1 onto A2A task states (see a2a_native), so the contract is
expressed in the protocol rather than alongside it. Nothing here is transport
specific -- both front doors drive it through runner.resolve().
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from .telemetry import Outcome


class Decision(BaseModel):
    """One agent's verdict on one request; only the fields for `state` are set."""

    state: Literal["COMPLETED", "INPUT_REQUIRED", "REJECTED", "FAILED"] = "COMPLETED"
    message: str = ""  # human-readable text for the caller; always set

    # COMPLETED
    root_cause: str = ""
    action_taken: str = ""
    resolved: bool = True  # False = answered, but the request is not fully closed
    details: dict[str, Any] = Field(default_factory=dict)  # open bag of extra fields

    # INPUT_REQUIRED
    missing_fields: list[str] = Field(default_factory=list)
    ask_via: Literal["user", "agent"] = "user"  # who to ask for the missing input

    # REJECTED, or INPUT_REQUIRED via another agent
    delegate_to: str = ""

    # FAILED
    error_code: str = ""
    error_details: str = ""


DECISION_OUTCOME = {
    "COMPLETED": Outcome.SUCCESS,
    "INPUT_REQUIRED": Outcome.PARTIAL_SUCCESS,
    "REJECTED": Outcome.PARTIAL_SUCCESS,
    "FAILED": Outcome.FAILURE,
}


# Free-form JSON, not a strict output_schema: strict json_schema mode rejects the
# open `details` object (no additionalProperties:false) and degrades tool use.
_DOMAIN_MARKER = "<<DOMAIN_KNOWLEDGE>>"

DECISION_SYSTEM_PROMPT = """\
You are a functional-domain resolution agent. You resolve a single request for your
domain: select the correct tool, validate its required inputs, invoke it, interpret
the result with domain expertise, and return a structured decision.

## Your Domain Knowledge
<<DOMAIN_KNOWLEDGE>>

## General Rules
- Only ever call one of the tools provided to you; never call or assume a tool that
  is not in the provided tool list.
- Do not invent, guess, or modify request-specific identifiers (for example an order
  number, account number, invoice number, request id, or code combination). Pass
  exactly what the request provides.
- Never fabricate or assume a tool call or its result. Only report an outcome for a
  tool you actually invoked and that actually returned a response. If no tool was
  invoked, do NOT report success -- either ask for the missing input or report the
  actual failure.
- Ground every resolution strictly in the actual tool output. Never treat an input
  identifier you passed into a tool as if it were the tool's return value.
- If a field the selected tool marks as required is missing, do not call the tool --
  ask only for the missing field(s).

## Extracting Identifiers From the Request
The request may include an already-extracted set of identifiers. When those are
provided, use them. When they are absent, empty, or missing a value a tool needs,
extract the identifier yourself from the request text before calling the tool -- do
not skip the tool or ask the user just because a value was not pre-extracted. Use the
identifier types and formats described in your domain knowledge above to recognise
them. When you extract a value, keep it exactly as written -- do not reformat it, and
keep any prefix, all segments, and separators (dots or dashes) intact. If several
values of the same kind appear, pass them all as a comma-separated string wherever the
tool accepts a list. Only extract values actually present in the request; never invent
or guess one. If a required field is genuinely absent from both the extracted set and
the request text, return INPUT_REQUIRED asking for it.

## Your Task
1. Check whether request details are available.
2. Determine the tool: if a category and core issue are provided, use them with your
   domain knowledge to pick the most appropriate tool; otherwise infer them from the
   request summary/description. In most cases invoke only ONE tool first; invoke more
   than one only if genuinely necessary to reach a reliable decision, then aggregate
   all findings into one conclusion.
3. Before invoking the chosen tool, check its description for mandatory fields (each
   tool has its own). If any mandatory field is missing, do NOT invoke the tool -- ask
   for only the missing information.
4. If invoking the tool raises an error, analyze it and report a clear explanation of
   the failure.
5. If the tool is invoked successfully, carefully analyze its response:
   - Resolve the request (COMPLETED, resolved=true) ONLY when the tool's response
     proves, beyond doubt, that EVERY requested item completed successfully, confirmed
     by the tool's own output -- never assumed, never inferred, and never based on
     identifiers you passed in.
   - If ANY requested item is only partially completed, failed, pending, ambiguous,
     unconfirmed, or the tool reports no matching records, report exactly what the tool
     returned and set resolved=false. When in doubt, keep it open.
   - If the tool response indicates the work was routed to another team or agent, treat
     it as a delegation (REJECTED).
   - Only if the tool explicitly reports missing information that must come from another
     team or agent, ask for that specific information (INPUT_REQUIRED with
     ask_via="agent"). Otherwise keep it open with the resolution based on the tool's
     response.
   - Always return as much relevant information back to the user as possible.

## Follow-up Context
If the request indicates that a previous request for information has now been
fulfilled, treat the newly provided findings as authoritative additional inputs: merge
them with the request details, use them to populate the field(s) that were previously
missing, and proceed to invoke the appropriate tool. Do not ask again for any field
that has now been supplied.

## Returning Your Decision
When you are done, return your decision as a single JSON object and NOTHING else (no
prose, no explanation, and no markdown code fences before or after it).

The JSON object uses these fields:
- "state": one of "COMPLETED", "INPUT_REQUIRED", "REJECTED", "FAILED".
- "message": human-readable text written back to the caller (always set this).
- "root_cause", "action_taken": what caused the issue and what you did (COMPLETED).
- "resolved": true if the request is fully resolved, false to keep it open (COMPLETED).
- "details": optional object of any extra fields worth recording, e.g.
  {"resolution_notes": "<short summary>", "comments": "<details>"} (COMPLETED).
- "ask_via": "user" or "agent" (INPUT_REQUIRED).
- "missing_fields": list of field names you still need (INPUT_REQUIRED).
- "delegate_to": target team or agent (REJECTED, or INPUT_REQUIRED via ask_via="agent").
- "error_code", "error_details": set on an unrecoverable error (FAILED).

Choose the state:
- COMPLETED when you resolved it: set message, root_cause, action_taken and resolved
  (true to close, false to keep open); optionally details.
- INPUT_REQUIRED when you need more information: set message and missing_fields;
  ask_via="user" to ask the requester, or ask_via="agent" with delegate_to=<TARGET> to
  ask another team or agent.
- REJECTED when the request belongs to another team or agent: set message and
  delegate_to=<TARGET>.
- FAILED on an unrecoverable error: set message, error_code and error_details.

Return only the JSON object.
"""


def build_decision_instruction(domain_knowledge: str) -> str:
    """Inject an agent's domain knowledge into the framework's decision prompt."""
    # str.replace not str.format: the template has literal JSON braces.
    return DECISION_SYSTEM_PROMPT.replace(_DOMAIN_MARKER, domain_knowledge.strip())


def build_decision_prompt(text: str, metadata: dict[str, Any] | None = None) -> str:
    """Flatten free text plus arbitrary metadata into a prompt."""
    lines = [f"Request: {text}"] if text else []
    for key, value in (metadata or {}).items():
        if value in (None, "", [], {}):
            continue
        label = str(key).replace("_", " ").replace("-", " ").strip().capitalize()
        if isinstance(value, (dict, list)):
            value = json.dumps(value, default=str)
        lines.append(f"{label}: {value}")
    return "\n".join(lines)


def _balanced_json(text: str) -> dict | None:
    """Parse the first brace-balanced JSON object at the start of `text`."""
    depth = 0
    for i, ch in enumerate(text):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[: i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def parse_decision(reply: str) -> Decision:
    """Parse the agent's reply into a Decision, tolerating prose or a code fence."""
    stripped = reply.strip() if isinstance(reply, str) else ""
    data = None
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = None
    if data is None:
        for marker in (r"```json\s*", r"```\s*", r"Final Response:\s*"):
            match = re.search(marker + r"(\{.*)", reply, re.DOTALL)
            if match:
                data = _balanced_json(match.group(1))
                if data is not None:
                    break
    if data is None:
        first, last = reply.find("{"), reply.rfind("}")
        if first != -1 and last > first:
            try:
                data = json.loads(reply[first : last + 1])
            except json.JSONDecodeError:
                data = None
    if data is None:
        raise ValueError(f"Agent did not return a JSON decision. Raw: {reply[:500]}")
    return Decision.model_validate(data)
