# Agent — Quick Start

A ready-to-run [Google ADK](https://google.github.io/adk-docs/) agent on a small,
readable framework. It calls an OpenAI-compatible LLM gateway, optionally OAuth2
APIs and MCP tools, and serves four HTTP endpoints — a public agent card, the
the structured `/decide` contract, and native
Agent2Agent (`/a2a`) — plus `/health` and `/ready` probes.

You extend the agent by editing the `agent/` package only; `framework/` is
identical across every agent and you never touch it.

---

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — the only tool you need; it manages Python and dependencies.
- Python **3.14+** (uv installs it automatically if missing).
- **Windows / macOS only:** a **Rust toolchain** ([rustup](https://rustup.rs)) — LiteLLM
  ships prebuilt wheels for Linux only, so `uv sync` builds it from source on those
  platforms. Linux and the Docker image use the wheel and need nothing extra.
- LLM gateway credentials (client id / secret / app key). Downstream API and MCP
  credentials are only needed if your agent's tools use them.

---

## 1. Install

```powershell
uv sync
```

Persistence is optional (step 5). SQLite needs no driver; for Postgres or Oracle
install its extra:

```powershell
uv sync --extra postgres   # PostgreSQL (psycopg)
uv sync --extra oracle     # Oracle (oracledb)
```

---

## 2. Configure

Copy `.env.example` to `.env` and fill it in. Settings are read from the
environment or this file.

- Non-secret **framework parameters** (bind address, DB URL, API keys, allowlist)
  live in [framework/config.py](framework/config.py).
- **All credentials and endpoints** live per-agent in
  [agent/config.py](agent/config.py) (LLM gateway, downstream APIs, MCP).

**Minimum to get a response from the model:**

```dotenv
LLM_CLIENT_ID=your-client-id
LLM_CLIENT_SECRET=your-client-secret
LLM_IDENTIFIER=your-caller-id
```

**Framework parameters (all optional unless noted):**

| Variable | Purpose | Default |
| --- | --- | --- |
| `APP_NAME` | This app's name (telemetry + agent card) | *(your agent's name)* |
| `ENVIRONMENT` | Environment tag in telemetry | `prod` |
| `HOST` / `PORT` | HTTP server bind address | `0.0.0.0` / `8080` |
| `DB_URL` | SQLAlchemy URL; **empty = persistence & telemetry disabled** | *(empty)* |
| `DB_ECHO` | Log SQL statements | `false` |
| `DB_AUTO_CREATE` | Create tables on startup (dev / SQLite only; prod provisions externally) | `false` |
| `TABLE_PREFIX` | Prepends `<prefix>_` to every table + index; empty = base names | *(empty)* |
| `API_KEYS` | Comma-separated valid `X-API-KEY` values for the HTTP endpoints | *(empty)* |
| `MAX_REQUEST_BYTES` | Max request body size in bytes; larger gets `413` | `10485760` |
| `TOOL_URL_ALLOWLIST` | Hosts/URL prefixes the built-in `http_request` tool may reach (empty = none) | *(empty)* |
| `OIDC_ISSUER` / `OIDC_AUDIENCE` | Verify `Authorization: Bearer` JWTs against this IdP; **empty issuer = off** (falls back to header identity) | *(empty)* |

**Agent parameters (in [agent/config.py](agent/config.py), env-switchable):**

| Variable | Purpose |
| --- | --- |
| `LLM_CLIENT_ID` / `LLM_CLIENT_SECRET` / `LLM_IDENTIFIER` | LLM gateway auth **(required)** |
| `LLM_BASE_URL` / `LLM_TOKEN_URL` | LLM gateway endpoints |
| `MODEL` | Model name for this agent |
| `<CONN>_BASE_URL` / `<CONN>_TOKEN_URL` / `<CONN>_TIMEOUT` | A downstream API connection's base + token endpoint / timeout (s) |
| `<CONN>_CLIENT_ID` / `<CONN>_CLIENT_SECRET` | Client-credentials for that connection |
| `MCP_URL` / `MCP_TOKEN_URL` / `MCP_CLIENT_ID` / `MCP_CLIENT_SECRET` | MCP server + client-credentials (bearer) |
| `MCP_USER_ID` / `MCP_API_KEY` | MCP routing headers |

`<CONN>` is the uppercased name of each connection you define in `agent/config.py`
(the default one is `API`). Add more connections to reach more APIs — see step 7.

---

## 3. Run it

**a) HTTP server** — the deployable mode (agent card + all endpoints), with auth,
persistence and telemetry:

```powershell
uv run main.py
```

Binds to `HOST`/`PORT` (default `0.0.0.0:8080`). Equivalent explicit form:
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

`/decide` and `/a2a` are authenticated. For local testing send a user
header (`AUTH_USER`), or set `API_KEYS=devkey` in `.env` and send `X-API-KEY: devkey`.
The examples use PowerShell's `Invoke-RestMethod`.

**Agent card (public, no auth):**

```powershell
Invoke-RestMethod http://localhost:8080/.well-known/agent.json
```

**`/decide` — the structured decision contract** (needs `API_KEYS=devkey` in `.env`):

```powershell
$headers = @{ "X-API-KEY" = "devkey" }
$body = @{
  text = "Describe the request to resolve here"
  contextId = "ctx-1"
  metadata = @{ reference = "REQ-123"; category = "..."; coreIssue = "..." }
} | ConvertTo-Json -Depth 6
Invoke-RestMethod -Uri http://localhost:8080/decide -Method Post -Headers $headers -ContentType "application/json" -Body $body
```

The response carries the agent's `Decision`: a `state` of `COMPLETED`,
`INPUT_REQUIRED`, `REJECTED` or `FAILED`, plus the fields that state implies. Add
`stream = $true` to the body for SSE progress frames. Re-send with the same
`contextId` and the returned `taskId` to resume an `INPUT_REQUIRED` task.

Over `/a2a` the same decision drives the A2A task's terminal state
(`TASK_STATE_COMPLETED`, `_INPUT_REQUIRED`, `_REJECTED`, `_FAILED`) and rides on
its final status metadata.

**`/a2a` — the native Agent2Agent v1.0 protocol** (served by the reference A2A SDK,
so any A2A client can drive the agent). The v1.0 JSON-RPC method names are
`SendMessage` / `SendStreamingMessage` / `GetTask` / `CancelTask`, and every call
**must** send the `A2A-Version: 1.0` header:

```powershell
$headers = @{ "X-API-KEY" = "devkey"; "A2A-Version" = "1.0" }
$body = @{
  jsonrpc = "2.0"; id = "1"; method = "SendMessage"
  params = @{ message = @{
    messageId = "m1"; role = "ROLE_USER"
    parts = @(@{ text = "hello" })
  } }
} | ConvertTo-Json -Depth 6
Invoke-RestMethod -Uri http://localhost:8080/a2a -Method Post -Headers $headers -ContentType "application/json" -Body $body
```

You get back a standard A2A `Task` with a `response` artifact.
`SendStreamingMessage` streams the task lifecycle (task -> working -> artifact ->
completed) as SSE.

**`/health` and `/ready` (public, no auth)** — liveness and readiness probes.
`/ready` returns `503` when a configured database is unreachable.

```powershell
Invoke-RestMethod http://localhost:8080/health
Invoke-RestMethod http://localhost:8080/ready
```

> An anonymous request (no `X-API-KEY` / `CLIENT_ID` / `AUTH_USER`) returns **401**.

---

## 5. Enable persistence & telemetry (optional)

Persistence and telemetry are **off** until you set `DB_URL`. When set, the HTTP
server records:

- decision state — so an `INPUT_REQUIRED` follow-up resumes,
- telemetry — one row per request and one per model call.

The schema is database-agnostic (native `JSONB` on PostgreSQL, JSON-as-text on
SQLite/Oracle):

```dotenv
# SQLite (no driver; set DB_AUTO_CREATE=true to create the tables)
DB_URL=sqlite:///./agent.db

# PostgreSQL (uv sync --extra postgres)
DB_URL=postgresql+psycopg://user:password@localhost:5432/agentdb

# Oracle (uv sync --extra oracle)
DB_URL=oracle+oracledb://user:password@localhost:1521/?service_name=FREEPDB1
```

Persistence/telemetry are best-effort — a database outage logs a warning and never
breaks a request.

**Provisioning the schema.** In production the app never issues DDL. Provision the
tables from the shipped reference DDL in `db/` (`postgresql.generated.sql` /
`oracle.generated.sql`). If you rename tables or set `TABLE_PREFIX`, regenerate the
DDL from the ORM models (no database connection needed):

```powershell
uv run scripts/update_ddl.py
```

For local development only, set `DB_AUTO_CREATE=true` to let the server create the
tables on startup.

---

## 6. Build and extend the agent

Everything agent-specific lives in the `agent/` package (you never edit `framework/`):

| File | What it holds |
| --- | --- |
| [agent/prompts.py](agent/prompts.py) | `SYSTEM_PROMPT` — your agent's domain knowledge only (identity, what its tools do, identifier formats). |
| [agent/tools.py](agent/tools.py) | Your tools — one plain function per workflow. The **docstring is the tool's description** the model reads. |
| [agent/config.py](agent/config.py) | Settings + credentials (LLM, API connections, MCP) as env-switchable fields. |
| [agent/agent.py](agent/agent.py) | Wires prompt + tools + model into `root_agent` via `build_agent(...)`, and the agent-card metadata `root_card`. |
| [agent/__init__.py](agent/__init__.py) | Re-exports `root_agent` / `root_card`, which `main.py` and the ADK CLI import. |

### Add a tool

A tool is a plain function whose docstring tells the model when to use it and which
fields are required. ADK builds the tool schema from the signature + docstring.

```python
# agent/tools.py
def lookup_order(order_number: str) -> dict:
    """Look up an order by its number. Use when the case references an order id.

    Args:
        order_number: The order number to look up. Required.
    """
    return _call("orders/lookup", {"orderNumber": order_number})
```

Then list it in `agent/agent.py`:

```python
root_agent = build_agent(
    ...,
    tools=[tools.lookup_order],
)
```

### Add another API connection

Each downstream API is a block of fields plus a grouped `Connection` property. Add
a block to reach a second API with its own auth:

```python
# agent/config.py
class Settings(BaseSettings):
    ...
    billing_base_url: str = "https://billing.example.com/api"
    billing_token_url: str = ""
    billing_client_id: str = ""
    billing_client_secret: str = ""
    billing_timeout: int = 60

    @property
    def billing_connection(self) -> Connection:
        return Connection(
            url=self.billing_base_url,
            token_url=self.billing_token_url,
            client_id=self.billing_client_id,
            client_secret=self.billing_client_secret,
            timeout=self.billing_timeout,
        )
```

A tool then targets it with `connection=`:

```python
def get_invoice(invoice_id: str) -> dict:
    """Fetch an invoice by id."""
    return _call("invoices", {"invoiceId": invoice_id}, connection="billing")
```

### Use an MCP server's tools

Expose a subset of a remote MCP server's tools to the agent:

```python
# agent/agent.py
from framework import build_agent, mcp_toolset

root_agent = build_agent(
    ...,
    tools=[
        mcp_toolset(
            config.settings.mcp_connection
        ),
    ],
)
```

### Built-in optional tools

The framework ships ready-made tools you can add to the tools list (opt-in, not
wired automatically):

- `http_request(url, method, json_body, params)` — call an allowlisted HTTP(S)
  service; refuses any URL not covered by `TOOL_URL_ALLOWLIST` (fail-closed). Sends
  no authentication.
- `current_datetime()` — the current UTC date/time, so the model never guesses it.

```python
from framework import build_agent, http_request, current_datetime

root_agent = build_agent(..., tools=[http_request, current_datetime])
```

For authenticated calls, build a per-agent variant that attaches the matching
credential automatically. Connections are matched by their `url` (most specific
match wins), matched on host boundaries (never a raw string prefix), and only sent
over HTTPS:

```python
from framework import build_agent, make_http_request_tool, Connection

api = Connection(
    url="https://api.example.com", client_id="...", client_secret="...", token_url="..."
)
http = make_http_request_tool([api])
root_agent = build_agent(..., tools=[http])  # still gated by TOOL_URL_ALLOWLIST
```

Each matched connection attaches whatever it carries, degrading independently: an
OAuth2 bearer (`client_id`), an API key, and `x-user-id`. The api key is sent under
`x-api-key` by default — set `api_key_header` for APIs that use another name:

```python
Connection(api_key="...", api_key_header="Ocp-Apim-Subscription-Key")
```

### Hand off to a downstream agent

An agent can hand a whole case to ONE downstream A2A agent instead of resolving it
itself -- useful for a supervisor that routes to specialist agents. Declare each
downstream with `remote_subagent(...)` and pass them as `subagents=[...]`:

```python
# agent/agent.py
from framework import build_agent, remote_subagent, Connection

gl_track = remote_subagent(
    name="gl_track",
    description="GL reconciliation, journal imports, period close.",  # what the model routes on
    connection=Connection(
        url="https://host/gl/.well-known/agent.json",  # the downstream's agent card
        invoke_url="https://host/gl/a2a",  # optional; else derived from the card
        client_id="...",
        client_secret="...",
        token_url="...",  # optional auth
    ),
)

root_agent = build_agent(
    ...,
    subagents=[gl_track],  # compiles into one auto `handoff` tool
)
```

At runtime the model either answers the case itself or calls the auto-generated
`handoff(to, task)` tool with one downstream name and a scoped objective. The
framework then calls that agent over A2A -- forwarding the original case plus the
objective -- and returns its reply verbatim. Rules:

- **One hop:** an agent hands to at most one downstream per case (or none).
- **Bounded:** it can only hand to a *declared* subagent; an unknown name is refused,
  and a runaway chain is stopped by a hop-depth guard.
- **Fail-safe:** if the downstream is unreachable, the case comes back for manual
  review rather than failing.
- **Traceable:** every hop is recorded in request telemetry (who called whom, when,
  and token cost).

---

## 7. Container and deploy

Config is entirely environment variables, so the same image runs anywhere.

- `Dockerfile` — hardened production image (multi-stage, distroless, non-root).
- `Dockerfile.dev` — local image with a shell; what `compose.yaml` builds.
- `compose.yaml` — `docker compose up --build` (or `podman-compose up --build`).

Point orchestrator probes at `/health` (liveness) and `/ready` (readiness). On
Kubernetes with a read-only root filesystem, mount a writable `emptyDir` at `/tmp`.

---

## Project layout

```
main.py            # HTTP entrypoint: app = serve(root_agent, card_info=root_card)
pyproject.toml     # dependencies (uv)
.env.example       # copy to .env and fill in
AGENTS.md          # guide for AI coding agents working in this project
Dockerfile         # hardened production image (Dockerfile.dev = local)
compose.yaml       # docker / podman compose
agent/             # THE ONLY PACKAGE YOU EDIT
  agent.py         #   wires root_agent + root_card
  config.py        #   settings + credentials
  prompts.py       #   SYSTEM_PROMPT (domain knowledge)
  tools.py         #   your tools
framework/         # the framework (do not edit) -- serving, LLM, auth, tools, persistence
db/                # reference DDL for provisioning persistence tables
scripts/           # update_ddl.py and other helpers
```

---

## Troubleshooting

- **401 on `/decide` or `/a2a`** — send an identity header
  (`AUTH_USER: you@example.com`) or set `API_KEYS` and pass `X-API-KEY`.
- **`/a2a` returns a version error** — send the `A2A-Version: 1.0` header on every call.
- **No telemetry / memory rows** — confirm `DB_URL` is set and you're hitting the
  **HTTP server** (the CLI and dev UI don't create the request span).
- **Model / tool auth errors** — re-check `LLM_*` (model) and your `<CONN>_*` /
  `MCP_*` (tools) credentials.
- **Bearer tokens rejected (401) after setting `OIDC_ISSUER`** — the token's `iss`
  and `aud` must match `OIDC_ISSUER` / `OIDC_AUDIENCE`; leave `OIDC_ISSUER` empty to
  fall back to header identity.
- **`uv sync` fails building `litellm` (cargo / Rust not found)** on Windows/macOS —
  install the Rust toolchain ([rustup](https://rustup.rs)) and re-run. Linux and the
  Docker image use the prebuilt wheel and don't need it.
- **`uv sync` can't resolve a DB driver** — you only need the extra for the database you use.
