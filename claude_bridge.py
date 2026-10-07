#!/usr/bin/env python3
"""claude_bridge.py — Hermes front end, Claude Code harness.

An OpenAI-compatible loopback server (chat completions + models). Requests
that carry Hermes tools run as turns of a persistent Claude Code session
(`claude -p --resume`, auto permission mode, all of Claude Code's own tools,
MCP servers, skills and CLAUDE.md). Hermes tools that Claude Code lacks are
requested through a JSON reply contract (--json-schema); Hermes runs them
and the results come back as the next turn. Requests without tools (titles,
summaries) stay stateless with every customisation off (--safe-mode).

The binary owns its own claude.ai login; this process never sees a token.
Stdlib only. Python 3.9+.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import http.client
import json
import logging
import os
import pwd
import queue
import re
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

__version__ = "2.0.0"


def _user() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:  # containers often run a uid with no passwd entry
        return os.environ.get("USER") or "hermes"


def _env_list(name: str, default: Iterable[str] = ()) -> Set[str]:
    raw = os.environ.get(name)
    if raw is None:
        return set(default)
    return {x.strip() for x in raw.split(",") if x.strip()}


HOME = Path(os.path.expanduser("~"))
USER = _user()
_DEFAULT_STATE = HOME / ".hermes" / "claude-bridge" if (HOME / ".hermes").is_dir() else HOME / "claude-bridge"
STATE_DIR = Path(os.environ.get("CLAUDE_BRIDGE_STATE_DIR", str(_DEFAULT_STATE)))
CLAUDE_BIN = os.environ.get("CLAUDE_BRIDGE_CLAUDE_BIN", str(HOME / ".local" / "bin" / "claude"))
WORKDIR = Path(os.environ.get("CLAUDE_BRIDGE_WORKDIR", str(HOME / "workspace")))
SCRATCH_DIR = STATE_DIR / "workdir"
SESSIONS_FILE = STATE_DIR / "sessions.json"
LOG_FILE = Path(os.environ.get("CLAUDE_BRIDGE_LOG", str(STATE_DIR / "bridge.log")))
DUMP_DIR = os.environ.get("CLAUDE_BRIDGE_DUMP_DIR", "")  # debugging only: writes conversation content
MAX_CONCURRENCY = int(os.environ.get("CLAUDE_BRIDGE_MAX_CONCURRENCY", "2"))
SPAWN_TIMEOUT = float(os.environ.get("CLAUDE_BRIDGE_TIMEOUT", "1800"))
QUEUE_WAIT = float(os.environ.get("CLAUDE_BRIDGE_QUEUE_WAIT", "120"))
CONTEXT_LENGTH = int(os.environ.get("CLAUDE_BRIDGE_CONTEXT_LENGTH", "1000000"))
HEARTBEAT_SECONDS = float(os.environ.get("CLAUDE_BRIDGE_HEARTBEAT", "10"))
PERMISSION_MODE = os.environ.get("CLAUDE_BRIDGE_PERMISSION_MODE", "auto")
SESSION_TTL = float(os.environ.get("CLAUDE_BRIDGE_SESSION_TTL_DAYS", "30")) * 86400
ADOPT_WINDOW = 12 * 3600
APPROVAL_TIMEOUT = float(os.environ.get("CLAUDE_BRIDGE_APPROVAL_TIMEOUT", "600"))
BG_WAIT = float(os.environ.get("CLAUDE_BRIDGE_BG_WAIT", "1800"))
BOOTSTRAP_MAX_CHARS = int(os.environ.get("CLAUDE_BRIDGE_BOOTSTRAP_MAX_CHARS", "100000"))
FORK_TTL = 86400.0
INSTANCE = uuid.uuid4().hex[:12]
CONTROL: Dict[str, str] = {"token": ""}  # set from --token-fd; memory only
BG_GRACE = 10.0
POLL_STALE = 60.0
FAKE_ERROR = os.environ.get("CLAUDE_BRIDGE_FAKE_ERROR", "")  # test hook: "limit" | "login" | "crash"

# Hermes tools Claude Code already has natively; they are not offered twice.
DEFAULT_DROP_TOOLS = (
    "terminal", "process", "process_manage", "read_file", "write_file", "patch", "search_files",
    "web_search", "web_extract", "vision_analyze", "execute_code", "delegate_task",
    "todo", "todo_list", "manage_connections",
)
DROP_TOOLS = _env_list("CLAUDE_BRIDGE_DROP_TOOLS", DEFAULT_DROP_TOOLS) - _env_list("CLAUDE_BRIDGE_KEEP_TOOLS")

STRUCTURED_TOOL = "StructuredOutput"
SESSION_FIELD = "claude_bridge_session"
ORIGIN_FIELD = "claude_bridge_origin"
FORK_FIELD = "claude_bridge_fork"

MODEL_ALIASES = ("fable", "opus", "sonnet", "haiku")
MODEL_IDS = (
    "claude-fable-5-1",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-haiku-4-5",
)
_MODEL_RE = re.compile(r"^claude-[a-z0-9.-]+(\[1m\])?$")

EFFORT_MAP = {
    "none": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
    "ultra": "max",
}
DEFAULT_EFFORT = "medium"

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {"type": "string"},
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "arguments": {"type": "object", "additionalProperties": True},
                },
                "required": ["name", "arguments"],
            },
        },
    },
    "required": ["content", "tool_calls"],
}

HARNESS_HEADER = """

# Running behind Hermes

You are Claude Code, and the user reaches you through Hermes, a chat gateway
(Discord, Telegram, CLI, ...) that also keeps memory, schedules and voice.
Two kinds of tools exist in this conversation:

1. Your own Claude Code tools: Bash, Read, Edit, Write, Glob, Grep, WebSearch,
   WebFetch, Agent, TodoWrite, your MCP servers and connectors, skills. They
   are native. Use them directly and freely, as often as the task needs,
   before you answer.
2. Hermes tools, listed below. They are NOT native: calling one natively
   fails with "No such tool available". Request them in your final reply;
   Hermes runs them and sends the results back as your next message, inside
   <tool_result> blocks.

<<DROPPED>>Finish every turn by calling StructuredOutput exactly once (do not write the
reply as plain text first) with:
- "content": your message to the user; Hermes delivers it. It may be empty
  while you wait for Hermes tool results.
- "tool_calls": a list of {"name": <Hermes tool>, "arguments": <object
  matching its parameters>}. Use [] when no Hermes tool is needed.

## Hermes tools
"""

TRANSCRIPT_PREAMBLE = """Below is the conversation so far, as a transcript. You are the assistant.
Messages are wrapped in <user>, <assistant> and <tool_result> tags; your
earlier tool calls appear inside <tool_calls>. Write ONLY your next assistant
turn, addressed to the user, with no tags and no transcript formatting.

"""

BOOTSTRAP_PREAMBLE = """This conversation started in Hermes before this Claude Code session existed.
Its history so far, as a transcript (your earlier turns are <assistant>; tool
calls you see there were Hermes tools):

"""

RESEND_NUDGE = "(Hermes sent this turn again: your previous reply never reached the user. Give your final reply again.)"

_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_DATA_URL = re.compile(r"^data:([a-zA-Z0-9.+/-]+);base64,(.*)$", re.S)

log = logging.getLogger("claude_bridge")
_slots = threading.BoundedSemaphore(MAX_CONCURRENCY)


# ── helpers ──────────────────────────────────────────────────────────


def child_env() -> Dict[str, str]:
    """Minimal, clean environment for every `claude` child.

    USER unlocks the macOS keychain login. Nothing ANTHROPIC_* or CLAUDE*
    passes through, so an API key cannot bill and a parent session cannot nest.
    """
    return {
        "HOME": str(HOME),
        "USER": USER,
        "LOGNAME": USER,
        "PATH": f"{HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "TERM": "dumb",
        "LANG": "en_US.UTF-8",
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "DISABLE_AUTOUPDATER": "1",
    }


_STRIP_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_BRIDGE_", "CLAUDE_CLI_BRIDGE_")


def harness_env() -> Dict[str, str]:
    """Environment for tool-path children: the bridge's own (PATH, proxies, CA
    bundles, SSH agent, tokens the user's tools need) minus anything that
    could bill an API key, nest a parent Claude Code, or reach the bridge's
    control channel."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(_STRIP_PREFIXES) and k not in ("CLAUDECODE",)}
    env.update({"HOME": str(HOME), "USER": USER, "LOGNAME": USER, "DISABLE_AUTOUPDATER": "1"})
    path = env.get("PATH") or "/usr/local/bin:/usr/bin:/bin"
    local = f"{HOME}/.local/bin"
    if local not in path.split(":"):
        path = f"{local}:{path}"
    env["PATH"] = path
    env.setdefault("LANG", "en_US.UTF-8")
    env.setdefault("TMPDIR", "/tmp")
    env["TERM"] = "dumb"
    return env


