"""claude-login — the chat side of the claude-cli bridge.

- `/login` (and `/claude-login`) runs `claude auth login --claudeai` in a pty
  inside the Hermes host, posts its output to the chat that asked, and feeds
  that user's next plain message (the code) straight to the pty, so the agent
  never sees it. Restricted to the user ids in CLAUDE_BRIDGE_LOGIN_USERS.
- Approvals: long-polls the bridge for Claude Code permission requests,
  posts each to the chat that owns the turn, and takes the owner's (or an
  admin's) next plain message there as the answer.
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
import urllib.error
import urllib.request
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
BRIDGE = os.environ.get("CLAUDE_CLI_BRIDGE_BASE_URL", "http://127.0.0.1:8790/v1").rstrip("/")
APPROVALS: Dict[Tuple[str, str], list] = {}
_APPROVAL_LOCK = threading.Lock()
_GATEWAY: Dict[str, Any] = {"gateway": None, "loop": None, "poller": None}


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


def bridge_call(method: str, path: str, body: Optional[dict] = None, timeout: float = 10) -> Tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BRIDGE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        with e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except Exception:
                return e.code, {}


def _send_to(platform: str, chat_id: str, thread_id: str, text: str) -> None:
    gateway, loop = _GATEWAY["gateway"], _GATEWAY["loop"]
    adapter = None
    for k, a in (getattr(gateway, "adapters", None) or {}).items():
        if str(getattr(k, "value", k)) == platform:
            adapter = a
    if adapter is None or loop is None:
        logger.warning("claude-login: no adapter for %s, approval prompt not delivered", platform)
        return
    coro = adapter.send(chat_id, text, metadata={"thread_id": thread_id} if thread_id else None)
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        loop.create_task(coro)
        return
    try:
        asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=30)
    except Exception as exc:
        logger.warning("claude-login: approval send failed: %s", exc)


def poll_approvals(stop: Optional[threading.Event] = None, wait: float = 25) -> None:
    after = 0
    while not (stop and stop.is_set()):
        try:
            status, data = bridge_call("GET", f"/approvals?after={after}&wait={wait}", timeout=wait + 15)
        except Exception:
            time.sleep(3)
            continue
        if status != 200:
            time.sleep(3)
            continue
        seq = int(data.get("seq") or 0)
        if seq < after:  # bridge restarted
            after = 0
            continue
        for item in data.get("approvals") or []:
            key = (str(item.get("platform")), str(item.get("chat_id")))
            with _APPROVAL_LOCK:
                lst = APPROVALS.setdefault(key, [])
                known = any(i["id"] == item["id"] for i in lst)
                if item.get("state") == "pending":
                    if not known:
                        lst.append(item)
                else:
                    lst[:] = [i for i in lst if i["id"] != item["id"]]
            if item.get("state") == "pending" and not known:
                _send_to(key[0], key[1], str(item.get("thread_id") or ""), str(item.get("text") or ""))
            elif item.get("state") == "timeout" and known:
                _send_to(key[0], key[1], str(item.get("thread_id") or ""), f"No answer in time; Claude Code was told no ({item.get('tool')}).")
        after = seq


def _answer_approval(o: Origin, text: str) -> bool:
    with _APPROVAL_LOCK:
        lst = list(APPROVALS.get((o.platform, o.chat_id), []))
    for item in lst:
        if o.user_id != str(item.get("user_id")) and not allowed(o):
            continue
        try:
            status, data = bridge_call("POST", f"/approvals/{item['id']}", {"user_id": o.user_id, "platform": o.platform, "text": text}, timeout=5)
        except Exception:
            return False
        if status in (200, 404):
            with _APPROVAL_LOCK:
                cur = APPROVALS.get((o.platform, o.chat_id), [])
                cur[:] = [i for i in cur if i["id"] != item["id"]]
        if status == 200:
            if item.get("tool") != "AskUserQuestion":
                _send_to(o.platform, o.chat_id, o.thread_id, "Allowed." if data.get("decision") == "allow" else "Denied.")
            return True
    return False


def _capture_gateway(gateway: Any, loop: Optional[asyncio.AbstractEventLoop]) -> None:
    if gateway is None or loop is None:
        return
    _GATEWAY["gateway"], _GATEWAY["loop"] = gateway, loop
    if _GATEWAY["poller"] is None:
        _GATEWAY["poller"] = threading.Thread(target=poll_approvals, name="claude-approvals", daemon=True)
        _GATEWAY["poller"].start()


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
        _capture_gateway(gateway, loop)
        sess = PENDING.get(o.key)
        if sess is not None and sess.alive() and text and not text.startswith("/"):
            sess.write(text)
            return {"action": "skip", "reason": "claude login input"}
        if text and not text.startswith("/") and _answer_approval(o, text):
            return {"action": "skip", "reason": "claude approval answer"}
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
