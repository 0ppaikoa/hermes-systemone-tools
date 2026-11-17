"""hermes-systemone-tools — the ``decide`` tool for the System 1 decision API.

Tool config lives under ``plugins.entries.hermes-systemone-tools`` in
config.yaml. This plugin registers a tool only; install ``hermes-systemone``
separately if you also want the model-provider side.
"""

from __future__ import annotations


def register(ctx) -> None:
    """Register the ``decide`` tool (called by the plugin loader)."""
    from .decide import DECIDE_SCHEMA, decide

    ctx.register_tool(
        name="decide",
        toolset="systemone",
        schema=DECIDE_SCHEMA,
        handler=decide,
        description="Ask typed questions about state and get raw System 1 answers.",
    )