_HELP: Dict[str, str] = {}


def claude_supports(flag: str) -> bool:
    if "text" not in _HELP:
        try:
            out = subprocess.run([CLAUDE_BIN, "--help"], capture_output=True, text=True, timeout=30, env=child_env())
            _HELP["text"] = out.stdout + out.stderr
        except Exception:
            _HELP["text"] = ""
    return flag in _HELP["text"]


def normalize_model(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    name = name.strip()
    for prefix in ("claude-cli/", "claude-bridge/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    if name in MODEL_ALIASES or name in MODEL_IDS or _MODEL_RE.match(name):
        return name
    return None


def normalize_effort(body: Dict[str, Any]) -> str:
    return effort_with_source(body)[0]


def effort_with_source(body: Dict[str, Any]) -> Tuple[str, str]:
    raw = body.get("reasoning_effort")
    if raw is None:
        reasoning = body.get("reasoning")
        if isinstance(reasoning, dict):
            raw = reasoning.get("effort")
    if raw is None:
        extra = body.get("extra_body")
        if isinstance(extra, dict):
            r = extra.get("reasoning")
            if isinstance(r, dict):
                raw = r.get("effort")
    if raw is None:
        return DEFAULT_EFFORT, "default"
    return EFFORT_MAP.get(str(raw).strip().lower(), DEFAULT_EFFORT), "request"


def content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: List[str] = []
    if isinstance(content, dict):
        content = [content]
    for p in content:
        if isinstance(p, dict):
            t = p.get("type")
            if t == "text":
                parts.append(str(p.get("text", "")))
            elif t in ("image_url", "image", "input_image"):
                parts.append("[image attachment omitted]")
            elif t in ("input_audio", "audio"):
                parts.append("[audio attachment omitted]")
            else:
                parts.append(json.dumps(p, ensure_ascii=False))
        else:
            parts.append(str(p))
    return "\n".join(x for x in parts if x)


def split_system(messages: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
    system_parts: List[str] = []
    rest: List[Dict[str, Any]] = []
    for m in messages:
        if m.get("role") in ("system", "developer"):
            text = content_to_text(m.get("content"))
            if text:
                system_parts.append(text)
        else:
            rest.append(m)
    return "\n\n".join(system_parts), rest


def tool_name(t: Any) -> str:
    fn = t.get("function", t) if isinstance(t, dict) else {}
    return str(fn.get("name") or "") if isinstance(fn, dict) else ""


def tool_name_set(tools: Iterable[Dict[str, Any]]) -> Set[str]:
    return {n for n in (tool_name(t) for t in tools or ()) if n}


def hermes_tools(tools: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Split Hermes tools into (offered, dropped-because-Claude-Code-has-it)."""
    kept, dropped = [], []
    for t in tools or []:
        (dropped if tool_name(t) in DROP_TOOLS else kept).append(t)
    return kept, [tool_name(t) for t in dropped]


def render_tools(tools: List[Dict[str, Any]]) -> str:
    out: List[str] = []
    for t in tools:
        fn = t.get("function", t) if isinstance(t, dict) else {}
        name = fn.get("name") or "?"
        desc = (fn.get("description") or "").strip()
        params = fn.get("parameters") or {"type": "object", "properties": {}}
        out.append(
            f"### {name}\n{desc}\nParameters (JSON Schema): "
            f"{json.dumps(params, ensure_ascii=False, separators=(',', ':'))}"
        )
    return "\n\n".join(out)


def _call_args(c: Dict[str, Any]) -> Any:
    fn = c.get("function", {}) if isinstance(c, dict) else {}
    args = fn.get("arguments", "")
    if isinstance(args, str):
        try:
            return json.loads(args)
        except Exception:
            return args
    return args


def render_transcript(messages: List[Dict[str, Any]]) -> str:
    blocks: List[str] = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            blocks.append(f"<user>\n{content_to_text(m.get('content'))}\n</user>")
        elif role == "assistant":
            text = content_to_text(m.get("content"))
            calls = m.get("tool_calls") or []
            inner = text
            if calls:
                rendered = [
                    json.dumps({"id": c.get("id"), "name": (c.get("function") or {}).get("name"), "arguments": _call_args(c)}, ensure_ascii=False)
                    for c in calls if isinstance(c, dict)
                ]
                inner = (text + "\n" if text else "") + "<tool_calls>\n" + "\n".join(rendered) + "\n</tool_calls>"
            blocks.append(f"<assistant>\n{inner}\n</assistant>")
        elif role == "tool":
            blocks.append(
                f"<tool_result id=\"{m.get('tool_call_id', '')}\" name=\"{m.get('name', '')}\">\n"
                f"{content_to_text(m.get('content'))}\n</tool_result>"
            )
        else:
            blocks.append(f"<{role}>\n{content_to_text(m.get('content'))}\n</{role}>")
    return "\n\n".join(blocks)


def build_prompts(body: Dict[str, Any]) -> Tuple[str, str, str, Optional[Dict[str, Any]]]:
    """Stateless path. Return (system_prompt, user_prompt, mode, schema);
    mode is "json" (response_format json_schema), "json_object" or "text"."""
    messages = body.get("messages") or []
    system_text, rest = split_system(messages)
    mode: str = "text"
    schema: Optional[Dict[str, Any]] = None
    rf = body.get("response_format")
    if isinstance(rf, dict):
        if rf.get("type") == "json_schema":
            js = rf.get("json_schema")
            cand = js.get("schema") if isinstance(js, dict) else None
            if isinstance(cand, dict):
                mode, schema = "json", cand
        elif rf.get("type") == "json_object":
            mode = "json_object"
            system_text = (system_text or "You are a helpful assistant.") + "\n\nRespond with a single JSON object and nothing else."
    if not system_text:
        system_text = "You are a helpful assistant."
    return system_text, TRANSCRIPT_PREAMBLE + render_transcript(rest), mode, schema


def wants_harness(body: Dict[str, Any]) -> bool:
    return bool(body.get("tools")) and body.get("tool_choice", "auto") != "none"


# ── session map ─────────────────────────────────────────────────────


def msg_hash(m: Dict[str, Any]) -> str:
    """Content identity of one OpenAI message, stable across Hermes's
    re-serialisation (tool-call ids and JSON spacing ignored)."""
    calls = [[(c.get("function") or {}).get("name"), _call_args(c)] for c in (m.get("tool_calls") or []) if isinstance(c, dict)]
    payload = [m.get("role"), content_to_text(m.get("content")).strip(), calls, m.get("tool_call_id") or ""]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def session_key(body: Dict[str, Any], system_text: str, rest: List[Dict[str, Any]]) -> str:
    for src in (body, body.get("metadata"), body.get("extra_body")):
        if isinstance(src, dict) and src.get(SESSION_FIELD):
            return str(src[SESSION_FIELD])
    first = next((content_to_text(m.get("content")) for m in rest if m.get("role") == "user"), "")
    return "anon:" + text_hash(system_text + "\x00" + first)


def resume_point(rest: List[Dict[str, Any]], entry: Dict[str, Any]) -> Optional[int]:
    """Index of the first message the Claude Code session has not seen, or None.

    `last` is the reply the session produced, `fed` the last message it was
    given (recorded as soon as a turn starts). The later match wins, so a
    retry of a turn that failed after delivery resumes instead of resending.
    """
    hashes = [msg_hash(m) for m in rest]
    best: Optional[int] = None
    for want, skip_assistant in ((entry.get("last"), False), (entry.get("fed"), True)):
        if not want:
            continue
        for i in range(len(hashes) - 1, -1, -1):
            if hashes[i] == want:
                j = i + 1
                while skip_assistant and j < len(rest) and rest[j].get("role") == "assistant":
                    j += 1
                best = j if best is None else max(best, j)
                break
    return best


def origin_of(o: Any) -> Tuple[str, str, str]:
    o = o if isinstance(o, dict) else {}
    return (str(o.get("platform") or ""), str(o.get("chat_id") or ""), str(o.get("user_id") or ""))


def adopt(rest: List[Dict[str, Any]], key: str, origin: Tuple[str, str, str], store: "SessionStore") -> Optional[Tuple[str, Dict[str, Any], int]]:
    """A recent session of the same person in the same chat, under another key,
    whose last reply is the last assistant message of this history.

    Hermes v0.20.4 gives the profile only the physical session id, which
    rotates on context compression; this keeps the Claude Code session.
    Short replies without tool calls ("Done.") are too common to trust.
    """
    if not all(origin):
        return None
    idx = max((i for i, m in enumerate(rest) if m.get("role") == "assistant"), default=-1)
    if idx < 0:
        return None
    m = rest[idx]
    if not m.get("tool_calls") and len(content_to_text(m.get("content")).strip()) < 24:
        return None
    want = msg_hash(m)
    best: Optional[Tuple[str, Dict[str, Any], int]] = None
    for k, e in store.items():
        if k == key or e.get("fork") or origin_of(e.get("origin")) != origin or e.get("last") != want:
            continue
        if time.time() - float(e.get("updated") or 0) > ADOPT_WINDOW:
            continue
        if best is None or float(e["updated"]) > float(best[1]["updated"]):
            best = (k, e, idx + 1)
    return best


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: Optional[Dict[str, Dict[str, Any]]] = None
        self._turn_locks: Dict[str, threading.Lock] = {}

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if self._data is None:
            try:
                self._data = json.loads(self.path.read_text())
            except Exception:
                self._data = {}
        return self._data

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            e = self._load().get(key)
            return dict(e) if e else None

    def put(self, key: str, entry: Dict[str, Any]) -> None:
        with self._lock:
            data = self._load()
            now = time.time()
            entry["updated"] = now
            data[key] = entry
            for k in [k for k, v in data.items() if now - float(v.get("updated") or 0) > (FORK_TTL if v.get("fork") else SESSION_TTL)]:
                del data[k]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1))
            os.replace(tmp, self.path)

    def items(self) -> List[Tuple[str, Dict[str, Any]]]:
        with self._lock:
            return [(k, dict(v)) for k, v in self._load().items()]

    def drop(self, key: str) -> None:
        with self._lock:
            self._load().pop(key, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._load())

    def turn_lock(self, key: str) -> threading.Lock:
        with self._lock:
            return self._turn_locks.setdefault(key, threading.Lock())


STORE = SessionStore(SESSIONS_FILE)


# ── turn content (stream-json input) ────────────────────────────────


def image_block(part: Dict[str, Any]) -> Dict[str, Any]:
    if part.get("type") == "image" and isinstance(part.get("source"), dict):
        return part
    url = part.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    url = str(url or part.get("url") or "")
    m = _DATA_URL.match(url)
    if m and m.group(1).lower() in _IMAGE_TYPES:
        return {"type": "image", "source": {"type": "base64", "media_type": m.group(1).lower(), "data": m.group(2).strip()}}
    if m:
        return {"type": "text", "text": f"[attachment of type {m.group(1)} not passed through: only png, jpeg, gif and webp images are supported]"}
    return {"type": "text", "text": f"[remote image not attached: {url[:300]} — only inline data: images reach you; fetch it yourself if you need it]"}


def content_blocks(content: Any) -> List[Dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, dict):
        content = [content]
    out: List[Dict[str, Any]] = []
    for p in content:
        if isinstance(p, dict) and p.get("type") in ("image_url", "image", "input_image"):
            out.append(image_block(p))
        elif isinstance(p, dict) and p.get("type") == "text":
            if p.get("text"):
                out.append({"type": "text", "text": str(p["text"])})
        else:
            t = content_to_text([p])
            if t:
                out.append({"type": "text", "text": t})
    return out


def bootstrap_transcript(history: List[Dict[str, Any]], limit: Optional[int] = None) -> str:
    """The most recent messages that fit in `limit` characters, oldest first."""
    limit = BOOTSTRAP_MAX_CHARS if limit is None else limit
    kept: List[str] = []
    size = 0
    for m in reversed(history):
        part = render_transcript([m])
        if kept and size + len(part) > limit:
            break
        kept.append(part)
        size += len(part) + 2
    kept.reverse()
    text = "\n\n".join(kept)
    if len(text) > limit:
        text = "…" + text[-limit:]
    omitted = len(history) - len(kept)
    return (f"({omitted} older message(s) omitted; this is the recent part only.)\n\n" if omitted else "") + text


def turn_blocks(delta: List[Dict[str, Any]], history: List[Dict[str, Any]], names: Dict[str, str]) -> List[Dict[str, Any]]:
    """One user turn for the Claude Code session: optional bootstrap history,
    then Hermes tool results and new user input, images as image blocks."""
    blocks: List[Dict[str, Any]] = []
    if history:
        blocks.append({"type": "text", "text": BOOTSTRAP_PREAMBLE + bootstrap_transcript(history) + "\n\nThe newest input follows."})
    if any(m.get("role") == "tool" for m in delta):
        blocks.append({"type": "text", "text": "Results of the Hermes tool calls you requested:"})
    for m in delta:
        role = m.get("role")
        if role == "tool":
            cid = str(m.get("tool_call_id") or "")
            name = str(m.get("name") or names.get(cid, ""))
            inner = content_blocks(m.get("content"))
            text = "\n".join(b["text"] for b in inner if b["type"] == "text")
            blocks.append({"type": "text", "text": f"<tool_result tool_call_id=\"{cid}\" name=\"{name}\">\n{text}\n</tool_result>"})
            blocks.extend(b for b in inner if b["type"] == "image")
        elif role == "user":
            blocks.extend(content_blocks(m.get("content")))
        else:
            blocks.append({"type": "text", "text": "(Turn recorded by Hermes outside this session)\n" + render_transcript([m])})
    if not blocks:
        blocks.append({"type": "text", "text": RESEND_NUDGE})
    return blocks


def stream_json_line(blocks: List[Dict[str, Any]]) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": blocks}}, ensure_ascii=False) + "\n"


# ── approvals (Claude Code permission requests relayed to the chat) ─


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text).split()) if "\n" not in str(text) else str(text).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def admins() -> Set[str]:
    return _env_list("CLAUDE_BRIDGE_LOGIN_USERS")


def describe_request(req: Dict[str, Any]) -> str:
    """Chat text for one can_use_tool request."""
    inp = req.get("input") if isinstance(req.get("input"), dict) else {}
    tool = str(req.get("display_name") or req.get("tool_name") or "a tool")
    if req.get("tool_name") == "AskUserQuestion":
        out = ["Claude Code asks:"]
        qs = [q for q in inp.get("questions") or [] if isinstance(q, dict)]
        for n, q in enumerate(qs, 1):
            head = f"{n}. " if len(qs) > 1 else ""
            out.append(f"{head}{q.get('question', '')}")
            for i, o in enumerate(q.get("options") or [], 1):
                if isinstance(o, dict):
                    desc = f" — {o.get('description')}" if o.get("description") else ""
                    out.append(f"   {i}) {o.get('label', '')}{desc}")
        out.append("Reply with an option number, or type your answer.")
        return "\n".join(out)
    detail = ""
    for k in ("command", "file_path", "notebook_path", "url", "query", "pattern", "path", "prompt"):
        if inp.get(k):
            detail = str(inp[k])
            break
    if not detail and inp:
        detail = json.dumps(inp, ensure_ascii=False, separators=(",", ":"))
    out = [f"Claude Code wants to use {tool}:"]
    if detail:
        out.append(_clip(detail, 500))
    note = req.get("description") or inp.get("description")
    if note and str(note) != detail:
        out.append(f"({_clip(note, 200)})")
    if req.get("blocked_path") and str(req["blocked_path"]) not in detail:
        out.append(f"Path: {req['blocked_path']}")
    out.append("Reply y / n, or give a reason to deny.")
    return "\n".join(out)


def decide(req: Dict[str, Any], reply: str) -> Dict[str, Any]:
    """Control-protocol decision for a user's chat reply (exact match after strip + lowercase)."""
    inp = req.get("input") if isinstance(req.get("input"), dict) else {}
    text = (reply or "").strip()
    if req.get("tool_name") == "AskUserQuestion":
        qs = [q for q in inp.get("questions") or [] if isinstance(q, dict)]
        answers: Dict[str, Any] = {}
        for q in qs:
            value: Any = text
            opts = [o for o in q.get("options") or [] if isinstance(o, dict)]
            if len(qs) == 1 and re.fullmatch(r"\d+(\s*,\s*\d+)*", text):
                picked = [opts[int(i) - 1]["label"] for i in re.findall(r"\d+", text) if 0 < int(i) <= len(opts)]
                if picked:
                    value = ", ".join(picked)
            answers[str(q.get("question", ""))] = value
        return {"behavior": "allow", "updatedInput": {**inp, "answers": answers}}
    low = text.lower()
    if low in ("y", "yes"):
        return {"behavior": "allow", "updatedInput": inp}
    if low in ("n", "no"):
        return {"behavior": "deny", "message": "The user denied this action."}
    return {"behavior": "deny", "message": text}


def deny(message: str) -> Dict[str, Any]:
    return {"behavior": "deny", "message": message}


class Approvals:
    """Pending permission requests, long-polled by the Hermes plugin and
    answered by the person who owns the turn (or a login admin)."""

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.items: Dict[str, Dict[str, Any]] = {}
        self.seq = 0
        self.counter = 0
        self.last_poll = 0.0
        self.polling = 0

    def _set(self, item: Dict[str, Any], state: str) -> None:
        self.seq += 1
        item["seq"], item["state"], item["closed"] = self.seq, state, (time.time() if state != "pending" else None)
        self.cond.notify_all()

    def channel_alive(self) -> bool:
        return bool(CONTROL["token"]) and (self.polling > 0 or time.time() - self.last_poll < POLL_STALE)

    def notice(self, origin: Dict[str, Any], text: str) -> bool:
        """A one-way message for the chat that owns a session."""
        if not (origin.get("platform") and origin.get("chat_id")) or not CONTROL["token"]:
            return False
        with self.cond:
            nid = "n" + uuid.uuid4().hex[:12]
            item = {"id": nid, "short": "", "tool": "", "req": {}, "text": text, "answer": None,
                    "platform": str(origin.get("platform") or ""), "chat_id": str(origin.get("chat_id") or ""),
                    "thread_id": str(origin.get("thread_id") or ""), "user_id": str(origin.get("user_id") or "")}
            self.items[nid] = item
            self._set(item, "notice")
            item["closed"] = time.time()
            self._prune()
        return True

    def ask(self, rid: str, req: Dict[str, Any], origin: Dict[str, Any], timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        tool = str(req.get("tool_name") or "?")
        if not (origin.get("platform") and origin.get("chat_id")) or not self.channel_alive():
            log.info("approval %s -> deny (no approval channel)", tool)
            if tool == "AskUserQuestion":
                return deny("no approval channel: nobody can answer here. Ask the question in your reply instead.")
            return deny("no approval channel")
        self.counter += 1
        short = str(self.counter)
        item = {
            "id": rid, "short": short, "tool": tool, "req": req, "text": f"[{short}] " + describe_request(req), "answer": None,
            "platform": str(origin.get("platform") or ""), "chat_id": str(origin.get("chat_id") or ""),
            "thread_id": str(origin.get("thread_id") or ""), "user_id": str(origin.get("user_id") or ""),
        }
        end = time.monotonic() + (APPROVAL_TIMEOUT if timeout is None else timeout)
        with self.cond:
            self.items[rid] = item
            self._set(item, "pending")
            while item["state"] == "pending":
                left = end - time.monotonic()
                if left <= 0:
                    self._set(item, "timeout")
                    break
                self.cond.wait(min(left, 5.0))
            state = item["state"]
            self._prune()
        if state == "cancelled":
            log.info("approval %s -> cancelled by claude", tool)
            return None
        if state == "timeout":
            log.info("approval %s -> deny (timeout)", tool)
            return deny("no answer from user")
        decision = decide(req, item["answer"])
        log.info("approval %s -> %s", tool, decision["behavior"])
        return decision

    def answer(self, rid: str, user_id: str, platform: str, text: str) -> Tuple[int, Dict[str, Any]]:
        with self.cond:
            item = self.items.get(rid)
            if not item or item["state"] != "pending":
                return 404, {"ok": False, "error": "no pending approval with that id"}
            uid = str(user_id or "")
            if not uid or (uid != item["user_id"] and uid not in admins() and f"{platform}:{uid}" not in admins()):
                return 403, {"ok": False, "error": "only the person who sent this turn (or an admin) can answer"}
            item["answer"] = text
            self._set(item, "answered")
            return 200, {"ok": True, "decision": decide(item["req"], text)["behavior"]}

    def cancel(self, rid: str) -> None:
        with self.cond:
            item = self.items.get(rid)
            if item and item["state"] == "pending":
                self._set(item, "cancelled")

    def poll(self, after: int, wait: float) -> Tuple[int, List[Dict[str, Any]]]:
        keys = ("id", "short", "seq", "state", "tool", "text", "platform", "chat_id", "thread_id", "user_id")
        end = time.monotonic() + max(0.0, min(wait, 60.0))
        with self.cond:
            self.polling += 1
            try:
                while True:
                    out = [{k: i[k] for k in keys} for i in self.items.values() if i["seq"] > after]
                    left = end - time.monotonic()
                    if out or left <= 0:
                        break
                    self.cond.wait(left)
            finally:
                self.polling -= 1
                self.last_poll = time.time()
            return self.seq, sorted(out, key=lambda i: i["seq"])

    def _prune(self) -> None:
        now = time.time()
        for k in [k for k, i in self.items.items() if i.get("closed") and now - i["closed"] > 900]:
            del self.items[k]


APPROVALS = Approvals()


# ── claude subprocess ───────────────────────────────────────────────


class BridgeError(Exception):
    def __init__(self, status: int, message: str, code: str = "bridge_error", etype: str = "bridge_error"):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.etype = etype


class ClientGone(Exception):
    pass


_LIMIT_RE = re.compile(r"limit", re.I)
_LIMIT_HINT_RE = re.compile(r"usage|reset|hit your|reached|exceed|out of", re.I)
_NO_SESSION_RE = re.compile(r"no conversation found|session .*not found", re.I)


def classify_error(text: str) -> BridgeError:
    """Map claude -p failure text to an HTTP error.

    The usage-limit message must avoid the words Hermes's auxiliary router
    treats as billing exhaustion (credits, quota, billing, funds, payment,
    afford): those make it silently retry on the next provider.
    """
    t = (text or "").strip() or "claude returned an error with no message"
    if "not logged in" in t.lower() or "/login" in t.lower():
        return BridgeError(
            401,
            "Claude Code is not logged in — run /login in chat (or `claude auth login` in a terminal), then retry.",
            code="claude_not_logged_in",
            etype="authentication_error",
        )
    if _LIMIT_RE.search(t) and _LIMIT_HINT_RE.search(t):
        return BridgeError(
            429,
            f"Claude usage limit reached — {t}. No fallback is configured on purpose; wait for the window to reset.",
            code="claude_usage_limit",
            etype="rate_limit_error",
        )
    if _NO_SESSION_RE.search(t):
        return BridgeError(409, f"claude session missing: {t}", code="claude_session_missing", etype="upstream_error")
    return BridgeError(502, f"claude -p failed: {t}", code="claude_error", etype="upstream_error")


class ClaudeRun:
    """Everything one `claude -p` spawn produced, as seen on its event stream."""

    def __init__(self) -> None:
        self.result_text: str = ""
        self.structured: Any = None
        self.results: List[Tuple[str, Any]] = []         # (result, structured_output) of every turn in the spawn
        self.saw_result: bool = False
        self.is_error: bool = False
        self.subtype: str = ""
        self.num_turns: int = 0
        self.session_id: str = ""
        self.permission_mode: str = ""
        self.model_ids: List[str] = []
        self.api_key_source: str = ""
        self.rate_limit: Optional[Dict[str, Any]] = None   # unifiedWindows from the last rate_limit_event
        self.usage_last: Optional[Dict[str, Any]] = None   # usage of the LAST API call = real context size
        self.usage_total: Optional[Dict[str, Any]] = None  # summed over the spawn's API calls
        self.native_tools: List[str] = []                  # tool_use names other than StructuredOutput
        self.misrouted: List[str] = []                     # Hermes tools the model tried to call natively
        self.unknown_tool_errors: int = 0
        self.permission_denials: int = 0
        self.approvals: int = 0
        self.background: Set[str] = set()                  # live non-ambient background task ids
        self.waited_background: bool = False
        self.text_streamed: str = ""
        self.final_text: str = ""


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _close_pipes(proc: subprocess.Popen) -> None:
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        try:
            if pipe is not None:
                pipe.close()
        except Exception:
            pass


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _fake_error() -> None:
    if FAKE_ERROR == "limit":
        raise classify_error("Claude usage limit reached. Your limit will reset at 3pm (America/New_York).")
    if FAKE_ERROR == "login":
        raise classify_error("Not logged in · Please run /login")
    if FAKE_ERROR == "crash":
        raise BridgeError(502, "claude -p failed: simulated crash", code="claude_error", etype="upstream_error")


class Background:
    """A detached claude child still running background tasks after its turn."""

    def __init__(self, proc: subprocess.Popen, close_stdin: Callable[[], None]) -> None:
        self.proc, self.close_stdin = proc, close_stdin
        self.done = threading.Event()

    def stop(self) -> None:
        self.close_stdin()
        if self.done.wait(10):
            return
        try:
            self.proc.send_signal(signal.SIGINT)
        except Exception:
            pass
        if not self.done.wait(5):
            _terminate(self.proc)
            self.done.wait(5)


BACKGROUND: Dict[str, Background] = {}


def stop_background(session: str, origin: Optional[Dict[str, Any]] = None) -> None:
    bg = BACKGROUND.get(session)
    if bg is None or bg.done.is_set() or bg.done.wait(10):
        return
    log.info("stopping background work of session %s before the next turn", session[:8])
    bg.stop()
    APPROVALS.notice(origin or {}, "[Background follow-up] The background work from the previous turn was stopped so your new message could go through.")


def spawn_claude(
    cmd: List[str],
    stdin_text: str,
    cwd: Path,
    hermes_names: Optional[Set[str]] = None,
    on_text: Optional[Callable[[str], None]] = None,
    on_tick: Optional[Callable[[], None]] = None,
    on_event: Optional[Callable[[str], None]] = None,
    on_permission: Optional[Callable[[str, Dict[str, Any]], Optional[Dict[str, Any]]]] = None,
    on_cancel: Optional[Callable[[str], None]] = None,
    on_init: Optional[Callable[[str], None]] = None,
    on_followup: Optional[Callable[[ClaudeRun], None]] = None,
    env: Optional[Dict[str, str]] = None,
) -> ClaudeRun:
    """Run one `claude -p` and consume its stream-json events.

    on_text(delta) gets text deltas; on_tick() runs on every event and at
    least once a second (it may raise ClientGone to abort the child);
    on_event(raw_line) sees every stdout line; on_init(session_id) fires on
    the init event. With on_permission, stdin stays open as the control
    channel and each can_use_tool request is answered by
    on_permission(request_id, request) on a worker thread. With on_followup,
    a turn that leaves background tasks running returns at once and the
    child keeps going detached; each later turn's result goes to
    on_followup(run_of_that_turn).
    """
    _fake_error()
    hermes_names = hermes_names or set()
    keep_open = on_permission is not None
    cwd.mkdir(parents=True, exist_ok=True)
    run = ClaudeRun()
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd),
            env=env if env is not None else child_env(),
        )
    except FileNotFoundError:
        raise BridgeError(500, f"claude binary not found at {CLAUDE_BIN}", code="claude_missing")

    lines: "queue.Queue[Optional[str]]" = queue.Queue()
    stderr_buf: List[str] = []
    other_lines: List[str] = []
    stdin_lock = threading.Lock()
    stdin_state = {"closed": False}
    open_requests: Set[str] = set()

    def send(obj: Dict[str, Any]) -> None:
        with stdin_lock:
            if stdin_state["closed"]:
                return
            try:
                assert proc.stdin is not None
                proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass

    def close_stdin() -> None:
        with stdin_lock:
            if not stdin_state["closed"]:
                stdin_state["closed"] = True
                try:
                    assert proc.stdin is not None
                    proc.stdin.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass

    def feed() -> None:
        with stdin_lock:
            try:
                assert proc.stdin is not None
                proc.stdin.write(stdin_text)
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
        if not keep_open:
            close_stdin()

    def answer(rid: str, req: Dict[str, Any]) -> None:
        try:
            decision = on_permission(rid, req) if on_permission else None
            if decision is None:  # cancelled by claude: it no longer wants a response
                return
            resp: Dict[str, Any] = {"subtype": "success", "request_id": rid, "response": decision}
        except Exception as exc:
            resp = {"subtype": "error", "request_id": rid, "error": str(exc)}
        finally:
            open_requests.discard(rid)
        send({"type": "control_response", "response": resp})

    def read_out() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.put(line)
        except Exception:
            pass
        lines.put(None)

    def read_err() -> None:
        try:
            assert proc.stderr is not None
            stderr_buf.append(proc.stderr.read() or "")
        except Exception:
            pass

    for target in (feed, read_out, read_err):
        threading.Thread(target=target, daemon=True).start()

    st = {"deadline": time.monotonic() + SPAWN_TIMEOUT, "idle": None, "first": None, "bg_live": False, "cur": "", "seen": set(), "results_seen": 0}

    def maybe_close(now: float) -> None:
        if not keep_open or stdin_state["closed"] or st["idle"] is None:
            return
        if run.background and now - st["first"] < BG_WAIT:
            st["bg_live"] = run.waited_background = True
            return
        if st["bg_live"] and not run.background and now - st["idle"] < BG_GRACE:
            return  # a finished background task usually triggers one more turn
        close_stdin()

    def handle(ev: Dict[str, Any], stripped: str) -> bool:
        """Apply one event to `run`; True when it was a turn result."""
        etype = ev.get("type")
        if etype == "control_request":
            rid = str(ev.get("request_id") or "")
            req = ev.get("request") or {}
            if req.get("subtype") == "can_use_tool" and on_permission:
                run.approvals += 1
                open_requests.add(rid)
                threading.Thread(target=answer, args=(rid, req), daemon=True).start()
            else:
                send({"type": "control_response", "response": {"subtype": "error", "request_id": rid, "error": f"unsupported control request: {req.get('subtype')}"}})
            return False
        if etype == "control_cancel_request":
            rid = str(ev.get("request_id") or "")
            if on_cancel and rid in open_requests:
                on_cancel(rid)
            return False
        if etype == "system" and ev.get("subtype") in ("task_started", "task_notification", "background_tasks_changed"):
            sub = ev.get("subtype")
            if sub == "background_tasks_changed":
                run.background = {str(t.get("task_id")) for t in ev.get("tasks") or [] if isinstance(t, dict) and not t.get("ambient")}
            elif sub == "task_started" and ev.get("is_backgrounded") and not ev.get("ambient"):
                run.background.add(str(ev.get("task_id")))
            elif sub == "task_notification":
                run.background.discard(str(ev.get("task_id")))
            maybe_close(time.monotonic())
            return False
        if ev.get("parent_tool_use_id") and etype in ("stream_event", "assistant", "user"):
            return False  # subagent traffic: not the main thread's context or reply
        if etype in ("stream_event", "assistant") and st["idle"] is not None:
            st["idle"], st["bg_live"] = None, False  # a new turn (e.g. a task notification) started
        if etype == "system":
            if ev.get("subtype") == "init":
                first_init = not run.session_id
                if ev.get("model"):
                    run.model_ids = [str(ev["model"])]
                run.api_key_source = str(ev.get("apiKeySource") or "")
                run.permission_mode = str(ev.get("permissionMode") or "")
                run.session_id = str(ev.get("session_id") or run.session_id)
                if first_init and on_init and run.session_id:
                    on_init(run.session_id)
        elif etype == "rate_limit_event":
            info = ev.get("rate_limit_info") or {}
            if isinstance(info, dict) and isinstance(info.get("unifiedWindows"), dict):
                run.rate_limit = info["unifiedWindows"]
        elif etype == "stream_event":
            e = ev.get("event") or {}
            sub = e.get("type")
            if sub == "message_start":
                st["cur"] = ""
            elif sub == "content_block_delta":
                d = e.get("delta") or {}
                if d.get("type") == "text_delta":
                    piece = str(d.get("text") or "")
                    st["cur"] += piece
                    if on_text and piece:
                        on_text(piece)
                        run.text_streamed += piece
            elif sub == "message_delta":
                u = e.get("usage")
                if isinstance(u, dict):
                    run.usage_last = u
            elif sub == "message_stop":
                run.final_text = st["cur"]
        elif etype == "assistant":
            msg = ev.get("message") or {}
            u = msg.get("usage")
            if isinstance(u, dict) and run.usage_last is None:
                run.usage_last = u
            for block in msg.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = str(block.get("name") or "")
                bid = str(block.get("id") or "")
                if name == STRUCTURED_TOOL or (bid and bid in st["seen"]):
                    continue
                st["seen"].add(bid)
                run.native_tools.append(name)
                if name in hermes_names:
                    run.misrouted.append(name)
        elif etype == "user":
            if "No such tool available" in stripped:
                run.unknown_tool_errors += 1
        elif etype == "result" or ("result" in ev and "usage" in ev and "duration_api_ms" in ev):
            run.saw_result = True
            run.result_text = str(ev.get("result") or "")
            run.structured = ev.get("structured_output")
            run.results.append((run.result_text, run.structured))
            run.is_error = bool(ev.get("is_error"))
            run.subtype = str(ev.get("subtype") or "")
            run.session_id = str(ev.get("session_id") or run.session_id)
            run.permission_denials += len(ev.get("permission_denials") or [])
            try:
                run.num_turns += int(ev.get("num_turns") or 0)
            except Exception:
                pass
            if isinstance(ev.get("usage"), dict):
                run.usage_total = ev["usage"]
            mu = ev.get("modelUsage")
            if isinstance(mu, dict) and mu:
                run.model_ids = list(mu.keys())
            now = time.monotonic()
            st["idle"] = now
            if st["first"] is None:
                st["first"] = now
            if run.is_error:
                close_stdin()
            maybe_close(now)
            return True
        return False

    def pump(detached: bool) -> str:
        """Read events until EOF ("eof") or, in the request thread, until a
        result leaves background tasks running ("detach")."""
        while True:
            try:
                line = lines.get(timeout=1.0)
            except queue.Empty:
                now = time.monotonic()
                if now > st["deadline"]:
                    if run.saw_result and keep_open and not stdin_state["closed"]:
                        log.warning("background work still running at the time limit; ending the spawn")
                        close_stdin()
                        st["deadline"] = now + 30
                        continue
                    if detached:
                        _terminate(proc)
                        return "eof"
                    raise BridgeError(504, f"claude -p exceeded {int(SPAWN_TIMEOUT)}s", code="claude_timeout", etype="timeout_error")
                maybe_close(now)
                if on_tick and not detached:
                    on_tick()
                continue
            if line is None:
                return "eof"
            if on_event:
                on_event(line)
            if on_tick and not detached:
                on_tick()
            stripped = line.strip()
            if not stripped.startswith("{"):
                if stripped:
                    other_lines.append(stripped)
                continue
            try:
                ev = json.loads(stripped)
            except Exception:
                other_lines.append(stripped)
                continue
            if handle(ev, stripped):
                if detached and on_followup and not run.is_error:
                    one = ClaudeRun()
                    one.result_text, one.structured = run.result_text, run.structured
                    one.results = [(run.result_text, run.structured)]
                    try:
                        on_followup(one)
                    except Exception:
                        log.exception("background follow-up delivery failed")
                elif not detached and on_followup and run.background and not stdin_state["closed"] and not run.is_error:
                    return "detach"

    def finish() -> None:
        close_stdin()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            _terminate(proc)
        _close_pipes(proc)

    outcome = "eof"
    try:
        outcome = pump(False)
    except BaseException:
        for rid in list(open_requests):
            if on_cancel:
                on_cancel(rid)
        _terminate(proc)
        _close_pipes(proc)
        raise

    if outcome == "detach":
        snapshot = copy.copy(run)
        snapshot.results = list(run.results)
        st["deadline"] = time.monotonic() + BG_WAIT
        bg = Background(proc, close_stdin)
        if run.session_id:
            BACKGROUND[run.session_id] = bg

        def rest() -> None:
            try:
                pump(True)
            finally:
                for rid in list(open_requests):
                    if on_cancel:
                        on_cancel(rid)
                finish()
                bg.done.set()
                if BACKGROUND.get(run.session_id) is bg:
                    BACKGROUND.pop(run.session_id, None)
                log.info("background work of session %s finished", run.session_id[:8])

        threading.Thread(target=rest, name="claude-background", daemon=True).start()
        log.info("session %s left %d background task(s) running; replying now", run.session_id[:8], len(run.background))
        return snapshot

    finish()
    if run.api_key_source and run.api_key_source != "none":
        log.warning("claude reports apiKeySource=%s — this spawn may be billing an API key, not the subscription", run.api_key_source)

    stderr_text = "".join(stderr_buf).strip()
    if run.saw_result:
        if run.is_error:
            raise classify_error(run.result_text or stderr_text or f"claude exited {proc.returncode} ({run.subtype})")
        if proc.returncode not in (0, None):
            log.warning("claude exited %s after a successful result event; stderr tail: %s", proc.returncode, stderr_text[-300:])
        return run
    tail = (stderr_text + "\n" + "\n".join(other_lines[-10:])).strip()[-800:]
    raise classify_error(tail or f"exit {proc.returncode} with no result event")


def run_claude(
    model: str,
    effort: str,
    system_prompt: str,
    user_prompt: str,
    schema: Optional[Dict[str, Any]] = None,
    on_text: Optional[Callable[[str], None]] = None,
    on_tick: Optional[Callable[[], None]] = None,
    on_event: Optional[Callable[[str], None]] = None,
) -> ClaudeRun:
    """Stateless, tools-off spawn for requests without Hermes tools."""
    _fake_error()
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", prefix="sys-", dir=str(SCRATCH_DIR), delete=False) as f:
        f.write(system_prompt)
        sys_path = f.name
    cmd = [
        CLAUDE_BIN, "-p",
        "--safe-mode",
        "--tools", "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--model", model,
        "--effort", effort,
        "--system-prompt-file", sys_path,
    ]
    if schema:
        cmd += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
    try:
        return spawn_claude(cmd, user_prompt, SCRATCH_DIR, on_text=on_text, on_tick=on_tick, on_event=on_event)
    finally:
        _unlink(sys_path)


def harness_cmd(model: str, effort: str, sys_path: str, session_uuid: str, mode: str, snapshot_off: bool) -> List[str]:
    """mode: "new" (--session-id), "resume" or "fork" (--resume --fork-session)."""
    cmd = [
        CLAUDE_BIN, "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--permission-mode", PERMISSION_MODE,
        "--permission-prompt-tool", "stdio",
        "--model", model,
        "--effort", effort,
        "--append-system-prompt-file", sys_path,
        "--json-schema", json.dumps(OUTPUT_SCHEMA, separators=(",", ":")),
    ]
    if mode == "new":
        cmd += ["--session-id", session_uuid]
    else:
        cmd += ["--resume", session_uuid]
        if mode == "fork":
            cmd.append("--fork-session")
    if snapshot_off:
        cmd += ["--system-prompt-snapshot", "off"]
    return cmd


class Turn:
    """What one harness request did, for logging and the reply."""

    def __init__(self) -> None:
        self.key = ""
        self.session = ""
        self.mode = "new"
        self.delta = 0
        self.dropped: List[str] = []
        self.offered = 0
        self.run: Optional[ClaudeRun] = None

    @property
    def resumed(self) -> bool:
        return self.mode != "new"


def harness_turn(
    body: Dict[str, Any],
    model: str,
    effort: str,
    on_tick: Optional[Callable[[], None]] = None,
    on_event: Optional[Callable[[str], None]] = None,
    store: Optional[SessionStore] = None,
) -> Tuple[Turn, str, List[Dict[str, Any]]]:
    """Run one Hermes request as a turn of its persistent Claude Code session.

    A Hermes fork (background review) runs in a fork of the user's session
    under its own key and never touches the user's map entry.
    """
    store = store or STORE
    _fake_error()
    system_text, rest = split_system(body.get("messages") or [])
    offered, dropped = hermes_tools(body.get("tools") or [])
    names = tool_name_set(offered)
    note = f"Earlier instructions from Hermes may name {', '.join(dropped)}. Those Hermes tools are not\navailable here; use your own equivalents.\n\n" if dropped else ""
    sys_prompt = (system_text or "").strip() + HARNESS_HEADER.replace("<<DROPPED>>", note) + (render_tools(offered) if offered else "(none in this conversation)")
    tc = body.get("tool_choice")
    if tc in ("required", "any") or isinstance(tc, dict):
        sys_prompt += "\n\nFor this turn you must request at least one Hermes tool."
    sp = text_hash(sys_prompt)
    fork = body.get(FORK_FIELD) if isinstance(body.get(FORK_FIELD), dict) else None
    origin = body.get(ORIGIN_FIELD) if isinstance(body.get(ORIGIN_FIELD), dict) and not fork else {}
    who = origin_of(origin)
    t = Turn()
    t.key, t.dropped, t.offered = session_key(body, system_text, rest), dropped, len(offered)
    if fork and not t.key.startswith("fork:"):
        t.key = "fork:" + t.key
    call_names = {str(c.get("id")): str((c.get("function") or {}).get("name") or "") for m in rest for c in (m.get("tool_calls") or []) if isinstance(c, dict)}
    fed = msg_hash(rest[-1]) if rest else ""

    lock = store.turn_lock(t.key)
    if not lock.acquire(timeout=QUEUE_WAIT):
        raise BridgeError(503, "another turn of this conversation is still running", code="session_busy", etype="overloaded_error")
    try:
        entry = store.get(t.key)
        start = resume_point(rest, entry) if entry else None
        mode, adopted_from = "resume", ""
        if start is None and fork and fork.get("parent"):
            parent = store.get(str(fork["parent"]))
            pstart = resume_point(rest, parent) if parent else None
            if pstart is not None:
                entry, start, mode = parent, pstart, "fork"
        if start is None and not fork:
            found = adopt(rest, t.key, who, store)
            if found:
                log.info("conversation %s continues Claude Code session %s from %s", t.key[:24], found[1]["uuid"][:8], found[0][:24])
                adopted_from, entry, start = found
        for attempt in (0, 1):
            if start is not None and entry:
                delta, history, target = rest[start:], [], str(entry["uuid"])
                snapshot_off = bool(entry.get("sp_off")) or entry.get("sp") != sp
            else:
                mode, entry = "new", None
                last_asst = max((i for i, m in enumerate(rest) if m.get("role") == "assistant"), default=-1)
                delta, history, target = rest[last_asst + 1:], rest[:last_asst + 1], str(uuid.uuid4())
                snapshot_off = False
            t.mode, t.session, t.delta = mode, target, len(delta)
            snapshot_off = snapshot_off and claude_supports("--system-prompt-snapshot")
            base = {
                "origin": dict(zip(("platform", "chat_id", "user_id"), who)) if all(who) else {},
                "fork": bool(fork),
                "sp": entry.get("sp") if entry else sp,
                "sp_off": bool(entry and (entry.get("sp_off") or entry.get("sp") != sp)),
                "created": entry.get("created") if (entry and mode == "resume") else time.time(),
                "turns": int(entry.get("turns") or 0) if (entry and mode == "resume") else 0,
            }
            prior_last = entry.get("last", "") if (entry and mode == "resume") else ""
            ulock = store.turn_lock("uuid:" + target)
            if not ulock.acquire(timeout=QUEUE_WAIT):
                raise BridgeError(503, "this Claude Code session is busy in another conversation", code="session_busy", etype="overloaded_error")
            held = [True]

            def release_ulock() -> None:
                if held[0]:
                    held[0] = False
                    ulock.release()

            def on_init(sid: str) -> None:
                t.session = sid
                store.put(t.key, {**base, "uuid": sid, "fed": fed, "last": prior_last})
                if mode == "fork":
                    release_ulock()  # the parent was only read to start the fork

            def on_followup(r: ClaudeRun) -> None:
                content, calls = run_to_message(r, "tools")
                if calls:
                    content += "\n(Requested Hermes tools were not run: " + ", ".join(c["function"]["name"] for c in calls) + ")"
                if content.strip() and not APPROVALS.notice(origin, "[Background follow-up] " + content.strip()):
                    log.info("background follow-up for %s dropped: no chat to deliver it to", t.key[:24])

            stdin_text = stream_json_line(turn_blocks(delta, history, call_names))
            SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", suffix=".txt", prefix="append-", dir=str(SCRATCH_DIR), delete=False) as f:
                f.write(sys_prompt)
                sys_path = f.name
            dump(f"{target[:8]}-{int(time.time())}-turn.jsonl", stdin_text)
            try:
                if mode == "resume":
                    stop_background(target, origin)
                with _slot():
                    t.run = spawn_claude(
                        harness_cmd(model, effort, sys_path, target, mode, snapshot_off), stdin_text, WORKDIR,
                        hermes_names=names, on_tick=on_tick, on_event=on_event,
                        on_permission=lambda rid, req: APPROVALS.ask(rid, req, origin), on_cancel=APPROVALS.cancel,
                        on_init=on_init, on_followup=on_followup, env=harness_env(),
                    )
                break
            except BridgeError as err:
                if err.code == "claude_session_missing" and mode != "new" and attempt == 0:
                    log.warning("session %s for %s is gone from Claude Code; starting a new one from the Hermes history", target[:8], t.key)
                    start, entry = None, None
                    continue
                raise
            finally:
                release_ulock()
                _unlink(sys_path)
        run = t.run
        assert run is not None
        content, tool_calls = run_to_message(run, "tools")
        produced = {"role": "assistant", "content": content, "tool_calls": tool_calls}
        t.session = run.session_id or t.session
        store.put(t.key, {**base, "uuid": t.session, "last": msg_hash(produced), "fed": fed, "turns": base["turns"] + 1})
        if adopted_from and t.resumed:
            store.drop(adopted_from)
        if run.permission_mode and run.permission_mode != PERMISSION_MODE:
            log.warning("claude reports permissionMode=%s, asked for %s", run.permission_mode, PERMISSION_MODE)
        if run.misrouted or run.unknown_tool_errors:
            log.warning("model called Hermes tool(s) natively %s; Claude Code rejected %d call(s)", run.misrouted, run.unknown_tool_errors)
        return t, content, tool_calls
    finally:
        lock.release()


class _slot:
    def __enter__(self):
        if not _slots.acquire(timeout=QUEUE_WAIT):
            raise BridgeError(503, "bridge busy: too many concurrent claude runs", code="busy", etype="overloaded_error")

    def __exit__(self, *exc):
        _slots.release()


def run_to_message(run: ClaudeRun, mode: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Return (content, openai_tool_calls) for the given mode."""
    structured = run.structured
    if isinstance(structured, str):
        try:
            structured = json.loads(structured)
        except Exception:
            structured = None
    result = run.result_text
    if mode == "tools" and len(run.results) > 1:
        texts, calls = [], []
        for res, st in run.results:
            one = ClaudeRun()
            one.result_text, one.structured = res, st
            c, tc = run_to_message(one, "tools")
            if c:
                texts.append(c)
            calls += tc
        return "\n\n".join(texts), calls
    if mode == "tools":
        if not isinstance(structured, dict) and result:
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict) and "content" in parsed:
                    structured = parsed
            except Exception:
                pass
        if isinstance(structured, dict):
            content = str(structured.get("content") or "")
            calls_out = []
            for c in structured.get("tool_calls") or []:
                if not isinstance(c, dict) or not c.get("name"):
                    continue
                args = c.get("arguments")
                if not isinstance(args, dict):
                    args = {}
                calls_out.append({
                    "id": "call_" + uuid.uuid4().hex[:24],
                    "type": "function",
                    "function": {"name": str(c["name"]), "arguments": json.dumps(args, ensure_ascii=False)},
                })
            return content, calls_out
        return result or run.final_text, []
    if mode == "json":
        if isinstance(structured, (dict, list)):
            return json.dumps(structured, ensure_ascii=False), []
        return result or run.final_text, []
    return result or run.final_text, []


def usage_from(run: ClaudeRun) -> Dict[str, int]:
    """OpenAI usage from the LAST API call of the spawn (the result event
    sums every internal call, which made Hermes over-count its context)."""
    u = run.usage_last or run.usage_total or {}
    inp = int(u.get("input_tokens") or 0)
    cr = int(u.get("cache_read_input_tokens") or 0)
    cw = int(u.get("cache_creation_input_tokens") or 0)
    out = int(u.get("output_tokens") or 0)
    if run.usage_total and run.usage_last is not run.usage_total:
        out = int(run.usage_total.get("output_tokens") or out)
    return {
        "prompt_tokens": inp + cr + cw,
        "completion_tokens": out,
        "total_tokens": inp + cr + cw + out,
        "prompt_tokens_details": {"cached_tokens": cr},
    }


def rate_limit_summary(run: ClaudeRun) -> str:
    rl = run.rate_limit or {}
    parts = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        w = rl.get(key)
        if isinstance(w, dict) and w.get("utilization") is not None:
            try:
                parts.append(f"{label}={int(round(float(w['utilization']) * 100))}%")
            except Exception:
                pass
    return " ".join(parts)


# ── HTTP ─────────────────────────────────────────────────────────────


def models_payload() -> Dict[str, Any]:
    now = int(time.time())
    return {"object": "list", "data": [
        {"id": m, "object": "model", "created": now, "owned_by": "claude-bridge", "context_length": CONTEXT_LENGTH, "max_input_tokens": CONTEXT_LENGTH}
        for m in MODEL_ALIASES + MODEL_IDS
    ]}


def dump(name: str, payload: Any) -> None:
    if not DUMP_DIR:
        return
    try:
        d = Path(DUMP_DIR)
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_text(payload if isinstance(payload, str) else json.dumps(payload, indent=2, ensure_ascii=False))
    except Exception as exc:
        log.warning("dump %s failed: %s", name, exc)


class Handler(BaseHTTPRequestHandler):
    server_version = f"claude-bridge/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, err: BridgeError) -> None:
        self._json(err.status, {"error": {"message": err.message, "type": err.etype, "code": err.code, "param": None}})

    def _sse(self, obj: Dict[str, Any]) -> None:
        self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _stream_error(self, err: BridgeError) -> None:
        """An OpenAI-style error after the 200 header went out. Hermes sees a
        status-less APIError and classifies it by message, so the message
        leads with the HTTP status line."""
        reason = http.client.responses.get(err.status, "Error")
        self._sse({"error": {"message": f"{err.status} {reason}: {err.message}", "type": err.etype, "code": err.code, "param": None, "status": err.status}})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _authorized(self) -> bool:
        token = CONTROL["token"]
        return bool(token) and hmac.compare_digest(self.headers.get("X-Bridge-Token") or "", token)

    def _client_gone(self) -> bool:
        try:
            r, _, _ = select.select([self.connection], [], [], 0)
            return bool(r) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except Exception:
            return True

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/v1/models", "/models"):
            return self._json(200, models_payload())
        if path == "/v1/approvals":
            if not self._authorized():
                return self._error(BridgeError(401, "control token required", code="unauthorized", etype="authentication_error"))
            q = dict(x.split("=", 1) for x in (self.path.split("?", 1)[1] if "?" in self.path else "").split("&") if "=" in x)
            try:
                after, wait = int(q.get("after", "0")), float(q.get("wait", "0"))
            except ValueError:
                return self._error(BridgeError(400, "after and wait must be numbers", code="bad_request", etype="invalid_request_error"))
            seq, items = APPROVALS.poll(after, wait)
            return self._json(200, {"instance": INSTANCE, "seq": seq, "approvals": items})
        if path == "/health":
            return self._json(200, {
                "ok": True, "version": __version__, "claude_bin": CLAUDE_BIN, "claude_version": SERVER_STATE.get("claude_version"),
                "workdir": str(WORKDIR), "permission_mode": PERMISSION_MODE, "sessions": len(STORE),
                "instance": INSTANCE, "pid": os.getpid(), "approvals": bool(CONTROL["token"]),
            })
        return self._error(BridgeError(404, f"no route {path}", code="not_found"))

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path.startswith("/v1/approvals/"):
            if not self._authorized():
                return self._error(BridgeError(401, "control token required", code="unauthorized", etype="authentication_error"))
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except Exception as exc:
                return self._error(BridgeError(400, f"bad JSON body: {exc}", code="bad_request", etype="invalid_request_error"))
            status, out = APPROVALS.answer(path.rsplit("/", 1)[1], str(body.get("user_id") or ""), str(body.get("platform") or ""), str(body.get("text") or ""))
            return self._json(status, out)
        if path not in ("/v1/chat/completions", "/chat/completions"):
            return self._error(BridgeError(404, f"no route {path}", code="not_found"))
        req_id = uuid.uuid4().hex[:8]
        t0 = time.time()
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception as exc:
            return self._error(BridgeError(400, f"bad JSON body: {exc}", code="bad_request", etype="invalid_request_error"))

        model = normalize_model(body.get("model"))
        if not model:
            return self._error(BridgeError(404, f"unknown model {body.get('model')!r}; use one of {', '.join(MODEL_ALIASES + MODEL_IDS)}", code="model_not_found", etype="invalid_request_error"))
        effort, effort_src = effort_with_source(body)
        stream = bool(body.get("stream"))
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        harness = wants_harness(body)
        n_msgs = len(body.get("messages") or [])
        dump(f"{req_id}-request.json", body)
        event_lines: List[str] = []

        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model}

        def chunk(delta: Dict[str, Any], finish_reason: Optional[str] = None) -> Dict[str, Any]:
            return {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason, "logprobs": None}]}

        sent = {"headers": False, "last": time.monotonic(), "probe": 0.0}

        def start_stream() -> None:
            if sent["headers"]:
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            sent["headers"] = True
            self._sse(chunk({"role": "assistant", "content": ""}))
            sent["last"] = time.monotonic()

        def on_text(piece: str) -> None:
            start_stream()
            self._sse(chunk({"content": piece}))
            sent["last"] = time.monotonic()

        def on_tick() -> None:
            now = time.monotonic()
            if now - sent["probe"] >= 1.0:
                sent["probe"] = now
                if self._client_gone():
                    raise ClientGone()
            if stream and now - sent["last"] >= HEARTBEAT_SECONDS:
                try:
                    start_stream()
                    self._sse(chunk({}))
                except OSError:
                    raise ClientGone()
                sent["last"] = time.monotonic()

        turn: Optional[Turn] = None
        mode = "tools" if harness else "text"
        try:
            if harness:
                turn, content, tool_calls = harness_turn(body, model, effort, on_tick=on_tick, on_event=event_lines.append if DUMP_DIR else None)
                run = turn.run
                assert run is not None
            else:
                system_prompt, user_prompt, mode, schema = build_prompts(body)
                with _slot():
                    run = run_claude(
                        model, effort, system_prompt, user_prompt, schema,
                        on_text=on_text if (stream and mode == "text") else None,
                        on_tick=on_tick,
                        on_event=event_lines.append if DUMP_DIR else None,
                    )
                content, tool_calls = run_to_message(run, mode)
        except ClientGone:
            log.info("req=%s client went away after %.1fs; claude stopped", req_id, time.time() - t0)
            return
        except BridgeError as err:
            log.warning("req=%s model=%s effort=%s(%s) mode=%s msgs=%d -> %d %s (%.1fs)", req_id, model, effort, effort_src, mode, n_msgs, err.status, err.code, time.time() - t0)
            if stream and sent["headers"]:
                return self._stream_error(err)
            return self._error(err)
        except Exception as exc:
            log.exception("req=%s unexpected failure", req_id)
            if stream and sent["headers"]:
                return self._stream_error(BridgeError(500, f"bridge failure: {exc}", code="bridge_error"))
            return self._error(BridgeError(500, f"bridge failure: {exc}", code="bridge_error"))
        finally:
            if event_lines:
                dump(f"{req_id}-events.jsonl", "".join(event_lines))

        usage = usage_from(run)
        finish = "tool_calls" if tool_calls else "stop"
        actual_model = ",".join(run.model_ids) or model
        where = ""
        if turn:
            where = f" key={turn.key[:24]} session={turn.session[:8]}{' ' + turn.mode} delta={turn.delta} hermes_tools={turn.offered} native={len(run.native_tools)}"
            if run.approvals:
                where += f" asked={run.approvals}"
            if run.permission_denials:
                where += f" denied={run.permission_denials}"
            if run.waited_background:
                where += " waited_bg"
        log.info(
            "req=%s model=%s(%s) effort=%s(%s) mode=%s msgs=%d stream=%d%s -> 200 %s calls=%d turns=%d in=%d cached=%d out=%d %.1fs%s",
            req_id, model, actual_model, effort, effort_src, mode, n_msgs, int(stream), where, finish,
            len(tool_calls), run.num_turns,
            usage["prompt_tokens"], usage["prompt_tokens_details"]["cached_tokens"], usage["completion_tokens"], time.time() - t0,
            (" " + rate_limit_summary(run)) if run.rate_limit else "",
        )

        if not stream:
            message: Dict[str, Any] = {"role": "assistant", "content": content if content else None}
            if tool_calls:
                message["tool_calls"] = tool_calls
            payload = {
                "id": cid, "object": "chat.completion", "created": created, "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": finish, "logprobs": None}],
                "usage": usage,
            }
            dump(f"{req_id}-response.json", payload)
            return self._json(200, payload)

        try:
            start_stream()
            remaining = content
            if run.text_streamed:
                if content.startswith(run.text_streamed):
                    remaining = content[len(run.text_streamed):]
                else:
                    log.warning("req=%s streamed text diverged from the final result; not re-sending", req_id)
                    remaining = ""
            step = 400
            for i in range(0, len(remaining), step):
                self._sse(chunk({"content": remaining[i:i + step]}))
            for idx, tc in enumerate(tool_calls):
                self._sse(chunk({"tool_calls": [{"index": idx, "id": tc["id"], "type": "function", "function": {"name": tc["function"]["name"], "arguments": ""}}]}))
                self._sse(chunk({"tool_calls": [{"index": idx, "function": {"arguments": tc["function"]["arguments"]}}]}))
            self._sse(chunk({}, finish))
            if include_usage:
                self._sse({**base, "choices": [], "usage": usage})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except OSError:
            log.warning("req=%s client went away before the reply was written", req_id)


SERVER_STATE: Dict[str, Any] = {}


def probe_claude() -> None:
    try:
        out = subprocess.run([CLAUDE_BIN, "--version"], capture_output=True, text=True, timeout=30, env=child_env(), cwd=str(SCRATCH_DIR))
        SERVER_STATE["claude_version"] = (out.stdout or out.stderr).strip()
    except Exception as exc:
        SERVER_STATE["claude_version"] = f"unavailable: {exc}"
    log.info("claude binary %s → %s", CLAUDE_BIN, SERVER_STATE["claude_version"])


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Hermes → Claude Code loopback bridge")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--token-fd", type=int, default=-1, help="read the approvals control token from this inherited fd")
    ap.add_argument("--owner-pid", type=int, default=0, help="pid of the process that started the bridge")
    args = ap.parse_args(argv)
    if args.token_fd >= 0:
        with os.fdopen(args.token_fd, "rb") as f:
            CONTROL["token"] = f.read(256).decode().strip()

    for d in (STATE_DIR, SCRATCH_DIR, WORKDIR):
        d.mkdir(parents=True, exist_ok=True)
    handlers: List[logging.Handler] = []
    try:
        handlers.append(logging.FileHandler(str(LOG_FILE)))
    except OSError:
        pass
    if sys.stderr.isatty() or not handlers:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("binding to %s — this bridge has no auth; keep it on loopback", args.host)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    pid_file = STATE_DIR / "bridge.pid"
    pid_file.write_text(json.dumps({"pid": os.getpid(), "owner": args.owner_pid, "instance": INSTANCE, "port": args.port}))
    threading.Thread(target=probe_claude, daemon=True).start()
    log.info("claude-bridge %s listening on http://%s:%d (concurrency=%d, workdir=%s, permission=%s, approvals=%s, drop=%d tools, context=%d, heartbeat=%ss)",
             __version__, args.host, args.port, MAX_CONCURRENCY, WORKDIR, PERMISSION_MODE, "on" if CONTROL["token"] else "off (no control token)", len(DROP_TOOLS), CONTEXT_LENGTH, int(HEARTBEAT_SECONDS))
    if FAKE_ERROR:
        log.warning("CLAUDE_BRIDGE_FAKE_ERROR=%s is set — every request returns a canned error", FAKE_ERROR)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
