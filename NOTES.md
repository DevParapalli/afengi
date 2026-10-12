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
- **Gateway URL shape.** `framework/llm.py:39` hardcodes
  `{base_url}/openai/deployments/{model}` (the Azure OpenAI shape). Pointing the
  agent at the ahitung worker needs `llm_base_url=http://worker:8081/v1` with no
  suffix, so this needs to become configurable -- a `url_template` field on
  `Connection` rather than a new parameter. Not done.
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
  (`scripts/.verification/mcp_toolset.py`) but **not against ahitung itself**.
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
