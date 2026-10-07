"""claude-login — `/login` (and `/claude-login`) for the claude-cli bridge.

Runs `claude auth login --claudeai` in a pty inside the Hermes host, posts
its output to the chat that asked, and feeds that user's next plain message
(the code) straight to the pty, so the agent never sees it. Restricted to
the user ids in CLAUDE_BRIDGE_LOGIN_USERS.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import relay

logger = logging.getLogger(__name__)

COMMANDS = ("login", "claude-login")
LOGIN_TIMEOUT = float(os.environ.get("CLAUDE_BRIDGE_LOGIN_TIMEOUT", "600"))
USAGE = "Usage: /login (start), /login status, /login logout, /login cancel"

_ORIGIN: contextvars.ContextVar = contextvars.ContextVar("claude_login_origin", default=None)
_RECENT: Dict[str, Any] = {"origin": None, "ts": 0.0}
PENDING: Dict[Tuple[str, str, str], relay.LoginSession] = {}


class Origin:
    def __init__(self, source: Any, gateway: Any, loop: Optional[asyncio.AbstractEventLoop]) -> None:
        plat = getattr(source, "platform", None)
        self.platform = str(getattr(plat, "value", plat) or "")
        self.chat_id = str(getattr(source, "chat_id", "") or "")
        self.user_id = str(getattr(source, "user_id", "") or "")
        self.thread_id = str(getattr(source, "thread_id", "") or "")
        self.source, self.gateway, self.loop = source, gateway, loop

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.platform, self.chat_id, self.user_id)


def claude_bin() -> str:
    return os.environ.get("CLAUDE_BRIDGE_CLAUDE_BIN", str(Path.home() / ".local" / "bin" / "claude"))


def claude_env() -> Dict[str, str]:
    home = str(Path.home())
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "hermes"
    try:
        import pwd
        user = pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        pass
    return {
        "HOME": home,
        "USER": user,
        "LOGNAME": user,
        "PATH": f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "TERM": "xterm-256color",
        "LANG": "en_US.UTF-8",
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "DISABLE_AUTOUPDATER": "1",
    }


def allowed(o: Origin) -> bool:
    users = {u.strip() for u in os.environ.get("CLAUDE_BRIDGE_LOGIN_USERS", "").split(",") if u.strip()}
    return bool(o.user_id) and (o.user_id in users or f"{o.platform}:{o.user_id}" in users)


def _claude(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run([claude_bin(), *args], capture_output=True, text=True, timeout=timeout, env=claude_env(), stdin=subprocess.DEVNULL)


def status_text() -> str:
    try:
        out = _claude("auth", "status", "--json")
        data = json.loads(out.stdout or "{}")
    except Exception as exc:
        return f"Could not read Claude Code login status: {exc}"
    if data.get("loggedIn"):
        plan = data.get("subscriptionType")
        return f"Claude Code is signed in ({data.get('authMethod') or 'unknown method'}{', ' + plan if plan else ''})."
    return "Claude Code is not signed in. Send /login to sign in."


def _adapter(o: Origin) -> Any:
    for name in ("_delivery_adapter_for", "_adapter_for_source"):
        fn = getattr(o.gateway, name, None)
        if callable(fn):
            try:
                a = fn(o.source)
                if a is not None:
                    return a
            except Exception:
                pass
    adapters = getattr(o.gateway, "adapters", None) or {}
    return adapters.get(getattr(o.source, "platform", None))


def sender(o: Origin):
    def send(text: str) -> None:
        adapter = _adapter(o)
        if adapter is None or o.loop is None:
            logger.warning("claude-login: no adapter to reach %s:%s", o.platform, o.chat_id)
            return
        meta = {"thread_id": o.thread_id} if o.thread_id else None
        fut = asyncio.run_coroutine_threadsafe(adapter.send(o.chat_id, text, metadata=meta), o.loop)
        try:
            fut.result(timeout=30)
        except Exception as exc:
            logger.warning("claude-login: send failed: %s", exc)
    return send


def on_dispatch(event: Any = None, gateway: Any = None, **_: Any) -> Optional[dict]:
    try:
        source = getattr(event, "source", None)
        if source is None:
            return None
        text = str(getattr(event, "text", "") or "").strip()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        o = Origin(source, gateway, loop)
        sess = PENDING.get(o.key)
        if sess is not None and sess.alive() and text and not text.startswith("/"):
            sess.write(text)
            return {"action": "skip", "reason": "claude login input"}
        if text.startswith("/"):
            head = text[1:].split(maxsplit=1)[0].split("@")[0].lower().replace("_", "-") if len(text) > 1 else ""
            if head in COMMANDS:
                _ORIGIN.set(o)
                _RECENT.update(origin=o, ts=time.monotonic())
    except Exception:
        logger.debug("claude-login: dispatch hook failed", exc_info=True)
    return None


def _start(o: Origin) -> str:
    existing = PENDING.get(o.key)
    if existing is not None and existing.alive():
        return "A Claude Code sign-in is already waiting for your code. Paste it here, or send /login cancel."
    send = sender(o)

    def finished(rc: int) -> None:
        if PENDING.get(o.key) is sess:
            PENDING.pop(o.key, None)
        note = "Sign-in timed out after 10 minutes. " if sess.timed_out else ""
        send(note + status_text())

    sess = relay.LoginSession([claude_bin(), "auth", "login", "--claudeai"], claude_env(), send=send, on_exit=finished, timeout=LOGIN_TIMEOUT, cwd=str(Path.home()))
    try:
        sess.start()
    except Exception as exc:
        return f"Could not start `claude auth login`: {exc}"
    PENDING[o.key] = sess
    return ("Starting Claude Code sign-in. I'll post the link here: open it, approve, then paste the code "
            "you get back as a normal message. It goes straight to Claude Code; the agent never sees it. "
            "/login cancel stops it.")


def _cli(sub: str) -> str:
    if sub == "status":
        return status_text()
    if sub == "logout":
        out = _claude("auth", "logout")
        return (out.stdout or out.stderr).strip() or "Logged out."
    if sub in ("start", "login") and sys.stdin.isatty():
        subprocess.call([claude_bin(), "auth", "login", "--claudeai"], env=claude_env())
        return status_text()
    return f"No chat to relay to. Run `{claude_bin()} auth login` in a terminal as this Hermes user (HOME={Path.home()})."


def command(raw_args: str = "") -> str:
    parts = (raw_args or "").strip().split()
    sub = parts[0].lower() if parts else "start"
    o = _ORIGIN.get()
    if o is None and time.monotonic() - float(_RECENT["ts"]) < 15:
        o = _RECENT["origin"]
    if o is None or o.gateway is None:
        return _cli(sub)
    if not allowed(o):
        if not os.environ.get("CLAUDE_BRIDGE_LOGIN_USERS", "").strip():
            return f"/login is off: set CLAUDE_BRIDGE_LOGIN_USERS={o.user_id} (comma list of user ids) in the Hermes environment and restart."
        return f"/login is not enabled for your user id ({o.user_id})."
    if sub == "status":
        return status_text()
    if sub == "cancel":
        sess = PENDING.pop(o.key, None)
        if sess is not None and sess.alive():
            sess.cancel()
            return "Sign-in cancelled."
        return "No sign-in in progress."
    if sub == "logout":
        sess = PENDING.pop(o.key, None)
        if sess is not None:
            sess.cancel()
        out = _claude("auth", "logout")
        return ((out.stdout or out.stderr).strip() or "Logged out.") + "\n" + status_text()
    if sub in ("start", "login"):
        return _start(o)
    return USAGE


def _ensure_bridge() -> None:
    try:
        from providers import get_provider_profile
        p = get_provider_profile("claude-cli")
        if p is not None and hasattr(p, "ensure_bridge"):
            p.ensure_bridge(force=True)
    except Exception:
        logger.debug("claude-login: bridge autostart skipped", exc_info=True)


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", on_dispatch)
    for name in COMMANDS:
        try:
            ctx.register_command(name, command, description="Sign Claude Code in or out for the claude-cli bridge", args_hint="[status|logout|cancel]")
        except TypeError:
            ctx.register_command(name, command, description="Sign Claude Code in or out for the claude-cli bridge")
        except Exception:
            logger.warning("claude-login: could not register /%s", name, exc_info=True)
    threading.Thread(target=_ensure_bridge, daemon=True).start()
