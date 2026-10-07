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
import re
import subprocess
import sys
import threading
import time
import types
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
SWALLOW: Dict[Tuple[str, str, str], float] = {}
MAX_MESSAGE = 1900
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


def _provider():
    try:
        from providers import get_provider_profile
        return get_provider_profile("claude-cli")
    except Exception:
        return None


def _token() -> str:
    p = _provider()
    return (p.bridge_token() if p is not None and hasattr(p, "bridge_token") else None) or ""


def bridge_call(method: str, path: str, body: Optional[dict] = None, timeout: float = 10) -> Tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BRIDGE + path, data=data, method=method, headers={"Content-Type": "application/json", "X-Bridge-Token": _token()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        with e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except Exception:
                return e.code, {}


def chunks(text: str, limit: int = MAX_MESSAGE) -> list:
    """Split on line boundaries so each piece fits a chat message."""
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out or [""]


def _send_to(platform: str, chat_id: str, thread_id: str, text: str) -> None:
    gateway, loop = _GATEWAY["gateway"], _GATEWAY["loop"]
    adapter = None
    for k, a in (getattr(gateway, "adapters", None) or {}).items():
        if str(getattr(k, "value", k)) == platform:
            adapter = a
    if adapter is None or loop is None:
        logger.warning("claude-login: no adapter for %s, message not delivered", platform)
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    for piece in chunks(text):
        coro = adapter.send(chat_id, piece, metadata={"thread_id": thread_id} if thread_id else None)
        if running is loop:
            loop.create_task(coro)
            continue
        try:
            asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=30)
        except Exception as exc:
            logger.warning("claude-login: send failed: %s", exc)


def poll_approvals(stop: Optional[threading.Event] = None, wait: float = 25) -> None:
    after, instance = 0, ""
    while not (stop and stop.is_set()):
        try:
            status, data = bridge_call("GET", f"/approvals?after={after}&wait={wait}", timeout=wait + 15)
        except Exception:
            time.sleep(3)
            continue
        if status == 401:
            p = _provider()
            if p is not None and hasattr(p, "ensure_bridge"):
                p.ensure_bridge(force=True, takeover=True)
            time.sleep(3)
            continue
        if status != 200:
            time.sleep(3)
            continue
        if data.get("instance") != instance:  # a new bridge process: its ids start over
            if instance:
                with _APPROVAL_LOCK:
                    APPROVALS.clear()
                after, instance = 0, str(data.get("instance") or "")
                continue
            instance = str(data.get("instance") or "")
        for item in data.get("approvals") or []:
            key = (str(item.get("platform")), str(item.get("chat_id")))
            state = item.get("state")
            with _APPROVAL_LOCK:
                lst = APPROVALS.setdefault(key, [])
                known = any(i["id"] == item["id"] for i in lst)
                if state == "pending":
                    if not known:
                        lst.append(item)
                else:
                    lst[:] = [i for i in lst if i["id"] != item["id"]]
            thread = str(item.get("thread_id") or "")
            if state == "pending" and not known:
                _send_to(key[0], key[1], thread, str(item.get("text") or ""))
            elif state == "notice":
                _send_to(key[0], key[1], thread, str(item.get("text") or ""))
            elif state == "timeout" and known:
                _send_to(key[0], key[1], thread, f"[{item.get('short')}] No answer in time; Claude Code was told no ({item.get('tool')}).")
        after = int(data.get("seq") or after)


_ID_FIRST = re.compile(r"^#?(\w+)\s+(.+)$", re.S)
_ID_LAST = re.compile(r"^(y|yes|n|no)\s+#?(\w+)$", re.I)


def pick_approval(o: Origin, text: str) -> Tuple[Optional[dict], str, bool]:
    """(item, reply, ambiguous) for a chat message. The replier's own pending
    requests come first; admins may answer others'. With several open, the
    reply must name one: "y 12", "n 12" or "12 <reason>"."""
    with _APPROVAL_LOCK:
        lst = list(APPROVALS.get((o.platform, o.chat_id), []))
    own = [i for i in lst if str(i.get("user_id")) == o.user_id]
    cands = own or ([i for i in lst if allowed(o)] if lst else [])
    if not cands:
        return None, text, False
    by_short = {str(i.get("short")): i for i in cands}
    m = _ID_LAST.match(text.strip())
    if m and m.group(2) in by_short:
        return by_short[m.group(2)], m.group(1), False
    m = _ID_FIRST.match(text.strip())
    if m and m.group(1) in by_short and len(cands) > 1:
        return by_short[m.group(1)], m.group(2), False
    if len(cands) == 1:
        return cands[0], text, False
    return None, text, True


