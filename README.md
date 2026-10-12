# Agent Framework — Quick Start

A small, readable framework for building and serving Google ADK agents backed by
an OpenAI-compatible LLM gateway, MCP tools, and OAuth2-protected APIs.
Ships one example agent and exposes four HTTP endpoints per agent: an agent card,
the native A2A protocol at `/a2a`, the same structured decision over plain JSON at
`/decide`.

---

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — the only tool you need; it manages Python and dependencies.
- Python **3.14+** (uv installs it automatically if missing).
- **Windows / macOS only:** a **Rust toolchain** ([rustup](https://rustup.rs)) — LiteLLM
  ships prebuilt wheels for Linux only, so `uv sync` builds it from source on those
  platforms. Linux and the Docker image use the wheel and need nothing extra.
- LLM gateway credentials (client id / secret / app key). Downstream API and MCP
  credentials are only needed for the agent's tools to work.

---

## 1. Install

```powershell
uv sync
```

To also install a database driver (only if you want persistence + telemetry — see step 5):

```powershell
uv sync --extra postgres   # PostgreSQL (psycopg)
uv sync --extra oracle     # Oracle (oracledb)
```

---

## 2. Configure

Create a `.env` file in the project root. Settings are read from the environment
or this file. Non-secret **framework parameters** live in
[framework/config.py](framework/config.py); **all credentials** live per-agent in
[agent/config.py](agent/config.py).

**Minimum to get a response from the model:**

```dotenv
LLM_CLIENT_ID=your-client-id
LLM_CLIENT_SECRET=your-client-secret
LLM_IDENTIFIER=your-caller-id
```

**Full reference (all optional unless noted):**

| Variable | Purpose | Default |
| --- | --- | --- |
| `APP_NAME` | This app's name (telemetry + agent card) | `agent-framework` |
| `ENVIRONMENT` | Environment tag in telemetry | `prod` |
| `HOST` / `PORT` | HTTP server bind address | `0.0.0.0` / `8080` |
| `DB_URL` | SQLAlchemy URL; **empty = persistence & telemetry disabled** | *(empty)* |
| `DB_ECHO` | Log SQL statements | `false` |
| `API_KEYS` | Comma-separated valid `X-API-KEY` values for the HTTP endpoints | `devkey` |
| `TOOL_URL_ALLOWLIST` | Comma-separated hosts/URL prefixes the built-in `http_request` tool may reach (empty = none) | *(empty)* |
| `LLM_CLIENT_ID` / `LLM_CLIENT_SECRET` / `LLM_IDENTIFIER` | LLM gateway auth **(required)** | — |
| `LLM_BASE_URL` / `LLM_TOKEN_URL` | LLM gateway endpoints | — |
| `LLM_URL_TEMPLATE` | Call URL shape with `{url}` / `{model}`; `{url}` for a plain OpenAI-compatible server (the ahitung worker) | `{url}/openai/deployments/{model}` |
| `MODEL` | Model for the example agent | `gpt-5-nano` |
| `API_BASE_URL` / `API_TOKEN_URL` / `API_TIMEOUT` | Downstream API tier + token endpoint / timeout (s) | — / `60` |
| `API_CLIENT_ID` / `API_CLIENT_SECRET` | Client-credentials for the downstream API | — |
| `MCP_URL` / `MCP_TOKEN_URL` | MCP server + client-credentials token endpoint | — |
| `MCP_CLIENT_ID` / `MCP_CLIENT_SECRET` | MCP client-credentials (bearer token); omit to skip the token | — |
| `MCP_USER_ID` / `MCP_API_KEY` | MCP routing headers; omit to skip the headers | — |
| `MCP_SESSION_HEADER` | Header that carries the ADK session id on every MCP call; ahitung's workspaced requires `x-ahit-session-id` | *(not sent)* |

---

## 3. Run it

Pick whichever fits what you're doing:

**a) HTTP server** — the deployable mode (agent card + `/a2a` + `/decide` +
`/decide`), with auth, persistence and telemetry:

```powershell
uv run main.py
```

This binds to `HOST`/`PORT` (default `0.0.0.0:8080`). Equivalent explicit form:
`uv run uvicorn main:app --host 0.0.0.0 --port 8080`.

**b) One-shot / interactive CLI** — quickest way to confirm the model works:

```powershell
uv run adk run .
```

**c) ADK visual dev UI** — chat with the agent in a browser, inspect tool calls:

```powershell
uv run adk web .
```

---

## 4. Call the HTTP endpoints

All `/decide` and `/a2a` requests are authenticated. The easiest identity for
local testing is a user header; alternatively set `API_KEYS=devkey` in `.env`
and send `X-API-KEY: devkey`. These examples use PowerShell's `Invoke-RestMethod`
so there's no quote-escaping to fight.

