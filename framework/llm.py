"""Custom LLM provider: wire an OpenAI-compatible gateway into ADK via LiteLlm.

ADK talks to any OpenAI-compatible endpoint through LiteLlm; this provider points
it at a configurable gateway, puts the OAuth token in the api-key header and a
caller identifier in the OpenAI `user` field. Creds come from the agent (see agent/config.py);
framework holds none. Token is fetched lazily and refreshed each call, so nothing
hits the network at import time and a long-running service never sends a stale one.

Gateway wiring (endpoint, creds, per-call token, identifier) lives in a custom
LiteLLMClient -- the object ADK calls to reach litellm -- so LiteLlm is built with
only its declared fields (model, llm_client). Passing gateway kwargs straight to
LiteLlm(...) trips the type checker on its Pydantic constructor.
"""

from typing import Any

from google.adk.models.lite_llm import LiteLlm, LiteLLMClient

from .auth import _get_valid_token
from .connection import Connection
from .telemetry import end_model_call, start_model_call

# Azure OpenAI deployment shape; a plain OpenAI-compatible server (e.g. the ahitung
# worker at .../v1) sets Connection.url_template="{url}".
DEFAULT_URL_TEMPLATE = "{url}/openai/deployments/{model}"


class _GatewayClient(LiteLLMClient):
    """The litellm client ADK calls, pre-loaded with the gateway config.

    Every completion is stamped with the endpoint, the identifier (user field)
    and a freshly minted OAuth token (api-key header). Both auth bits degrade:
    no client_id -> no token header; no identifier -> no user field.
    """

    def __init__(self, model: str, connection: Connection, identifier: str = ""):
        self.model = model
        self._connection = connection
        self._identifier = identifier

    def _gateway_args(self) -> dict[str, Any]:
        template = self._connection.url_template or DEFAULT_URL_TEMPLATE
        args: dict[str, Any] = {
            "base_url": template.format(url=self._connection.url, model=self.model),
            "api_key": "unused",  # the real token travels in the api-key header
        }
        if self._connection.client_id:
            args["extra_headers"] = {
                "api-key": _get_valid_token(
                    self._connection.client_id,
                    self._connection.client_secret,
                    self._connection.token_url,
                    basic_auth=self._connection.basic_auth,
                )
            }
        if self._identifier:
            args["user"] = self._identifier
        return args

    async def acompletion(self, model, messages, tools, **kwargs):
        return await super().acompletion(
            model, messages, tools, **{**kwargs, **self._gateway_args()}
        )

    def completion(self, model, messages, tools, stream=False, **kwargs):
        return super().completion(
            model, messages, tools, stream=stream, **{**kwargs, **self._gateway_args()}
        )


class _InstrumentedLlm(LiteLlm):
    """LiteLlm that records per-model-call telemetry.

    Gateway config + token refresh live in _GatewayClient; this subclass only
    wraps generate_content_async to record telemetry from the ADK-level
    LlmResponse (token counts, finish reason). identifier is read off the client
    so no extra Pydantic field is needed on LiteLlm.
    """

    async def generate_content_async(self, llm_request, stream: bool = False):
        # Per-model-call telemetry (a span of the active request; no-op off-request).
        identifier = getattr(self.llm_client, "_identifier", "")
        call = start_model_call(model_name=self.model, identifier=identifier)
        input_tokens = output_tokens = None
        finish_reason = None
        try:
            async for response in super().generate_content_async(llm_request, stream=stream):
                usage = getattr(response, "usage_metadata", None)
                if usage is not None:
                    input_tokens = getattr(usage, "prompt_token_count", None) or input_tokens
                    output_tokens = (
                        getattr(usage, "candidates_token_count", None) or output_tokens
                    )
                reason = getattr(response, "finish_reason", None)
                if reason is not None:
                    finish_reason = str(reason)
                yield response
        finally:
            end_model_call(
                call,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                finish_reason=finish_reason,
            )


def custom_llm_provider(
    model: str = "gpt-5-nano",
    *,
    connection: Connection,
    identifier: str = "",
) -> LiteLlm:
    """Build a telemetry-instrumented LiteLlm on an OpenAI-compatible gateway.

    model is per-agent; the gateway url + creds come from connection and the
    identifier from the agent (see agent/config.py). Auth degrades when creds or
    identifier are empty.
    """
    return _InstrumentedLlm(
        model=f"{model}",
        llm_client=_GatewayClient(
            model=model,
            connection=connection,
            identifier=identifier,
        ),
    )
