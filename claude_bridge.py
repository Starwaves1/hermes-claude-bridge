#!/usr/bin/env python3
"""claude_bridge.py — Hermes → `claude -p` loopback bridge.

An OpenAI-compatible HTTP server (chat completions + models) that answers
every request by spawning the local, unmodified `claude` binary in print
mode. The binary owns its own claude.ai login, so usage draws from the
subscription and this process never sees, stores, or forwards a token.

Design (settled 2026-09-03, see README):
- Hermes owns the agent loop. Claude Code's tools are disabled on every
  spawn (--tools "" --safe-mode --strict-mcp-config).
- Hermes tool schemas are rendered into the system prompt; the model
  answers in a fixed JSON shape (--json-schema) that this bridge converts
  into OpenAI tool_calls. No tool ever runs inside the child process.
- The child is read as an event stream (--output-format stream-json). If the
  model calls a Hermes tool *natively* instead of via the JSON reply (Opus
  4.8 does this), the bridge intercepts that tool_use block, stops the child
  and returns it as a normal tool call — Claude Code would otherwise answer
  "No such tool available" and the model would tell the user its tools are
  gone (observed 2026-09-03, see ai/README.md).
- Usage-limit and login errors come back as HTTP errors with a plain
  message. There is deliberately no fallback model here.

Stdlib only. Python 3.9+.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pwd
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

__version__ = "1.1.0"

HOME = Path(os.path.expanduser("~"))
USER = pwd.getpwuid(os.getuid()).pw_name
STATE_DIR = Path(os.environ.get("CLAUDE_BRIDGE_STATE_DIR", str(HOME / ".hermes" / "claude-bridge")))
CLAUDE_BIN = os.environ.get("CLAUDE_BRIDGE_CLAUDE_BIN", str(HOME / ".local" / "bin" / "claude"))
WORKDIR = Path(os.environ.get("CLAUDE_BRIDGE_WORKDIR", str(STATE_DIR / "workdir")))
LOG_FILE = Path(os.environ.get("CLAUDE_BRIDGE_LOG", str(STATE_DIR / "bridge.log")))
DUMP_DIR = os.environ.get("CLAUDE_BRIDGE_DUMP_DIR", "")  # debugging only: writes conversation content, one file set per request id
MAX_CONCURRENCY = int(os.environ.get("CLAUDE_BRIDGE_MAX_CONCURRENCY", "3"))
SPAWN_TIMEOUT = float(os.environ.get("CLAUDE_BRIDGE_TIMEOUT", "900"))
QUEUE_WAIT = float(os.environ.get("CLAUDE_BRIDGE_QUEUE_WAIT", "120"))
CONTEXT_LENGTH = int(os.environ.get("CLAUDE_BRIDGE_CONTEXT_LENGTH", "200000"))
# Streamed requests outside the tool loop get an empty keep-alive chunk after
# this many silent seconds (Hermes counts any chunk as compression progress;
# its default idle budget is 30s). The tool loop never heartbeats, so its
# errors keep arriving as real HTTP status codes.
HEARTBEAT_SECONDS = float(os.environ.get("CLAUDE_BRIDGE_HEARTBEAT", "10"))
FAKE_ERROR = os.environ.get("CLAUDE_BRIDGE_FAKE_ERROR", "")  # test hook: "limit" | "login" | "crash"

# The one native tool Claude Code registers when --json-schema is given.
STRUCTURED_TOOL = "StructuredOutput"

# Model names Hermes may ask for. Aliases are passed to `claude --model`
# verbatim (the binary resolves them, honouring ~/.claude/settings.json pins).
MODEL_ALIASES = ("fable", "opus", "sonnet", "haiku")
MODEL_IDS = (
    "claude-fable-5-1",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-haiku-4-5",
)
_MODEL_RE = re.compile(r"^claude-[a-z0-9.-]+(\[1m\])?$")

# Hermes effort vocabulary → Claude Code --effort vocabulary.
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

TOOL_PROTOCOL_HEADER = """

# Tool calling protocol

