"""HTTP entrypoint -- serve this agent's endpoints (agent card + native ``/a2a``
+ the ``/decide`` contract).

Run either way:

    uv run main.py                                       # bind HOST / PORT and serve
    uv run uvicorn main:app --host 0.0.0.0 --port 8080   # drive uvicorn yourself

The bind address comes from ``HOST`` / ``PORT`` (see ``framework/config.py``).
One agent is deployed per instance: the agent package is imported directly.

For the ADK dev tools (visual UI / one-shot CLI), run from the project root:

    uv run adk web .    # visual dev UI
    uv run adk run .    # one-shot / interactive CLI
"""

import uvicorn

from agent import root_agent, root_card
from framework import serve
from framework.config import settings

app = serve(root_agent, card_info=root_card)


def main() -> None:
    uvicorn.run("main:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