def _answer_approval(o: Origin, text: str) -> bool:
    item, reply, ambiguous = pick_approval(o, text)
    if ambiguous:
        _send_to(o.platform, o.chat_id, o.thread_id, "Several approvals are open. Reply `y <number>`, `n <number>` or `<number> <reason>`.")
        return True
    if item is None:
        return False
    with _APPROVAL_LOCK:
        cur = APPROVALS.get((o.platform, o.chat_id), [])
        cur[:] = [i for i in cur if i["id"] != item["id"]]

    def post() -> None:
        try:
            status, data = bridge_call("POST", f"/approvals/{item['id']}", {"user_id": o.user_id, "platform": o.platform, "text": reply}, timeout=10)
        except Exception as exc:
            status, data = 0, {"error": str(exc)}
        if status == 200:
            if item.get("tool") != "AskUserQuestion":
                _send_to(o.platform, o.chat_id, o.thread_id, f"[{item.get('short')}] " + ("Allowed." if data.get("decision") == "allow" else "Denied."))
        else:
            _send_to(o.platform, o.chat_id, o.thread_id, f"[{item.get('short')}] Your answer was not taken ({data.get('error') or status}); it did not reach the agent either.")

    threading.Thread(target=post, name="claude-approval-answer", daemon=True).start()
    return True


def _capture_gateway(gateway: Any, loop: Optional[asyncio.AbstractEventLoop]) -> None:
    if gateway is None or loop is None:
        return
    _GATEWAY["gateway"], _GATEWAY["loop"] = gateway, loop
    if _GATEWAY["poller"] is None:
        _GATEWAY["poller"] = threading.Thread(target=poll_approvals, name="claude-approvals", daemon=True)
        _GATEWAY["poller"].start()
        p = _provider()
        if p is not None and hasattr(p, "ensure_bridge") and not _token():
            threading.Thread(target=p.ensure_bridge, kwargs={"force": True, "takeover": True}, daemon=True).start()


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
        plain = bool(text) and not text.startswith("/")
        sess = PENDING.get(o.key)
        if sess is not None and sess.alive() and plain:
            sess.write(text)
            return {"action": "skip", "reason": "claude login input"}
        if plain and SWALLOW.get(o.key, 0) > time.monotonic():
            SWALLOW.pop(o.key, None)
            _send_to(o.platform, o.chat_id, o.thread_id, "That message was held back: the Claude Code sign-in had already ended, and it may have been a code. Send it again if it was meant for the agent, or /login to retry.")
            return {"action": "skip", "reason": "claude login ended"}
        if plain and _answer_approval(o, text):
            return {"action": "skip", "reason": "claude approval answer"}
        if text.startswith("/"):
            head = text[1:].split(maxsplit=1)[0].split("@")[0].lower().replace("_", "-") if len(text) > 1 else ""
            if head in COMMANDS:
                _ORIGIN.set(o)
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
        status = status_text()
        if rc != 0 or sess.timed_out or not status.startswith("Claude Code is signed in"):
            SWALLOW[o.key] = time.monotonic() + LOGIN_TIMEOUT
        note = "Sign-in timed out after 10 minutes. " if sess.timed_out else ""
        send(note + status)

    sess = relay.LoginSession([claude_bin(), "auth", "login", "--claudeai"], claude_env(), send=send, on_exit=finished, timeout=LOGIN_TIMEOUT, cwd=str(Path.home()))
    try:
        sess.start()
    except Exception as exc:
        return f"Could not start `claude auth login`: {exc}"
    PENDING[o.key] = sess
    SWALLOW.pop(o.key, None)
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


def invoking_origin() -> Optional[Origin]:
    """Who ran the command: Hermes's session context when it is set, else the
    origin the dispatch hook recorded for this same message (task-local)."""
    try:
        from gateway.session_context import get_session_env
        plat, chat, user = (get_session_env(f"HERMES_SESSION_{k}", "") for k in ("PLATFORM", "CHAT_ID", "USER_ID"))
    except Exception:
        plat = chat = user = ""
    hooked = _ORIGIN.get()
    if plat and chat and user and _GATEWAY["gateway"] is not None:
        if hooked is not None and hooked.key == (plat, chat, user):
            return hooked
        src = getattr(hooked, "source", None)
        if src is None:
            for k in (getattr(_GATEWAY["gateway"], "adapters", None) or {}):
                if str(getattr(k, "value", k)) == plat:
                    src = types.SimpleNamespace(platform=k, chat_id=chat, user_id=user, thread_id="")
        if src is not None:
            o = Origin(src, _GATEWAY["gateway"], _GATEWAY["loop"])
            o.platform, o.chat_id, o.user_id = plat, chat, user
            return o
    return hooked


def command(raw_args: str = "") -> str:
    parts = (raw_args or "").strip().split()
    sub = parts[0].lower() if parts else "start"
    o = invoking_origin()
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
    p = _provider()
    if p is not None and hasattr(p, "ensure_bridge"):
        try:
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