The tools listed below belong to the Hermes harness. Hermes runs them and
returns the results as <tool_result> blocks in the next turn of the
transcript. They are NOT tools of your own runtime: your runtime exposes
exactly one native tool, StructuredOutput, which you use to deliver your
reply. Never invoke the names below through native function calling — that
fails with "No such tool available". Request them only inside the JSON reply.

Deliver every reply by calling StructuredOutput once with a JSON object of
exactly two fields (do not write the JSON as plain text):
- "content": your message to the user. It may be empty while you are still
  working with tools.
- "tool_calls": a list of {"name": <tool name>, "arguments": <object matching
  the tool's parameters>}. Use [] when no tool is needed.

Call several independent tools in one reply when that saves a round trip.
Never invent tool names or argument fields that are not in the schema.

## Available tools
"""

TRANSCRIPT_PREAMBLE = """Below is the conversation so far, as a transcript. You are the assistant.
Messages are wrapped in <user>, <assistant> and <tool_result> tags; your
earlier tool calls appear inside <tool_calls>. Write ONLY your next assistant
turn, addressed to the user, with no tags and no transcript formatting.

"""

log = logging.getLogger("claude_bridge")
_slots = threading.BoundedSemaphore(MAX_CONCURRENCY)


# ── helpers ──────────────────────────────────────────────────────────


def child_env() -> Dict[str, str]:
    """Environment for the `claude` child: minimal and deliberately clean.

    `USER` is what unlocks the keychain login (verified 2026-09-03: HOME+PATH
    alone reports "Not logged in"). No ANTHROPIC_* or CLAUDE_* variables are
    ever passed through, so an API key in the parent can never bill instead
    of the subscription and a parent Claude Code session cannot nest.
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
    """Return (claude_effort, source) where source is "request" or "default".

    Logged on every call so a missing `/effort` plumbing shows up as
    effort=medium(default) instead of hiding behind the default value.
    """
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
                parts.append("[image attachment omitted: this model route is text-only]")
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


def tool_name_set(tools: Iterable[Dict[str, Any]]) -> Set[str]:
    names: Set[str] = set()
    for t in tools or ():
        fn = t.get("function", t) if isinstance(t, dict) else {}
        if isinstance(fn, dict) and fn.get("name"):
            names.add(str(fn["name"]))
    return names


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
                rendered = []
                for c in calls:
                    fn = c.get("function", {}) if isinstance(c, dict) else {}
                    args = fn.get("arguments", "")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            pass
                    rendered.append(
                        json.dumps(
                            {"id": c.get("id"), "name": fn.get("name"), "arguments": args},
                            ensure_ascii=False,
                        )
                    )
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
    """Return (system_prompt, user_prompt, mode, schema).

    mode is "tools" (Hermes tool loop → fixed JSON contract), "json" (caller
    sent response_format=json_schema → that schema), "json_object"
    (response_format=json_object → instruction only) or "text".
    """
    messages = body.get("messages") or []
    system_text, rest = split_system(messages)
    tools = body.get("tools") or []
    tool_choice = body.get("tool_choice", "auto")
    tools_enabled = bool(tools) and tool_choice != "none"
    mode: str = "text"
    schema: Optional[Dict[str, Any]] = None
    if tools_enabled:
        mode, schema = "tools", OUTPUT_SCHEMA
        system_text = (system_text or "You are a helpful assistant.") + TOOL_PROTOCOL_HEADER + render_tools(tools)
        if tool_choice in ("required", "any") or isinstance(tool_choice, dict):
            system_text += "\n\nFor this turn you must call at least one tool."
    else:
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
    user_prompt = TRANSCRIPT_PREAMBLE + render_transcript(rest)
    return system_text, user_prompt, mode, schema


# ── claude subprocess ───────────────────────────────────────────────


class BridgeError(Exception):
    def __init__(self, status: int, message: str, code: str = "bridge_error", etype: str = "bridge_error"):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.etype = etype


_LIMIT_RE = re.compile(r"limit", re.I)
_LIMIT_HINT_RE = re.compile(r"usage|reset|hit your|reached|exceed|out of", re.I)


def classify_error(text: str) -> BridgeError:
    """Map claude -p failure text to an HTTP error.

    The usage-limit message must avoid the words Hermes's auxiliary router
    treats as billing exhaustion (credits, quota, billing, funds, payment,
    afford): those make it silently retry on the next provider in its
    chain, which this bridge rules out on purpose. See tests/test_bridge.py.
    """
    t = (text or "").strip() or "claude returned an error with no message"
    if "not logged in" in t.lower() or "/login" in t.lower():
        return BridgeError(
            401,
            "Claude Code is not logged in on this Mac. Run `claude auth login` in a terminal, then retry.",
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
    return BridgeError(502, f"claude -p failed: {t}", code="claude_error", etype="upstream_error")


class ClaudeRun:
    """Everything one `claude -p` spawn produced, as seen on its event stream."""

    def __init__(self) -> None:
        self.result_text: str = ""            # final `result` field of the result event
        self.structured: Any = None           # `structured_output` of the result event
        self.saw_result: bool = False
        self.is_error: bool = False
        self.subtype: str = ""
        self.num_turns: int = 0
        self.model_ids: List[str] = []
        self.api_key_source: str = ""
        self.rate_limit: Optional[Dict[str, Any]] = None   # unifiedWindows from the last rate_limit_event
        self.usage_last: Optional[Dict[str, Any]] = None   # usage of the LAST API call = real context size
        self.usage_total: Optional[Dict[str, Any]] = None  # aggregated usage across turns (result event)
        self.intercepted: bool = False
        self.native_calls: List[Dict[str, Any]] = []      # tool_use blocks the bridge took over
        self.native_text: str = ""                        # text blocks of that same message
        self.unknown_tool_errors: int = 0                 # "No such tool available" replies seen
        self.text_streamed: str = ""                      # text deltas already handed to on_text
        self.final_text: str = ""                         # text blocks of the last assistant message


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


def run_claude(
    model: str,
    effort: str,
    system_prompt: str,
    user_prompt: str,
    schema: Optional[Dict[str, Any]] = None,
    tool_names: Optional[Set[str]] = None,
    on_text: Optional[Callable[[str], None]] = None,
    on_tick: Optional[Callable[[], None]] = None,
    on_event: Optional[Callable[[str], None]] = None,
) -> ClaudeRun:
    """Spawn `claude -p` and consume its stream-json events.

    on_text(delta) receives text deltas as they arrive (caller decides whether
    to forward them). on_tick() is called on every event and at least once a
    second while waiting, so the caller can keep its own connection alive.
    on_event(raw_line) sees every stdout line (debug dumps).
    """
    if FAKE_ERROR == "limit":
        raise classify_error("Claude usage limit reached. Your limit will reset at 3pm (America/New_York).")
    if FAKE_ERROR == "login":
        raise classify_error("Not logged in · Please run /login")
    if FAKE_ERROR == "crash":
        raise BridgeError(502, "claude -p failed: simulated crash", code="claude_error", etype="upstream_error")

    tool_names = tool_names or set()
    WORKDIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", prefix="sys-", dir=str(WORKDIR), delete=False) as f:
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
            cwd=str(WORKDIR),
            env=child_env(),
        )
    except FileNotFoundError:
        _unlink(sys_path)
        raise BridgeError(500, f"claude binary not found at {CLAUDE_BIN}", code="claude_missing")

    lines: "queue.Queue[Optional[str]]" = queue.Queue()
    stderr_buf: List[str] = []
    other_lines: List[str] = []

    def feed() -> None:
        try:
            assert proc.stdin is not None
            proc.stdin.write(user_prompt)
            proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

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

    deadline = time.monotonic() + SPAWN_TIMEOUT
    cur_text = ""                      # text of the assistant message in flight
    cur_calls: List[Dict[str, Any]] = []
    seen_call_ids: Set[str] = set()
    try:
        while True:
            try:
                line = lines.get(timeout=1.0)
            except queue.Empty:
                if time.monotonic() > deadline:
                    _terminate(proc)
                    raise BridgeError(504, f"claude -p exceeded {int(SPAWN_TIMEOUT)}s", code="claude_timeout", etype="timeout_error")
                if on_tick:
                    on_tick()
                continue
            if line is None:
                break
            if on_event:
                on_event(line)
            if on_tick:
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
            etype = ev.get("type")
            if etype == "system":
                if ev.get("subtype") == "init":
                    if ev.get("model"):
                        run.model_ids = [str(ev["model"])]
                    run.api_key_source = str(ev.get("apiKeySource") or "")
            elif etype == "rate_limit_event":
                info = ev.get("rate_limit_info") or {}
                if isinstance(info, dict) and isinstance(info.get("unifiedWindows"), dict):
                    run.rate_limit = info["unifiedWindows"]
            elif etype == "stream_event":
                e = ev.get("event") or {}
                st = e.get("type")
                if st == "message_start":
                    cur_text, cur_calls, seen_call_ids = "", [], set()
                elif st == "content_block_delta":
                    d = e.get("delta") or {}
                    if d.get("type") == "text_delta":
                        piece = str(d.get("text") or "")
                        cur_text += piece
                        if on_text and piece:
                            on_text(piece)
                            run.text_streamed += piece
                elif st == "message_delta":
                    u = e.get("usage")
                    if isinstance(u, dict):
                        run.usage_last = u
                elif st == "message_stop":
                    run.final_text = cur_text
                    if cur_calls:
                        run.intercepted = True
                        run.native_calls = list(cur_calls)
                        run.native_text = cur_text
                        break
            elif etype == "assistant":
                msg = ev.get("message") or {}
                u = msg.get("usage")
                if isinstance(u, dict) and run.usage_last is None:
                    run.usage_last = u
                for block in msg.get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = str(block.get("name") or "")
                    if name == STRUCTURED_TOOL:
                        continue
                    bid = str(block.get("id") or "")
                    if bid and bid in seen_call_ids:
                        continue
                    seen_call_ids.add(bid)
                    if name in tool_names:
                        cur_calls.append({"id": bid, "name": name, "input": block.get("input") if isinstance(block.get("input"), dict) else {}})
                    else:
                        log.warning("model called unknown native tool %r (not a Hermes tool); letting claude reject it", name)
            elif etype == "user":
                if "No such tool available" in stripped:
                    run.unknown_tool_errors += 1
            elif etype == "result" or ("result" in ev and "usage" in ev and "duration_api_ms" in ev):
                run.saw_result = True
                run.result_text = str(ev.get("result") or "")
                run.structured = ev.get("structured_output")
                run.is_error = bool(ev.get("is_error"))
                run.subtype = str(ev.get("subtype") or "")
                try:
                    run.num_turns = int(ev.get("num_turns") or 0)
                except Exception:
                    run.num_turns = 0
                if isinstance(ev.get("usage"), dict):
                    run.usage_total = ev["usage"]
                mu = ev.get("modelUsage")
                if isinstance(mu, dict) and mu:
                    run.model_ids = list(mu.keys())
    finally:
        if run.intercepted:
            _terminate(proc)
        _unlink(sys_path)

    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        _terminate(proc)
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        try:
            if pipe is not None:
                pipe.close()
        except Exception:
            pass

    if run.api_key_source and run.api_key_source != "none":
        log.warning("claude reports apiKeySource=%s — this spawn may be billing an API key, not the subscription", run.api_key_source)

    if run.intercepted:
        return run
    stderr_text = "".join(stderr_buf).strip()
    if run.saw_result:
        if run.is_error:
            raise classify_error(run.result_text or stderr_text or f"claude exited {proc.returncode} ({run.subtype})")
        if proc.returncode not in (0, None):
            log.warning("claude exited %s after a successful result event; stderr tail: %s", proc.returncode, stderr_text[-300:])
        return run
    tail = (stderr_text + "\n" + "\n".join(other_lines[-10:])).strip()[-800:]
    raise classify_error(tail or f"exit {proc.returncode} with no result event")


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def run_to_message(run: ClaudeRun, mode: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Return (content, openai_tool_calls) for the given prompt mode."""
    if run.intercepted:
        calls_out = []
        for c in run.native_calls:
            calls_out.append({
                "id": "call_" + uuid.uuid4().hex[:24],
                "type": "function",
                "function": {"name": c["name"], "arguments": json.dumps(c.get("input") or {}, ensure_ascii=False)},
            })
        return run.native_text, calls_out
    structured = run.structured
    if isinstance(structured, str):
        try:
            structured = json.loads(structured)
        except Exception:
            structured = None
    result = run.result_text
    if mode == "tools":
        if not isinstance(structured, dict) and result:
            # schema requested but structured_output missing: try the text itself
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


# kept for callers/tests that still hold a raw result dict
def claude_result_to_message(data: Dict[str, Any], mode: str) -> Tuple[str, List[Dict[str, Any]]]:
    run = ClaudeRun()
    run.saw_result = True
    run.result_text = str(data.get("result") or "")
    run.structured = data.get("structured_output")
    return run_to_message(run, mode)


def usage_from(run: ClaudeRun) -> Dict[str, int]:
    """OpenAI usage block from the LAST API call of the spawn.

    The result event's usage is summed over every internal turn (a
    "[structured-output-enforce]" re-prompt or a rejected native tool call
    doubles it), which made Hermes think a 160k context was 660k and
    auto-compress every turn. The last call's input is the real context.
    """
    u = run.usage_last or run.usage_total or {}
    inp = int(u.get("input_tokens") or 0)
    cr = int(u.get("cache_read_input_tokens") or 0)
    cw = int(u.get("cache_creation_input_tokens") or 0)
    out = int(u.get("output_tokens") or 0)
    if run.usage_total and run.usage_last is not run.usage_total:
        # output of the whole spawn is what the user paid for; report that
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
    items = []
    for m in MODEL_ALIASES + MODEL_IDS:
        items.append({
            "id": m,
            "object": "model",
            "created": now,
            "owned_by": "claude-bridge",
            "context_length": CONTEXT_LENGTH,
            "max_input_tokens": CONTEXT_LENGTH,
        })
    return {"object": "list", "data": items}


def dump(name: str, payload: Any) -> None:
    if not DUMP_DIR:
        return
    try:
        d = Path(DUMP_DIR)
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        if isinstance(payload, str):
            p.write_text(payload)
        else:
            p.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    except Exception as exc:  # debugging aid only
        log.warning("dump %s failed: %s", name, exc)


class Handler(BaseHTTPRequestHandler):
    server_version = f"claude-bridge/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet; we log ourselves
        return

    # -- responses --
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

    # -- routes --
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/v1/models", "/models"):
            return self._json(200, models_payload())
        if path == "/health":
            return self._json(200, {"ok": True, "version": __version__, "claude_bin": CLAUDE_BIN, "claude_version": SERVER_STATE.get("claude_version")})
        return self._error(BridgeError(404, f"no route {path}", code="not_found"))

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
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
        try:
            system_prompt, user_prompt, mode, schema = build_prompts(body)
        except Exception as exc:
            return self._error(BridgeError(400, f"could not render messages: {exc}", code="bad_request", etype="invalid_request_error"))
        tools = body.get("tools") or []
        n_tools = len(tools)
        tool_names = tool_name_set(tools) if mode == "tools" else set()
        n_msgs = len(body.get("messages") or [])
        dump(f"{req_id}-request.json", body)
        dump(f"{req_id}-system.txt", system_prompt)
        dump(f"{req_id}-prompt.txt", user_prompt)
        event_lines: List[str] = []

        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model}

        def chunk(delta: Dict[str, Any], finish_reason: Optional[str] = None) -> Dict[str, Any]:
            return {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason, "logprobs": None}]}

        sent = {"headers": False, "last": time.monotonic()}

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
            # The tool loop keeps real HTTP status codes (no early headers);
            # aux/text calls get keep-alives so Hermes sees progress.
            if mode == "tools" and not sent["headers"]:
                return
            if time.monotonic() - sent["last"] >= HEARTBEAT_SECONDS:
                start_stream()
                self._sse(chunk({}))
                sent["last"] = time.monotonic()

        if not _slots.acquire(timeout=QUEUE_WAIT):
            return self._error(BridgeError(503, "bridge busy: too many concurrent claude runs", code="busy", etype="overloaded_error"))
        try:
            run = run_claude(
                model, effort, system_prompt, user_prompt, schema,
                tool_names=tool_names,
                on_text=on_text if (stream and mode == "text") else None,
                on_tick=on_tick if stream else None,
                on_event=event_lines.append if DUMP_DIR else None,
            )
        except BridgeError as err:
            log.warning("req=%s model=%s effort=%s(%s) mode=%s msgs=%d tools=%d -> %d %s (%.1fs)", req_id, model, effort, effort_src, mode, n_msgs, n_tools, err.status, err.code, time.time() - t0)
            if stream and sent["headers"]:
                self._sse({"error": {"message": err.message, "type": err.etype, "code": err.code, "param": None}})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return
            return self._error(err)
        except Exception as exc:
            log.exception("req=%s unexpected failure", req_id)
            if stream and sent["headers"]:
                self._sse({"error": {"message": f"bridge failure: {exc}", "type": "bridge_error", "code": "bridge_error", "param": None}})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return
            return self._error(BridgeError(500, f"bridge failure: {exc}", code="bridge_error"))
        finally:
            _slots.release()
            if event_lines:
                dump(f"{req_id}-events.jsonl", "".join(event_lines))

        content, tool_calls = run_to_message(run, mode)
        usage = usage_from(run)
        finish = "tool_calls" if tool_calls else "stop"
        actual_model = ",".join(run.model_ids) or model
        dump(f"{req_id}-claude.json", {"result": run.result_text, "structured_output": run.structured, "intercepted": run.intercepted, "native_calls": run.native_calls, "usage_last": run.usage_last, "usage_total": run.usage_total, "num_turns": run.num_turns})
        log.info(
            "req=%s model=%s(%s) effort=%s(%s) mode=%s msgs=%d tools=%d stream=%d -> 200 %s calls=%d%s turns=%d in=%d cached=%d out=%d %.1fs%s",
            req_id, model, actual_model, effort, effort_src, mode, n_msgs, n_tools, int(stream), finish,
            len(tool_calls), " native" if run.intercepted else "", run.num_turns,
            usage["prompt_tokens"], usage["prompt_tokens_details"]["cached_tokens"], usage["completion_tokens"], time.time() - t0,
            (" " + rate_limit_summary(run)) if run.rate_limit else "",
        )
        if run.unknown_tool_errors:
            log.warning("req=%s claude rejected %d native tool call(s) before the model answered; check the tool protocol prompt", req_id, run.unknown_tool_errors)

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

        start_stream()
        remaining = content
        if run.text_streamed:
            if content.startswith(run.text_streamed):
                remaining = content[len(run.text_streamed):]
            else:
                log.warning("req=%s streamed text diverged from the final result; not re-sending", req_id)
                remaining = ""
        if remaining:
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


SERVER_STATE: Dict[str, Any] = {}


def probe_claude() -> None:
    try:
        out = subprocess.run([CLAUDE_BIN, "--version"], capture_output=True, text=True, timeout=30, env=child_env(), cwd=str(WORKDIR))
        SERVER_STATE["claude_version"] = (out.stdout or out.stderr).strip()
    except Exception as exc:
        SERVER_STATE["claude_version"] = f"unavailable: {exc}"
    log.info("claude binary %s → %s", CLAUDE_BIN, SERVER_STATE["claude_version"])


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Hermes → claude -p loopback bridge")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    WORKDIR.mkdir(parents=True, exist_ok=True)
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.append(logging.FileHandler(str(LOG_FILE)))
    except OSError:
        pass
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("binding to %s — this bridge has no auth; keep it on loopback", args.host)
    probe_claude()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    log.info("claude-bridge %s listening on http://%s:%d (concurrency=%d, effort default=%s, context=%d, heartbeat=%ss)", __version__, args.host, args.port, MAX_CONCURRENCY, DEFAULT_EFFORT, CONTEXT_LENGTH, int(HEARTBEAT_SECONDS))
    if FAKE_ERROR:
        log.warning("CLAUDE_BRIDGE_FAKE_ERROR=%s is set — every request returns a canned error", FAKE_ERROR)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
