"""The example agent.

Wires the system prompt, the API tools and (optionally) a subset of MCP tools into
a deployable ADK agent. ``agent/__init__.py`` re-exports the ``root_agent`` defined
here for ``main.py`` and the ADK CLI.

This is the only part of the repo you replace to build your own agent: swap the
tools, the prompt and the card flavour below. The framework needs no changes.
"""

from framework import CardInfo, build_agent

from . import config, tools
from .prompts import SYSTEM_PROMPT

root_agent = build_agent(
    name="example_agent",
    description="Resolves order, inventory and account support requests.",
    instruction=SYSTEM_PROMPT,
    model=config.settings.model,
    connection=config.settings.llm_connection,
    identifier=config.settings.llm_identifier,
    tools=[
        tools.order_status,
        tools.order_refund_status,
        tools.inventory_check,
        tools.account_lookup,
        # mcp_toolset(config.settings.mcp_connection),
    ],
)

# Domain flavour for the card's resolution skill; the framework appends the fixed
# A2A contract wording and the version.
root_card = CardInfo(
    tags=["support", "a2a", "orders", "resolution"],
    examples=["Where is my order ORD-118422?"],
    skill_description=(
        "Resolves commerce support requests: order status, refunds, stock "
        "availability and customer account lookups."
    ),
    documentation_url="",
)
