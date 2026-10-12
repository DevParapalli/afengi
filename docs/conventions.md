# Architecture notes and repo conventions

Design rationale for the framework: the decisions that are not obvious from
reading the code, and the conventions to keep when extending it. Written for a
senior engineer returning after a break.

## Text / comment style

- ASCII only. No em-dashes, box-drawing, smart quotes, emoji or unicode arrows.
  Use plain `--`, `->`, `#`. Applies to source, comments, docstrings and config.
- Terse and technical. Comments explain DECISIONS and rationale, not what the
  code plainly does. Jargon (soft-FK, upsert, fail-closed) is fine; do not
  dumb it down.
- No `# ----` divider blocks. Inline markdown in comments is fine.
- Docstrings: one line where possible. No full-function writeups.

## Package management

- **uv** for everything (not pip/poetry). `uv add <pkg>`, `uv run <cmd>`,
  `uv sync`.
- `[tool.uv] package = false`: this is an app, not a distributable. uv installs
  dependencies but does not build the project; `framework/` and `agent/` are
  imported from source with the project root on `sys.path` (so run from the
  root: `uv run main.py`, `adk web .`).
- Version is read from `pyproject.toml [project].version` at runtime via
  `tomllib` (see `framework/config.py`), not from installed dist metadata.
- **MCP protocol needs an ADK flag.** `mcp>=2.3` is pinned for protocol
  `2026-07-28` (stateless), but ADK 2.11 only negotiates it with
  `ADK_ENABLE_MCP_MODERN_PROTOCOL=1` in the process environment; otherwise it
  silently falls back to `2025-11-25`. Both Dockerfiles set it, and
  `mcp_toolset()` refuses to build without it. `.env` is read by pydantic but not
  exported, so local runs need `uv run --env-file .env main.py`.

## Layout: the framework / agent split

The whole design rests on one boundary.

- `framework/` -- the shared runtime. Identical across every agent built on it.
  Owns the wire contracts, serving, auth, persistence, telemetry and the LLM
  gateway. **Agents never edit it.**
- `agent/` -- one specific agent: its prompt (domain knowledge only), its tools,
  its config. **This is the only code you change to build a new agent.**

The framework holds NO credentials. Every credential and endpoint lives in
`agent/config.py` and reaches the framework as a `Connection`. This keeps the
framework shippable as an immutable base.

`framework/__init__.py` re-exports the agent-facing API. Internals (models,
telemetry, security, db, wire contracts) are imported from their submodules and
deliberately not re-exported -- agents have no business touching them.

Keep the file count honest: one concern per file, and a concern that has shrunk
gets folded into its neighbour rather than kept alive for symmetry. `resolve()`
lives beside `run()`/`stream()` in `runner.py`; the load/save helpers live beside
the models they touch; the A2A client lives with the delegation that is its only
caller.

**Naming gotcha:** never give a submodule the same name as a symbol re-exported
from it in `__init__.py`. `from .resolve import resolve` rebinds the package
attribute `framework.resolve` from the module to the function, so
`import framework.resolve` silently yields the function. That is why the module
was `framework/resolution.py` while the function stayed `resolve()`; both now
live in `framework/runner.py`.

## Connection: one bundle for every remote

`framework/connection.py` defines a single frozen `Connection` dataclass used by
every connector -- LLM gateway, downstream API, MCP server, downstream agent:

    url            primary endpoint (base URL, or agent CARD url for an agent)
    invoke_url     optional concrete-call override (e.g. an A2A JSON-RPC target)
    token_url      OAuth2 client-credentials token endpoint
    client_id / client_secret / basic_auth
    api_key / api_key_header / user_id
    timeout        per-call default, seconds

Each consumer reads only the fields it needs and **degrades on empties**: no
`client_id` means no bearer is minted; no `api_key` means that header is not
sent. One type covers every remote, so adding a second API or a downstream agent
is a config change, not a code change.

## LLM gateway pattern

ADK reaches any OpenAI-compatible endpoint through `LiteLlm`. Two constraints
shaped `framework/llm.py`:

1. `LiteLlm` is a Pydantic model whose only declared fields are `model` and
   `llm_client`. Passing gateway kwargs (`api_base`, `extra_headers`, `user`)
   straight to its constructor trips the type checker. **Fix:** put all gateway
   wiring in a custom `LiteLLMClient` subclass (`_GatewayClient`) that overrides
   `completion`/`acompletion` and merges the gateway args there.
2. Telemetry needs the ADK-level `LlmResponse` (token counts, finish reason), so
   a thin `LiteLlm` subclass (`_InstrumentedLlm`) wraps `generate_content_async`.
   It reads the identifier off `self.llm_client._identifier` rather than adding a
   Pydantic field.

The token is minted lazily and refreshed per call, so nothing hits the network at
import time and a long-running service never sends a stale one.

## Contract layering

One agent brain, two front doors, both built in `framework/server.py`:

