"""claude-cli — Hermes model-provider profile for the local claude-bridge.

The bridge (claude_bridge.py, run by hand or as a LaunchAgent)
listens on 127.0.0.1:8790 and answers every chat completion by spawning the
unmodified `claude -p` binary with tools disabled. Hermes keeps its own
agent loop; the bridge translates Hermes tool schemas into a JSON answer
contract and back. Subscription usage, no API key, no token in Hermes.

Rules: personal single-user agent only. Never a multi-user bot. Never the
built-in `anthropic` OAuth provider alongside it.
"""
from __future__ import annotations

import os

from providers import register_provider
from providers.base import OMIT_TEMPERATURE, ProviderProfile

_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}


class ClaudeCliProfile(ProviderProfile):
    """Declarative profile plus one hook: forward Hermes reasoning effort
    as the standard top-level `reasoning_effort` kwarg so `/effort` in chat
    reaches `claude --effort` on the next turn."""

    def build_api_kwargs_extras(self, *, reasoning_config=None, **context):
        if isinstance(reasoning_config, dict):
            if reasoning_config.get("enabled") is False:
                return {}, {"reasoning_effort": "low"}
            effort = str(reasoning_config.get("effort") or "").strip().lower()
            if effort in _EFFORTS:
                return {}, {"reasoning_effort": effort}
        return {}, {}

    def fetch_models(self, *, api_key=None, base_url=None, timeout=8.0):
        # The bridge needs no key; never send whatever placeholder is in .env.
        return super().fetch_models(api_key=None, base_url=base_url, timeout=timeout)


claude_cli = ClaudeCliProfile(
    name="claude-cli",
    aliases=("claude-bridge",),
    display_name="Claude (Code CLI bridge)",
    description="Claude via the local `claude -p` binary — subscription usage, tools stay in Hermes",
    signup_url="https://claude.ai/",
    env_vars=("CLAUDE_CLI_BRIDGE_API_KEY",),
    # CLAUDE_CLI_BRIDGE_BASE_URL exists for tests (point one CLI run at a
    # second bridge instance); the gateway never sets it.
    base_url=os.environ.get("CLAUDE_CLI_BRIDGE_BASE_URL", "http://127.0.0.1:8790/v1"),
    auth_type="api_key",
    supports_health_check=True,
    fixed_temperature=OMIT_TEMPERATURE,
    default_aux_model="sonnet",
    fallback_models=(
        "fable",
        "opus",
        "sonnet",
        "haiku",
        "claude-fable-5-1",
        "claude-opus-4-8",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    ),
)

register_provider(claude_cli)
