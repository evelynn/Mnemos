"""Process-wide provider logging guard for source-bearing LLM requests."""

from __future__ import annotations

import logging
import os


def enforce_provider_logging_privacy() -> None:
    """Disable ambient SDK/body debug logging before constructing clients.

    Anthropic's Python SDK reads ``ANTHROPIC_LOG`` from the process
    environment and its debug records can contain request options, including
    source-bearing prompts. Mnemos never authorizes that payload channel, so
    the provider and transport loggers stay at WARNING even if the parent
    deployment accidentally enabled debug logging.
    """

    os.environ["ANTHROPIC_LOG"] = "off"
    for prefix in ("anthropic", "httpx"):
        logging.getLogger(prefix).setLevel(logging.WARNING)
        for name, candidate in list(
            logging.Logger.manager.loggerDict.items()
        ):
            if name.startswith(f"{prefix}.") and isinstance(
                candidate, logging.Logger
            ):
                candidate.setLevel(logging.WARNING)