**Agent card (public, no auth):**

```powershell
Invoke-RestMethod http://localhost:8080/.well-known/agent.json
```

**`/decide` — the structured decision contract** (needs `API_KEYS=devkey` in `.env`):

```powershell
$headers = @{ "X-API-KEY" = "devkey" }
$body = @{
  text = "Where is my order ORD-118422?"
  contextId = "ctx-1"
  metadata = @{ category = "Orders"; coreIssue = "Status"; reference = "REQ-4471" }
} | ConvertTo-Json -Depth 6
Invoke-RestMethod -Uri http://localhost:8080/decide -Method Post -Headers $headers -ContentType "application/json" -Body $body
```

The response carries the agent's `Decision`: a `state` of `COMPLETED`,
`INPUT_REQUIRED`, `REJECTED` or `FAILED`, plus the fields that state implies.
Add `stream = $true` to the body to receive SSE progress frames instead.

Re-send with the same `contextId` and the returned `taskId` to resume a task that
came back `INPUT_REQUIRED` (its prior decision is replayed into the prompt when a
database is configured).

**`/a2a` — the native Agent2Agent protocol**, served by the a2a-sdk reference
handler. Same decision, same terminal states, expressed as an A2A task: the
`Decision` selects the task's terminal state and rides on its final status
metadata. Any A2A client can drive it; note that the v1.0 handler requires an
`A2A-Version: 1.0` header on every call.

> An anonymous request (no `X-API-KEY`/`CLIENT_ID`/`AUTH_USER`) returns **401**.

---

## 5. Enable persistence & telemetry (optional)

Persistence and telemetry are **off** until you set `DB_URL`. When set, the HTTP
server creates the tables automatically on startup and records:

- decision state — so an `INPUT_REQUIRED` follow-up resumes,
- telemetry — one row per request and one per model call.

The schema is database-agnostic (native `JSONB` on PostgreSQL, JSON-as-text on Oracle):

```dotenv
# PostgreSQL
DB_URL=postgresql+psycopg://user:password@localhost:5432/agentdb

# …or Oracle
DB_URL=oracle+oracledb://user:password@localhost:1521/?service_name=FREEPDB1
```

Then install the matching driver (`uv sync --extra postgres` / `--extra oracle`)
and start the server (step 3c). Persistence/telemetry are best-effort — a database
outage logs a warning and never breaks a request.

---

## 6. Customize the agent

One agent is deployed per instance; it lives in `agent/` as four small files —
you never touch `framework/`:

- `config.py` — API base URL, MCP sub-component, model,
- `prompts.py` — the system prompt (domain knowledge only),
- `tools.py` — API tools (plain functions),
- `agent.py` — wires them into a `root_agent` via `build_agent(...)`.

`agent/__init__.py` re-exports `root_agent`, which `main.py` serves. To build a
different agent, edit these files (or swap the contents of the `agent/` package).

The framework also ships two optional, ready-made tools you can add to any agent's
tools list (they are opt-in, not wired automatically):

- `http_request(url, method, json_body, params)` — call an allowlisted HTTP(S)
  service; refuses any URL not covered by `TOOL_URL_ALLOWLIST` (fail-closed).
- `current_datetime()` — the current UTC date/time, so the model never guesses it.

```python
from framework import build_agent, http_request, current_datetime

root_agent = build_agent(..., tools=[http_request, current_datetime])
```

Need authenticated calls? Build a per-agent variant that attaches the matching
bearer automatically — connections are matched by their `url` (most specific match
wins), matched on host boundaries (never a raw string prefix), and only sent over
HTTPS:

```python
from framework import build_agent, make_http_request_tool, Connection

api = Connection(url="https://api.example.com", client_id=..., client_secret=..., token_url=...)
http = make_http_request_tool([api])
root_agent = build_agent(..., tools=[http])  # still gated by TOOL_URL_ALLOWLIST
```

Each matched connection attaches whatever it carries, degrading independently: an
OAuth2 bearer (`client_id`), an API key, and `x-user-id`. The api key is sent
under `x-api-key` by default — set `api_key_header` for APIs that use another name:

```python
Connection(api_key="...", api_key_header="Ocp-Apim-Subscription-Key")
```

---

## Troubleshooting

- **401 on `/decide` or `/a2a`** — send an identity header (`AUTH_USER: you@example.com`)
  or set `API_KEYS` and pass `X-API-KEY`.
- **No telemetry / memory rows** — confirm `DB_URL` is set and you're hitting the
  **HTTP server** (the CLI and dev UI don't create the request span).
- **Model/tool auth errors** — re-check `LLM_*` (model) and `API_*` / `MCP_*` (tools) credentials.
- **`uv sync` can't resolve a DB driver** — you only need the extra for the database you use.