- `GET /.well-known/agent.json` -- the public agent card (discovery, unauthed).
- `POST /a2a` -- the native Agent2Agent protocol, verbatim. **The primary
  contract.**
- `POST /decide` -- the same structured decision over plain JSON, for callers
  that do not speak A2A. Text in, decision out.

Both share ONE code path (`runner.resolve()`), so the contract, persistence and
telemetry outcome behave identically whichever way a caller arrives. The
endpoints differ only in how they serialize.

`serve()` builds one `_decision` sibling off the agent's brain, whose system
prompt demands a structured JSON reply, with the agent's tools and
`after_tool_callback=strip_attachments`.

### Why the SDK owns the A2A wire format

`/a2a` is served by the a2a-sdk's own `DefaultRequestHandler`; we supply only an
`AgentExecutor`. Conformance then follows the SDK, not our code. We mount only
`jsonrpc_routes` -- not `agent_card_routes`, since we already serve the card.

Gotchas verified against a2a-sdk 1.1.2:

- The v1.0 handler REQUIRES an `A2A-Version: 1.0` header. Missing or empty
  defaults to `0.3` and fails with `VersionNotSupportedError` (-32009).
- `TaskState` is a protobuf enum: the members are `TASK_STATE_WORKING`,
  `TASK_STATE_COMPLETED` and so on. There is no `TaskState.working`.
- `Message.metadata` is a `google.protobuf.Struct`, which accepts only JSON
  scalars, lists and dicts. `bytes` and `datetime` raise `ValueError`. Tool
  results are arbitrary agent output, so everything is round-tripped through
  JSON with a string fallback before it goes near a Struct.
- `DefaultRequestHandler` already tracks running producer tasks and cancels them
  in `on_cancel_task`; the executor does not need its own registry. But
  `on_cancel_task` awaits `executor.cancel()` FIRST and then rejects the
  cancellation unless the aggregated result is `TASK_STATE_CANCELED` -- so
  `cancel()` must emit that terminal state itself.

## The decision contract

`framework/decision.py` defines the one structured object an agent returns:
`Decision`, with a terminal state plus the fields that state implies. The four
states map 1:1 onto the A2A task lifecycle, so the contract is expressed **in**
the protocol rather than alongside it:

    COMPLETED      -> TASK_STATE_COMPLETED
    INPUT_REQUIRED -> TASK_STATE_INPUT_REQUIRED
    REJECTED       -> TASK_STATE_REJECTED
    FAILED         -> TASK_STATE_FAILED

The decision itself rides on the terminal status message's metadata under the
`afw.` prefix, so an A2A client reads it without a side channel, while a plain
client that ignores metadata still sees a text artifact and a terminal state.

