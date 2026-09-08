"""Shared infrastructure parameters (non-secret).

Only cross-cutting, non-secret knobs: app identity, HTTP bind address, DB URL,
endpoint API keys, generic-tool URL allowlist. Credentials do NOT live here --
each agent owns its LLM gateway / downstream-API / MCP creds in its own
agent/config.py and passes them as framework.credentials.Credentials. Read from
the environment (or a local .env file).
"""

import tomllib
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_version() -> str:
    """App version straight from pyproject.toml [project].version (single source of
    truth; framework runs from source, not installed as a dist)."""
    path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))["project"]["version"]
    except OSError, KeyError, tomllib.TOMLDecodeError:
        return "0.0.0"


_APP_VERSION = _read_version()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # This application's identity (telemetry + agent card + agentMetadata).
    app_name: str = "agent-framework"
    app_version: str = _APP_VERSION  # from installed dist metadata; override via env if needed
    environment: str = "prod"

    # HTTP server bind address (used by main.py / uvicorn).
    host: str = "0.0.0.0"
    port: int = 8080

    # Persistence + telemetry (DB-agnostic: PostgreSQL or Oracle).
    # A single ready-to-use SQLAlchemy URL, passed straight to create_engine
    # (see db.py), so it must be fully assembled and URL-encoded -- framework does
    # no further escaping. Empty = run without a DB; persistence + telemetry no-op.
    #
    # Shape:  dialect+driver://USER:PASSWORD@HOST:PORT/NAME
    #   SQLite:      sqlite:///./agent.db  (stdlib driver; no extra to install)
    #   PostgreSQL:  postgresql+psycopg://user:pw@host:5432/mydb
    #   Oracle:      oracle+oracledb://user:pw@host:1521/?service_name=svc
    #
    # Escape the username and password, not the whole string. If either contains
    # a character that is reserved in a URL (@ : / ? # % ...), percent-encode it
    # or the URL parses wrong. Build it once in Python and paste the result:
    #
    #     from urllib.parse import quote_plus
    #     url = (
    #         "postgresql+psycopg://"
    #         f"{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{name}"
    #     )
    #
    # Quote ONLY USER and PASSWORD -- quoting the whole URL would also escape the
    # "://" and "/" separators and break it.
    db_url: str = ""
    db_echo: bool = False
    # Schema control. Prod default False: framework never issues DDL -- the schema
    # is provisioned externally (see db/postgresql.sql, db/oracle.sql). Set True
    # only for local/dev to let init_db() create_all from the ORM models.
    db_auto_create: bool = False

    # Optional prefix prepended to EVERY framework table (e.g. an agent name ->
    # acme_decision_state). Empty = base names used verbatim. The *_TABLE vars below
    # stay the base names; the prefix is applied on top via settings.table().
    table_prefix: str = ""

    # Database table names (base names; table_prefix is prepended when set).
    # Configurable so a deployment can bind to a DBA-provisioned schema without a
    # code change (names, once promoted dev->stage->prod, can't be changed in code).
    # Defaults are used when the corresponding env var is unset.
    decision_state_table: str = "decision_state"
    telemetry_api_request_table: str = "telemetry_api_request"
    telemetry_model_call_table: str = "telemetry_model_call"

    # Endpoint auth: comma-separated list of valid X-API-KEY values.
    # Empty = no API-key mechanism. Combined with no OIDC issuer that disables
    # endpoint auth entirely (serve() warns loudly); never ship a default key.
    api_keys: str = ""

    # OIDC bearer verification (optional defense-in-depth). Empty oidc_issuer =
    # OFF: identity falls back to the CAE-forwarded headers (client-id / user).
    # When set, a request's `Authorization: Bearer <JWT>` is cryptographically
    # verified (RS256) against the IdP's JWKS plus iss/aud/exp (and scope, if set),
    # and the caller identity is taken from the token's claims. JWKS = the IdP's
    # PUBLIC keys, fetched from oidc_jwks (derived <issuer>/v1/keys when the
    # override is blank) and cached in memory -- nothing secret is stored. If the
    # JWKS can't be fetched we DEGRADE to header trust and log loudly (the ingress
    # already validated the token before it reached us).
    #   Okta example: OIDC_ISSUER=https://<your-tenant>.okta.com/oauth2/default
    #                 OIDC_AUDIENCE=api://default
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_required_scope: str = ""
    oidc_jwks_url: str = ""  # explicit override; else derived from oidc_issuer

    # Generic-tool URL allowlist.
    # Comma-separated hosts / URL prefixes the framework's HTTP-calling tools are
    # permitted to reach. Empty = allow none (a tool must opt in explicitly).
    tool_url_allowlist: str = ""

    # Max accepted HTTP request body size (bytes); larger requests get 413. 10 MB
    # is generous for JSON request text/metadata (files flow OUT in responses, not in)
    # while bounding memory per request.
    max_request_bytes: int = 10 * 1024 * 1024

    @property
    def auth_configured(self) -> bool:
        """True when some caller-authentication mechanism is configured."""
        return bool(self.valid_api_keys or self.oidc_issuer)

    @property
    def valid_api_keys(self) -> set[str]:
        """The configured API keys as a set (comma-separated in the env)."""
        return {key.strip() for key in self.api_keys.split(",") if key.strip()}

    @property
    def oidc_jwks(self) -> str:
        """JWKS URL for bearer verification: the explicit override if set, else
        derived from the issuer (Okta shape: <issuer>/v1/keys). Empty = OIDC off."""
        if self.oidc_jwks_url:
            return self.oidc_jwks_url
        return f"{self.oidc_issuer.rstrip('/')}/v1/keys" if self.oidc_issuer else ""

    @property
    def allowed_tool_urls(self) -> set[str]:
        """The allowlisted host / URL prefixes as a set (comma-separated in the env)."""
        return {u.strip() for u in self.tool_url_allowlist.split(",") if u.strip()}

    def table(self, name: str) -> str:
        """Base table name with table_prefix prepended (empty prefix = unchanged)."""
        return f"{self.table_prefix}_{name}" if self.table_prefix else name

    @field_validator("table_prefix")
    @classmethod
    def _strip_prefix(cls, v: str) -> str:
        """Trim whitespace + surrounding underscores so table() owns the single join _."""
        return v.strip().strip("_")


settings = Settings()
