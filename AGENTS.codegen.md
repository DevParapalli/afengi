# AGENTS.md

Guidance for AI coding agents working in this repository. (Humans: see README.md.)

This project is one deployable AI agent built on the **agent-framework**. It has two
parts with very different rules:

- `framework/` -- the shared runtime (LLM gateway, HTTP server, auth, persistence,
  telemetry, and the A2A / decision / care response contracts). It is GENERATED and is
  identical across every agent. **Do not edit it.** Changes here get overwritten on the
  next generation and break the contract the platform depends on.
- `agent/` -- this specific agent. **This is the only code you change.**

## Golden rules

- Edit only `agent/**` and `.env`. Do not modify `framework/**`, `main.py`, `db/**`,
  `Dockerfile*`, or `compose.yaml` unless explicitly asked.
- Never hardcode secrets or endpoints. Everything configurable comes from environment
  variables (`.env`); see `.env.example`. Never commit `.env`.
- Keep it ASCII. Write terse, technical comments that explain decisions, not the obvious.
- Add a dependency only with `uv add <pkg>` (updates `pyproject.toml` + the lockfile).
  Do not hand-edit the dependency lists.
- After any change, verify it boots: `uv run main.py` (or `uv run adk run .`).

## The agent/ package (what each file is for)

- `agent/prompts.py` -- `SYSTEM_PROMPT`: this agent's domain knowledge (its identity, what
  its tools do, the identifier formats it must extract). Editing this changes behaviour with
  no framework change. Do NOT use single `{ }` braces here -- ADK treats them as template
  variables; write examples without braces.
- `agent/tools.py` -- one plain function per capability. The **docstring IS the tool
  description** the model reads, so keep it accurate: say when to use the tool and which
  fields are mandatory. Call an API through the `_call(endpoint, body, connection="api")`
  helper.
- `agent/agent.py` -- wires the prompt + tools into `root_agent` via `build_agent(...)`, and
  defines `root_card` (agent-card tags / examples). Add a tool by listing its function in
  `tools=[...]`.
- `agent/config.py` -- per-agent settings + credentials (model, LLM / API / MCP), read from
  env. Add a new API connection here as `<name>_base_url` / `<name>_connection` / `_timeout`.

## Common tasks

- **Change what the agent knows or does** -> edit `SYSTEM_PROMPT` in `agent/prompts.py`.
- **Add a tool** -> write a function in `agent/tools.py` (typed arguments + a clear
  docstring), then add it to `tools=[...]` in `agent/agent.py`.
- **Call a second API** -> add `<name>_base_url` / `<name>_token_url` / `<name>_client_id` /
  `<name>_client_secret` / `<name>_timeout` and a `<name>_connection` property in
  `agent/config.py`, then call `_call(..., connection="<name>")`.
- **Hand off to a downstream agent** -> declare `remote_subagent(name, description,
  connection)` (connection.url = the downstream's agent-card url) and pass
  `subagents=[...]` to `build_agent` in `agent/agent.py`. The model routes via the auto
  `handoff` tool: one hop, bounded to declared subagents, downstream reply returned
  verbatim, unreachable -> manual review.
- **Change model / gateway / credentials** -> `.env` (see `.env.example`).

## Run

- `uv sync` -- install dependencies. Add `--extra postgres` or `--extra oracle` only if you
  point `DB_URL` at Postgres or Oracle. On Windows/macOS this builds LiteLLM from source
  (its wheels are Linux-only), so a Rust toolchain (rustup) must be installed there;
  Linux and Docker use the wheel and need nothing.
- `uv run main.py` -- serve the HTTP endpoints (default `0.0.0.0:8080`).
- `uv run adk run .` -- one-shot CLI. `uv run adk web .` -- visual dev UI.

## Endpoints (owned by the framework -- do not reimplement)

- `GET  /.well-known/agent.json` -- public agent card.
- `POST /decide` -- the structured decision contract over plain JSON.
- `POST /a2a`    -- native Agent2Agent protocol.
- `GET  /health` -- liveness. `GET /ready` -- readiness (checks the DB when configured).

## Data and config

- Persistence is optional and chosen by `DB_URL`: empty = off; `sqlite:///./agent.db`
  (stdlib, also set `DB_AUTO_CREATE=true`); or Postgres / Oracle (install the extra).
- Auth, request size limits, retries, and token / JWT verification are framework concerns
  configured via env -- do not reimplement them in `agent/`.

## Deploy

- Container: `Dockerfile` (hardened, distroless) or `Dockerfile.dev` (local). `compose.yaml`
  for docker / podman-compose. All configuration is environment variables.

Before you report done: `uv run main.py` starts without error, and any new tool appears in
the agent's tool list.
