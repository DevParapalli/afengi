"""Settings for the example agent.

Everything this agent needs -- the LLM gateway, the downstream API tier, the MCP
server and every credential -- lives here; editing it never touches the framework
(framework holds no creds). Every value is env-switchable (e.g. LLM_CLIENT_ID),
so the same code runs unchanged across dev / stage / prod. Creds are exposed to
the framework connectors as grouped Connections via the properties below.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict

from framework.connection import Connection


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM gateway (OpenAI-compatible).
    llm_base_url: str = "https://your-llm-gateway.example.com/"
    llm_token_url: str = "https://your-idp.example.com/oauth2/token"
    llm_client_id: str = ""
    llm_client_secret: str = ""
    llm_identifier: str = ""
    # Call URL shape ({url}, {model}); empty = the Azure deployment shape. "{url}"
    # for a plain OpenAI-compatible server such as the ahitung worker.
    llm_url_template: str = ""
    model: str = "gpt-5-nano"

    # Downstream API connection "api" (base url + creds + timeout). tools reach it
    # via _call(..., connection="api"); add more <name>_* blocks for other APIs.
    api_base_url: str = "https://your-api.example.com"
    api_token_url: str = "https://your-idp.example.com/oauth2/token"
    api_client_id: str = ""
    api_client_secret: str = ""
    api_timeout: int = 60

    # MCP server.
    mcp_url: str = "https://your-mcp-server.example.com/mcp/"
    mcp_token_url: str = "https://your-idp.example.com/oauth2/token"
    mcp_client_id: str = ""
    mcp_client_secret: str = ""
    mcp_user_id: str = ""
    mcp_api_key: str = ""
    # Header carrying the ADK session id; "x-ahit-session-id" for ahitung.
    mcp_session_header: str = ""

    # Grouped connection accessors (handed to the framework connectors).
    @property
    def llm_connection(self) -> Connection:
        """LLM gateway connection -- creds sent as HTTP Basic on the token request."""
        return Connection(
            url=self.llm_base_url,
            token_url=self.llm_token_url,
            client_id=self.llm_client_id,
            client_secret=self.llm_client_secret,
            basic_auth=True,
            url_template=self.llm_url_template,
        )

    @property
    def api_connection(self) -> Connection:
        """Downstream API tier connection (client-credentials form-body flow)."""
        return Connection(
            url=self.api_base_url,
            token_url=self.api_token_url,
            client_id=self.api_client_id,
            client_secret=self.api_client_secret,
            timeout=self.api_timeout,
        )

    @property
    def mcp_connection(self) -> Connection:
        """MCP connection: bearer client-credentials plus the user-id / api-key headers."""
        return Connection(
            url=self.mcp_url,
            token_url=self.mcp_token_url,
            client_id=self.mcp_client_id,
            client_secret=self.mcp_client_secret,
            user_id=self.mcp_user_id,
            api_key=self.mcp_api_key,
            session_header=self.mcp_session_header,
        )


settings = Settings()
