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

import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from providers import register_provider
from providers.base import OMIT_TEMPERATURE, ProviderProfile

_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
SESSION_FIELD = "claude_bridge_session"
ORIGIN_FIELD = "claude_bridge_origin"
FORK_FIELD = "claude_bridge_fork"
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


def _state_dir() -> Path:
    home = Path.home()
    default = home / ".hermes" / "claude-bridge" if (home / ".hermes").is_dir() else home / "claude-bridge"
    return Path(os.environ.get("CLAUDE_BRIDGE_STATE_DIR", "") or default)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class _Supervisor:
    """Starts the bridge and holds its approvals control token in memory.

    The token goes to the bridge over an inherited pipe (never env or disk).
    A bridge this process did not start is left alone while its owner lives;
    an orphan (owner gone, e.g. a previous gateway) is replaced.
    """

    def __init__(self, base_url: str) -> None:
        u = urllib.parse.urlparse(base_url)
        self.host, self.port = u.hostname or "127.0.0.1", u.port or 8790
        self.root = f"{u.scheme}://{self.host}:{self.port}"
        self.checked = 0.0
        self.proc: subprocess.Popen | None = None
        self.token: str | None = None
        self.lock = threading.Lock()

    def health(self, timeout: float = 1.0) -> dict | None:
        try:
            with urllib.request.urlopen(self.root + "/health", timeout=timeout) as r:
                return json.loads(r.read() or b"{}") if r.status == 200 else None
        except Exception:
            return None

    def up(self, timeout: float = 1.0) -> bool:
        return self.health(timeout) is not None

    def authorized(self) -> bool:
        if not self.token:
            return False
        req = urllib.request.Request(self.root + "/v1/approvals?after=0&wait=0", headers={"X-Bridge-Token": self.token})
        try:
            with urllib.request.urlopen(req, timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    def _replace(self, health: dict) -> bool:
        try:
            info = json.loads((_state_dir() / "bridge.pid").read_text())
        except Exception:
            return False
        pid = int(info.get("pid") or 0)
        if not pid or pid != int(health.get("pid") or -1):
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and self.up(0.5):
            time.sleep(0.25)
        if self.up(0.5):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            time.sleep(0.5)
        return not self.up(0.5)

    def ensure(self, force: bool = False, takeover: bool = False) -> None:
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
            health = self.health()
            if health is not None:
                if self.authorized():
                    return
                try:
                    owner = int(json.loads((_state_dir() / "bridge.pid").read_text()).get("owner") or 0)
                except Exception:
                    return  # not started by this plugin (e.g. a LaunchAgent): leave it
                if owner and owner != os.getpid() and _alive(owner) and not takeover:
                    return
                if not self._replace(health):
                    return
            self._spawn()
        except Exception:
            pass
        finally:
            self.lock.release()

    def _spawn(self) -> None:
        script = _bridge_script()
        if script is None:
            return
        state = _state_dir()
        state.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        r, w = os.pipe()
        try:
            os.write(w, token.encode())
        finally:
            os.close(w)
        try:
            with open(state / "bridge.out", "ab") as out:
                self.proc = subprocess.Popen(
                    [sys.executable, str(script), "--host", self.host, "--port", str(self.port), "--token-fd", str(r), "--owner-pid", str(os.getpid())],
                    stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True, close_fds=True, pass_fds=(r,),
                    env={**os.environ, "CLAUDE_BRIDGE_STATE_DIR": str(state)},
                )
        finally:
            os.close(r)
        self.token = token
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and self.proc.poll() is None and not self.up(0.5):
            time.sleep(0.25)


SUPERVISOR = _Supervisor(BASE_URL)


def _calling_agent():
    """The Hermes AIAgent whose request is being built (a local of
    build_api_kwargs and friends), or None."""
    f = sys._getframe(2)
    for _ in range(30):
        if f is None:
            return None
        for name in ("agent", "self"):
            a = f.f_locals.get(name)
            if a is not None and hasattr(a, "session_id") and hasattr(a, "_memory_write_origin"):
                return a
        f = f.f_back
    return None


def _is_fork(agent) -> bool:
    """Background review / curator forks reuse the user's session id but are
    not user turns (agent/background_review.py, v0.20.4 and later)."""
    return bool(agent is not None and (getattr(agent, "_persist_disabled", False) or getattr(agent, "_memory_write_origin", "") == "background_review"))


def _origin() -> dict:
    """Where the turn came from, so the bridge can ask that person to approve."""
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return {}
    o = {k: get_session_env(f"HERMES_SESSION_{k.upper()}", "") for k in ("platform", "chat_id", "thread_id", "user_id")}
    return {k: str(v) for k, v in o.items() if v} if o.get("platform") and o.get("chat_id") else {}


class ClaudeCliProfile(ProviderProfile):
    """Declarative profile plus hooks: `/effort` → top-level reasoning_effort,
    conversation key and chat origin → extra_body, bridge kept alive."""

    def ensure_bridge(self, force: bool = False, takeover: bool = False) -> None:
        SUPERVISOR.ensure(force, takeover)

    def bridge_token(self) -> str | None:
        return SUPERVISOR.token

    def _routing(self, key) -> dict:
        if not key:
            return {}
        agent = _calling_agent()
        if _is_fork(agent):
            return {SESSION_FIELD: f"fork:{key}:{id(agent):x}", FORK_FIELD: {"parent": str(key)}}
        extra = {SESSION_FIELD: str(key)}
        origin = _origin()
        if origin:
            extra[ORIGIN_FIELD] = origin
        return extra

    def build_extra_body(self, *, session_id=None, **context):
        return self._routing(context.get("cache_scope_id") or session_id)

    def build_api_kwargs_extras(self, *, reasoning_config=None, **context):
        SUPERVISOR.ensure()
        extra = self._routing(context.get("cache_scope_id") or context.get("session_id"))
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
