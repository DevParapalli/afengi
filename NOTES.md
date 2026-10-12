# Working notes

Outstanding work, open decisions, and things deliberately left alone. Updated
2026-10-10. Architecture rationale lives in `docs/conventions.md`; change history
lives in git. This file is only what is *not done*.

## Open decisions

- **Auth strictness.** `API_KEYS` now defaults empty and `serve()` logs a loud
  `WARNING` when neither it nor `OIDC_ISSUER` is set, but it still starts and
  serves unauthenticated. The stricter reading of fail-closed is to refuse to
  start unless auth is configured or an explicit `ALLOW_ANONYMOUS=true` is set.
  Not implemented -- the warning is a log line, not a guard.
- **Model names on a plain OpenAI-compatible server.** `LLM_URL_TEMPLATE={url}`
  points the agent at the ahitung worker, but litellm still picks its provider
  from the model name: `gpt-*` routes as OpenAI, while an arbitrary worker model
  name fails or routes to another provider's wire format. Workaround:
  `MODEL=openai/<name>` (litellm strips the prefix; telemetry records it).
  Forcing `custom_llm_provider="openai"` in `_GatewayClient` would fix it for
  every agent, but changes routing for non-`gpt` models on existing gateways.
  Undecided until the worker's model naming is known.
- **Non-UUID session ids reach ahitung.** The ADK session id is the caller's
  `contextId` (`/decide`) or A2A context id, sent verbatim under
  `MCP_SESSION_HEADER`. workspaced refuses anything that is not a UUID, so a
  caller sending `contextId: "ctx-1"` gets every workspace tool refused. Generated
  ids (uuid7 / ADK uuid4) are fine. Options: validate at the endpoint, or map
  non-UUIDs with uuid5. Not done.
- **`warnings` in the protocol.** `Resolution.warnings` reaches callers via the
  `/decide` envelope and `afw.warnings` A2A metadata. Producers today are the
  degrade points (dropped attachment, unreadable A2A metadata, malformed
  `parentRequestId` / `hop`, non-object `/decide` metadata). Intended to be
  wired further into the protocol / telemetry; shape not settled.
- **Scratchpad tool.** A place for the agent to read and write working notes
  across a task. Explicitly optional: the agent is encouraged to use it, never
  required to. Not designed yet.

## Deferred

- **Tests, to post-v1.** `pytest`, `pytest-asyncio` and `pytest-cov` are in the
  `dev` dependency group and `tests/**` per-file-ignores are already in the ruff
  config, but there is no `tests/` directory. The interim coverage is
  `scripts/.verification/*.py` -- three scripts with real assertions and
  non-zero exit, run manually:

  ```
  PYTHONPATH=. uv run scripts/.verification/decision_contract.py
  PYTHONPATH=. uv run scripts/.verification/a2a_executor.py
  PYTHONPATH=. uv run scripts/.verification/decide_endpoint.py
  ```

  They cover the decision contract, the full A2A task lifecycle (all four
  terminal states, token coalescing, protobuf Struct sanitizing, cancellation
  closing the telemetry span), and `/decide` JSON + SSE + auth + the card.

## Known limitations, reported not fixed

- **`scripts/build_zips.py:291`** calls `relative_to(ROOT)` on the `--out` path,
  so any out-dir outside the repo raises `ValueError`. Only affects the
  non-default `--out`.
- **`scripts/build_zips.py:78`: the leak guard is inert.** `_DEV_DENYLIST` is
  empty, which is correct while `agent/` holds only the neutral example. If a
  private agent is ever developed there, fill it with that domain's vocabulary
  (tool names, internal system names, team codes) or the fail-closed check
  passes everything. The non-ASCII check still runs regardless.
- **Template blank lines.** When the optional `{{MCP_FIELDS}}` /
  `{{MCP_PROPERTIES}}` tokens render empty, generated `config.py` gets an extra
  blank line or two. Cosmetic; `ruff format` in the generated project fixes it.
  Not worth making the tokens carry their own newlines.
- **`scripts/.verification/decide_endpoint.py:62`** asserts two retired route
  names stay unmounted. Useful as a guard, but it keeps those names alive in the
  tree; drop the assertion if they should not appear anywhere.

## Not verified

- **Whether cancelling the asyncio task aborts the in-flight LiteLLM request**,
  or merely detaches from it. The A2A side is correct and tested -- the SDK
  cancels the producer task, `CancelledError` propagates, and the telemetry span
  is closed -- but whether cancellation reaches the provider through ADK's and
  litellm's generators needs a real gateway and a long generation.
- Anything against a live LLM gateway. All verification to date stubs
  `runner.stream`. MCP is covered against a local mcp 2.x server
  (`scripts/.verification/mcp_toolset.py`). Against ahitung, only workspaced was
  exercised, by hand on 2026-10-12: tool listing, `write_file` / `read_file` /
  `edit_file` round-trips, and the refusal without `x-ahit-session-id`. That
  run was on Windows with `fcntl` / `termios` stubbed, so the exec and terminal
  tools were not run. The worker (LLM proxy + MCP) does not exist yet and is
  untested.
- **`decide_endpoint.py` needs `API_KEYS` set** (e.g. `API_KEYS=devkey`), or it
  fails with an `IndexError` on line 56 before any check runs.
- **The ADK modern-protocol flag is experimental and private.** If a future ADK
  renames or removes `MCP_MODERN_PROTOCOL`, `mcp_toolset()` raises `ValueError` at
  build time rather than degrading. Re-check on each ADK bump; drop the flag once
  ADK turns it on by default.
- **Database drivers after the 2026-10-10 `uv lock --upgrade`**: sqlalchemy
  2.0 -> 2.1, oracledb 4 -> 26, psycopg 3.3.6. None of the verification scripts
  touch a database.

## Local artefacts, not in git

Present on disk, gitignored, safe from commit -- but worth deleting once no
longer needed for reference:

- `afw_frames.txt`, `nists_frames.txt` -- captured real supervisor traffic
- `.env`, `.netrc` -- live credentials
- `bruno/` -- request collections still naming the old contract
- `__/` -- superseded SPEC and conventions drafts
