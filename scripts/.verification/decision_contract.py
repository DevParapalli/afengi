"""Verify the decision contract: JSON recovery, prompt flattening, Struct sanitizing.

Run from the project root (framework/ must be importable):
    PYTHONPATH=. uv run scripts/.verification/decision_contract.py
"""

import datetime
import json

from google.protobuf.struct_pb2 import Struct

from framework.a2a_native import _progress_text, _struct_safe
from framework.decision import build_decision_prompt, parse_decision

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if got == want else 'FAIL'} {label}")


print("JSON recovery from model output")
fenced = '```json\n{"state": "REJECTED", "message": "nope", "delegate_to": "billing"}\n```'
check("fenced json", parse_decision(fenced).delegate_to, "billing")
check("bare json", parse_decision('{"state": "FAILED", "error_code": "E1"}').error_code, "E1")
check(
    "json wrapped in prose",
    parse_decision(
        'Thinking...\n{"state": "COMPLETED", "message": "done"} -- hope that helps'
    ).message,
    "done",
)
try:
    parse_decision("no json here")
    failures.append("unparseable reply should raise")
    print("  FAIL unparseable raises")
except ValueError:
    print("  ok   unparseable raises")

print("\nPrompt flattening (free-form metadata, empties dropped)")
prompt = build_decision_prompt(
    "Invoice missing for order 42",
    {"category": "Billing", "context_extracted": {"order": "42"}, "empty": "", "none": None},
)
check("request line", prompt.splitlines()[0], "Request: Invoice missing for order 42")
check("label cased", "Category: Billing" in prompt, True)
check("nested json-encoded", 'Context extracted: {"order": "42"}' in prompt, True)
check("empty dropped", "Empty" in prompt, False)
check("none dropped", "None:" in prompt, False)

print("\nprotobuf Struct sanitizing (bytes/datetime would raise otherwise)")
hostile = {
    "blob": b"\x00\xff",
    "when": datetime.datetime.now(),
    "n": 3,
    "nested": {"a": [1, 2]},
}
try:
    Struct().update({"raw": hostile})
    failures.append("expected raw payload to be rejected by Struct")
    print("  FAIL raw payload rejected")
except ValueError:
    print("  ok   raw payload rejected by Struct")
s = Struct()
s.update({"safe": _struct_safe(hostile)})
check("sanitized accepted", "safe" in s, True)
check(
    "sanitized is json-round-trippable", json.loads(json.dumps(_struct_safe(hostile)))["n"], 3
)

print("\nProgress text")
check("tool_call", _progress_text("tool_call", {"name": "gl_report"}), "Calling gl_report...")
check(
    "tool_result",
    _progress_text("tool_result", {"name": "gl_report"}),
    "Received result from gl_report",
)
check("token passthrough", _progress_text("token", {"text": "hi"}), "hi")

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all checks passed")
