# docs/

Maintainer notes for the agent-framework **base repo**. Nothing here is shipped:
`scripts/build_zips.py` packages by explicit file globs, so this folder never
enters `framework.zip` / `agent-template.zip`.

## Contents

- **conventions.md** -- architecture notes and repo conventions: text/comment
  style, package management (uv), the framework/agent split, the `Connection`
  bundle, the LLM gateway pattern, contract layering (native A2A + `/decide`),
  the decision contract, streaming, attachments, persistence, telemetry,
  security, delegation and deployment. Read it before
  making non-trivial changes, and keep it in sync when conventions change.
- **telemetry-queries.md** -- reusable SQL for reconstructing a request flow
  across agents and for per-request / per-agent / per-day cost reporting.
