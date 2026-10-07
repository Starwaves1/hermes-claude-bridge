"""claude-cli — Hermes model-provider profile for the local claude-bridge.

Hermes is the front end; every tool-carrying turn runs inside a persistent
Claude Code session on the bridge (127.0.0.1:8790). The profile forwards
`/effort`, tags each request with a stable per-conversation key, and starts
the bridge as a detached child when nothing answers on its port (Docker has
no launchd or systemd).

Rules: one person's subscription, for that person. Never a multi-user bot.
Never the built-in `anthropic` OAuth provider alongside it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from providers import register_provider
from providers.base import OMIT_TEMPERATURE, ProviderProfile

_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
SESSION_FIELD = "claude_bridge_session"
BASE_URL = os.environ.get("CLAUDE_CLI_BRIDGE_BASE_URL", "http://127.0.0.1:8790/v1")
CHECK_EVERY = 20.0


def _bridge_script() -> Path | None:
    candidates = [os.environ.get("CLAUDE_BRIDGE_SCRIPT", "")]
    here = Path(__file__).resolve()
    candidates += [str(here.parents[2] / "claude_bridge.py"), str(Path.home() / "hermes-claude-bridge" / "claude_bridge.py")]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    return None


class _Supervisor:
    def __init__(self, base_url: str) -> None:
        u = urllib.parse.urlparse(base_url)
        self.host, self.port = u.hostname or "127.0.0.1", u.port or 8790
        self.health = f"{u.scheme}://{self.host}:{self.port}/health"
        self.checked = 0.0
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def up(self, timeout: float = 1.0) -> bool:
        try:
            with urllib.request.urlopen(self.health, timeout=timeout) as r:
                return r.status == 200
        except Exception:
            return False

    def ensure(self, force: bool = False) -> None:
        if os.environ.get("CLAUDE_BRIDGE_AUTOSTART", "1") == "0" or self.host not in ("127.0.0.1", "localhost"):
            return
        now = time.monotonic()
        if not force and now - self.checked < CHECK_EVERY:
            return
        if not self.lock.acquire(blocking=False):
            return
        try:
            self.checked = now
            if self.proc is not None:
                self.proc.poll()
            if self.up():
                return
            script = _bridge_script()
            if script is None:
                return
            home = Path.home()
            default = home / ".hermes" / "claude-bridge" if (home / ".hermes").is_dir() else home / "claude-bridge"
            log_dir = Path(os.environ.get("CLAUDE_BRIDGE_STATE_DIR", "") or default)
            log_dir.mkdir(parents=True, exist_ok=True)
            with open(log_dir / "bridge.log", "ab") as out:
                self.proc = subprocess.Popen(
                    [sys.executable, str(script), "--host", self.host, "--port", str(self.port)],
                    stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True, close_fds=True,
                    env={**os.environ, "CLAUDE_BRIDGE_STATE_DIR": str(log_dir)},
                )
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and self.proc.poll() is None and not self.up(0.5):
                time.sleep(0.25)
        except Exception:
            pass
        finally:
            self.lock.release()


SUPERVISOR = _Supervisor(BASE_URL)


class ClaudeCliProfile(ProviderProfile):
    """Declarative profile plus hooks: `/effort` → top-level reasoning_effort,
    conversation key → extra_body, bridge kept alive."""

    def ensure_bridge(self, force: bool = False) -> None:
        SUPERVISOR.ensure(force)

    def build_extra_body(self, *, session_id=None, **context):
        key = context.get("cache_scope_id") or session_id
        return {SESSION_FIELD: str(key)} if key else {}

    def build_api_kwargs_extras(self, *, reasoning_config=None, **context):
        SUPERVISOR.ensure()
        key = context.get("cache_scope_id") or context.get("session_id")
        extra = {SESSION_FIELD: str(key)} if key else {}
        if isinstance(reasoning_config, dict):
            if reasoning_config.get("enabled") is False:
                return extra, {"reasoning_effort": "low"}
            effort = str(reasoning_config.get("effort") or "").strip().lower()
            if effort in _EFFORTS:
                return extra, {"reasoning_effort": effort}
        return extra, {}

    def fetch_models(self, *, api_key=None, timeout=8.0, **_unused):
        # The bridge needs no key. ProviderProfile.fetch_models' signature varies
        # across Hermes versions; extra kwargs are absorbed (scar 2026-09-16).
        SUPERVISOR.ensure()
        return super().fetch_models(api_key=None, timeout=timeout)


claude_cli = ClaudeCliProfile(
    name="claude-cli",
    aliases=("claude-bridge",),
    display_name="Claude (Claude Code bridge)",
    description="Claude Code as the harness via the local `claude` binary — subscription usage",
    signup_url="https://claude.ai/",
    env_vars=("CLAUDE_CLI_BRIDGE_API_KEY",),
    base_url=BASE_URL,
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
SUPERVISOR.ensure(force=True)