**No `output_schema`.** Strict `json_schema` mode on most gateways rejects a
schema containing an open object (`details` here) because it lacks
`additionalProperties: false`. Free-form dicts are fundamentally incompatible
with OpenAI-style strict structured outputs, and strict mode also degrades tool
use. Instead the prompt spells out the JSON fields and `parse_decision` recovers
the object from the reply via `extract_json_from_text` -- a multi-strategy parse
(whole string, ```json fence, first `{` to last `}`) that tolerates models which
wrap JSON in prose.

Incoming request metadata is deliberately **free-form**: callers send whatever
their domain uses and `build_decision_prompt` renders every non-empty key as a
`Key: value` line. The framework carries no domain vocabulary of its own.

## Streaming and progress

`runner.stream()` runs ADK in SSE mode and yields `(kind, payload)` tuples:
`thought`, `tool_call`, `tool_result`, `token`, then a final `final_text`.

- Partial text chunks are answer tokens; the aggregated non-partial text is the
  full reply and is kept, not re-emitted, to avoid duplication.
- Partial `function_call` chunks are skipped -- only the aggregated call carries
  complete args.
- `run()` stays in non-streaming mode and skips `part.thought`, so a structured
  JSON final answer is never clobbered by reasoning text.

Over A2A each progress tuple becomes a WORKING status update. **Answer tokens are
coalesced on a ~50ms timer**: one status update per token is correct but costs a
queue round-trip and a lock acquisition each, even for a non-streaming
`message/send` whose caller will only ever see the final task.

Over SSE (`/decide`) a `keep_alive` frame every ~15s of agent silence
holds the connection open; the heartbeat is driven by `asyncio.wait_for` on a
queue fed by a producer task.

## File attachments

Tools may return `attachments: [{fileName, content(b64), contentType}]`.
`strip_attachments` is an ADK `after_tool_callback` -- returning a dict REPLACES
the tool response before it reaches the model. It stashes the files in session
state and returns a cleaned response carrying only
`{fileName, mediaType, status}`, so **base64 blobs never enter the model's
context** (or its token bill). After the run they are drained from session state
and attached to the response as artifacts.

`mediaType` is derived from the filename extension, not the declared
`contentType`, which callers get wrong more often than not.

## Persistence

DB-agnostic by construction, and entirely optional.

- `PortableJSON` is a `TypeDecorator`: JSONB on PostgreSQL, JSON-as-text
  elsewhere (Oracle, SQLite).
- All `String` columns carry explicit lengths (Oracle VARCHAR2 requires it).
- Upserts use `Session.merge` -- portable, no `ON CONFLICT` / `MERGE`.
- Empty `DB_URL` disables everything: `session_scope()` yields None and every
  persistence and telemetry call becomes a no-op, so the app still runs.
- Tables are independent, with no cross-FKs, so each evolves separately.
- DB drivers are NOT main dependencies. No driver means no persistence or
  SQLite (stdlib); PostgreSQL and Oracle are opt-in extras. This keeps a
  slim/distroless image building cleanly.

**Schema control is inverted: production never issues DDL.** `DB_AUTO_CREATE`
defaults to false, so `init_db()` logs "schema externally managed" and skips
`create_all`. The schema ships as hand-off DDL in `db/`, generated from the
models by `scripts/update_ddl.py` (dev-only, no DB connection needed). Run it
with the project root on `PYTHONPATH`.

`TABLE_PREFIX` prepends `<prefix>_` to every table and index name, so several
agents can share one schema.

## Telemetry

Two spans, insert-on-start and update-on-end, both best-effort (never raise):

- `ApiRequestTelemetry` -- one row per inbound request.
- `AiModelCallTelemetry` -- one row per model call, linked to the request by a
  `ContextVar`. This is where LLM cost insight comes from.

Downstream API and MCP calls are logged, not billed, so they are not recorded as
telemetry rows.

**Flow tracing across agents.** `parent_request_id` is a real indexed column;
`flow_id` and `hop` live in `app_meta`. The root request seeds `flow_id` with its
own id and every hop propagates it via `propagation_metadata()`, which the
receiving agent reads back off the incoming A2A message metadata. `trace_id` is
RESERVED for a future OTel trace id -- do not populate it. Cross-agent
reconstruction assumes a shared telemetry database; see
`docs/telemetry-queries.md`.

An ADK-runner bridge (`before_agent_telemetry` / `after_agent_telemetry`) opens a
synthetic span only when no request span is active, so `adk web` / `adk run` get
telemetry too without double-counting the HTTP span. Caveat: on an agent error
the after-callback does not fire, leaving that span inserted but unfinalized.

Cancellation closes the span explicitly. Without that, a cancelled A2A task
leaves a telemetry row open forever.

## Security

`authenticate(headers) -> Identity | None`, checked in order: `X-API-KEY`
(compared with `hmac.compare_digest`, constant-time), then a machine client id,
then a user header. Header lookup normalizes case and dash/underscore variants.

`None` means anonymous and `authenticated=False` means bad credential -- both
401, but telemetry is recorded either way, so a rejected request still produces a
row with its outcome and error set.

The SDK owns the `/a2a` routes, so those are gated by a path-scoped middleware
for parity with the other endpoints, and only when API keys are configured
(matching what the card advertises).

**Client-facing errors are generic.** Every failure path returns "internal error
processing the request"; the real exception goes to the log and the telemetry
row. Callers never see internal detail.

Other hardening worth preserving:

- A body-size middleware rejects an over-large request by `Content-Length`
  before it is buffered.
- The generic HTTP tool is fail-closed against a URL allowlist: empty allowlist
  means it can reach nothing.
- Outbound calls retry with `tenacity` on transient failures only (network
  errors and 429/500/502/503/504), so a 401 returns normally and the caller's
  token-refresh logic stays intact.

## Delegation

Single-hop and deterministic. Each declared subagent compiles into one
auto-generated `handoff(to, task)` tool that only RECORDS the routing decision in
session state; the runner performs the A2A call after the model turn, so it can
forward the original message alongside the model's scoped objective, and returns
the downstream reply verbatim.

The topology is fixed -- an agent may only hand off to a declared subagent. Only
the routing choice is the model's. A hop-depth guard stops runaway chains, and
any failure returns "unavailable" rather than raising.

## Deployment

Generic and portable by intent: k8s primary, with docker/podman-compose and VM
as fallbacks.

- `/health` is liveness with no dependency checks -- a DB blip must not restart
  the pod. `/ready` is readiness and pings the DB when configured (503 while it
  is down, so traffic stops routing until it recovers).
- The production Dockerfile is a distroless multi-stage build: uv installs a
  managed Python into a known prefix, then the runtime stage copies just the
  interpreter and the app. Numeric `USER`, arbitrary-UID-friendly permissions
  (`g=rX`), OCI labels, and a deep healthcheck against `/ready`.
- Base images are `ARG`s so they can be swapped for internally mirrored refs
  without editing the Dockerfile.
- `.dockerignore` is deny-all then allow-back.
- `Dockerfile.dev` is a simple single-stage image with a shell, for compose and
  local work.

**LiteLLM build note:** recent litellm ships compiled wheels for Linux/glibc
only. Docker builds on a glibc base use the wheel and need no toolchain;
Windows/macOS or musl builds fall back to the sdist and need a Rust toolchain.
