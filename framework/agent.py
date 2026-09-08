"""Assemble an ADK agent from a prompt and a list of tools."""

from collections.abc import Sequence
from dataclasses import dataclass, field

from google.adk.agents import LlmAgent

from .connection import Connection
from .delegation import Subagent, make_handoff_tool, register_subagents
from .llm import custom_llm_provider


@dataclass(frozen=True)
class CardInfo:
    """Domain-owned metadata for the public agent card.

    The framework owns the card's structure and the A2A contract wording (skill
    ids, the terminal-state guarantee, transport and security);
    the agent supplies its own domain flavour here so domain terms never live in
    the framework. Passed to serve().

    Fields:
        tags: keyword tags for the resolution skill.
        examples: example prompts for the resolution skill.
        skill_description: what this agent's resolution skill does functionally. The
            framework appends the fixed A2A contract sentence; left blank, a
            generic domain sentence is used.
        documentation_url: optional link to this agent's documentation.
    """

    tags: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    skill_description: str = ""
    documentation_url: str = ""


def build_agent(
    name: str,
    instruction: str,
    tools: Sequence = (),
    description: str = "",
    *,
    model: str = "gpt-5-nano",
    connection: Connection,
    identifier: str = "",
    subagents: Sequence[Subagent] = (),
) -> LlmAgent:
    """Build an LlmAgent on the custom LLM provider.

    The gateway url + creds come from connection (framework holds none); the app
    identifier is passed separately. tools may mix plain functions (ADK wraps via
    hints+docstrings) and toolsets like mcp_toolset(...). subagents are downstream
    agents this one may hand a request to: they compile into one auto `handoff` tool
    and the runner performs the single-hop A2A call (see delegation.py).
    """
    tools = list(tools)
    subagents = list(subagents)
    if subagents:
        tools.append(make_handoff_tool(subagents))
        register_subagents(name, subagents)
    return LlmAgent(
        name=name,
        model=custom_llm_provider(
            model=model,
            connection=connection,
            identifier=identifier,
        ),
        description=description,
        instruction=instruction,
        tools=tools,
    )
