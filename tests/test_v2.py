"""Claude Code harness path and the /login relay, against fake `claude` scripts.
Run: python3 -m unittest discover -s tests
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="bridge-v2-test-"))
for k, v in {"CLAUDE_BRIDGE_STATE_DIR": TMP / "state", "CLAUDE_BRIDGE_WORKDIR": TMP / "work", "CLAUDE_BRIDGE_LOG": TMP / "bridge.log", "CLAUDE_BRIDGE_AUTOSTART": "0"}.items():
    os.environ.setdefault(k, str(v))
sys.path.insert(0, str(ROOT))
import claude_bridge as cb  # noqa: E402

FAKE = TMP / "fake-claude.py"
FAKE.write_text(f"""#!{sys.executable}
import os, sys, time
d = os.environ["FAKE_DIR"]
n = len([f for f in os.listdir(d) if f.endswith(".args")])
args = sys.argv[1:]
open(f"{{d}}/{{n}}.args", "w").write("\\n".join(args))
if "--append-system-prompt-file" in args:
    open(f"{{d}}/{{n}}.append", "w").write(open(args[args.index("--append-system-prompt-file") + 1]).read())
open(f"{{d}}/{{n}}.stdin", "w").write(sys.stdin.read())
open(f"{{d}}/{{n}}.pid", "w").write(str(os.getpid()))
if os.path.exists(f"{{d}}/sleep-{{n}}"):
    time.sleep(float(open(f"{{d}}/sleep-{{n}}").read()))
rp = f"{{d}}/reply-{{n}}.jsonl"
sys.stdout.write(open(rp if os.path.exists(rp) else f"{{d}}/reply.jsonl").read())
sys.stdout.flush()
ep = f"{{d}}/exit-{{n}}"
sys.exit(int(open(ep).read()) if os.path.exists(ep) else 0)
""")
FAKE.chmod(0o755)

FAKE_LOGIN = TMP / "fake-claude-login.py"
FAKE_LOGIN.write_text(f"""#!{sys.executable}
import json, os, sys
state = os.path.join(os.environ["HOME"], "logged-in")
a = sys.argv[1:]
if a[:2] == ["auth", "status"]:
    ok = os.path.exists(state)
    print(json.dumps({{"loggedIn": ok, "authMethod": "claude.ai" if ok else "none", "subscriptionType": "max", "email": "someone@example.com"}}))
    sys.exit(0 if ok else 1)
if a[:2] == ["auth", "login"]:
    url = "https://claude.com/cai/oauth/authorize?code=true&state=abc"
    sys.stdout.write("Opening browser to sign in\\u2026\\r\\nIf the browser didn't open, visit: \\x1b]8;;" + url + "\\x07\\x1b[94m" + url + "\\x1b[39m\\x1b]8;;\\x07\\r\\n")
    sys.stdout.write("\\r\\u280b\\r\\u2819\\rPaste code here if prompted > ")
    sys.stdout.flush()
    code = sys.stdin.readline().strip()
    sys.stdout.write("\\r\\n" + code + "\\r\\n")
    if code == "GOODCODE#123":
        open(state, "w").write("1")
        print("Login successful.")
        sys.exit(0)
    print("Invalid code")
    sys.exit(1)
if a[:2] == ["auth", "logout"]:
    if os.path.exists(state):
        os.unlink(state)
    print("Successfully logged out from your Anthropic account.")
    sys.exit(0)
sys.exit(2)
""")
FAKE_LOGIN.chmod(0o755)

USAGE = {"input_tokens": 3, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 100, "output_tokens": 20}


def turn_events(content="", calls=None, session="s", extra=None, is_error=False, result=None):
    structured = {"content": content, "tool_calls": calls or []}
    ev = [
        {"type": "system", "subtype": "init", "model": "claude-fable-5-1", "permissionMode": "auto", "apiKeySource": "none", "session_id": session},
        {"type": "stream_event", "event": {"type": "message_start", "message": {"usage": USAGE}}},
    ]
    ev += extra or []
    if not is_error:
        ev += [
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_s", "name": "StructuredOutput", "input": structured}], "usage": USAGE}},
            {"type": "stream_event", "event": {"type": "message_delta", "usage": USAGE}},
            {"type": "stream_event", "event": {"type": "message_stop"}},
        ]
    res = {"type": "result", "subtype": "error" if is_error else "success", "is_error": is_error, "duration_api_ms": 1, "num_turns": 2,
           "session_id": session, "result": result if result is not None else json.dumps(structured), "usage": {**USAGE, "output_tokens": 50}, "modelUsage": {"claude-fable-5-1": {}}, "permission_denials": []}
    if not is_error:
        res["structured_output"] = structured
    ev.append(res)
    return "".join(json.dumps(e) + "\n" for e in ev)


TOOLS = [
    {"type": "function", "function": {"name": n, "description": f"the {n} tool", "parameters": {"type": "object", "properties": {"x": {"type": "string"}}}}}
    for n in ("terminal", "read_file", "web_search", "text_to_speech", "memory", "send_message")
]
SYSTEM = {"role": "system", "content": "You are Mica, a personal assistant."}
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


class Harness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), cb.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="calls-", dir=TMP))
        self.saved = {k: getattr(cb, k) for k in ("CLAUDE_BIN", "child_env", "STORE", "WORKDIR", "SCRATCH_DIR", "HEARTBEAT_SECONDS")}
        self.saved_help = dict(cb._HELP)
        cb.CLAUDE_BIN = str(FAKE)
        orig_env = self.saved["child_env"]
        cb.child_env = lambda: {**orig_env(), "FAKE_DIR": str(self.dir)}
        cb.STORE = cb.SessionStore(self.dir / "sessions.json")
        cb.WORKDIR = self.dir / "workspace"
        cb.SCRATCH_DIR = self.dir / "scratch"
        cb._HELP["text"] = "--system-prompt-snapshot <on|off>"

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(cb, k, v)
        cb._HELP.clear()
        cb._HELP.update(self.saved_help)

    def reply(self, text, n=None):
        (self.dir / (f"reply-{n}.jsonl" if n is not None else "reply.jsonl")).write_text(text)

    def call(self, n):
        argv = (self.dir / f"{n}.args").read_text().split("\n")
        stdin = (self.dir / f"{n}.stdin").read_text()
        lines = [json.loads(l) for l in stdin.splitlines() if l.strip()]
        append = (self.dir / f"{n}.append").read_text() if (self.dir / f"{n}.append").exists() else ""
        return argv, lines, append

    def post(self, body, raw=False):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
                return r.status, (data if raw else json.loads(data))
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def body(self, messages, key="conv-1", **kw):
        b = {"model": "fable", "messages": [SYSTEM] + messages, "tools": TOOLS, "claude_bridge_session": key}
        b.update(kw)
        return b

    @staticmethod
    def as_hermes(msg):
        return {k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")}

    @staticmethod
    def texts(line):
        return "\n".join(b.get("text", "") for b in line["message"]["content"] if b["type"] == "text")

    def test_first_turn_then_resume_with_only_the_delta(self):
        self.reply(turn_events("hello there"), 0)
        status, out = self.post(self.body([{"role": "user", "content": "hi there"}]))
        self.assertEqual(status, 200)
        msg = out["choices"][0]["message"]
        self.assertEqual(msg["content"], "hello there")
        self.assertEqual(out["usage"]["prompt_tokens"], 1003)  # last API call's context
        argv, lines, append = self.call(0)
        sid = argv[argv.index("--session-id") + 1]
        self.assertNotIn("--resume", argv)
        for flag, val in (("--input-format", "stream-json"), ("--output-format", "stream-json"), ("--permission-mode", "auto"), ("--permission-prompts", "none"), ("--model", "fable")):
            self.assertEqual(argv[argv.index(flag) + 1], val)
        for banned in ("--safe-mode", "--strict-mcp-config", "--tools", "--bare", "--setting-sources", "--no-session-persistence", "--system-prompt-file"):
            self.assertNotIn(banned, argv)
        self.assertIn("--json-schema", argv)
        self.assertIn("--include-partial-messages", argv)
        self.assertEqual(lines[0]["type"], "user")
        self.assertEqual(self.texts(lines[0]), "hi there")
        self.assertTrue(append.startswith("You are Mica"))
        self.assertIn("Running behind Hermes", append)

        self.reply(turn_events("doing fine"), 1)
        hist = [{"role": "user", "content": "hi there"}, self.as_hermes(msg), {"role": "user", "content": "how are you?"}]
        status, out = self.post(self.body(hist))
        self.assertEqual(status, 200)
        argv, lines, _ = self.call(1)
        self.assertEqual(argv[argv.index("--resume") + 1], sid)
        self.assertNotIn("--session-id", argv)
        self.assertNotIn("--system-prompt-snapshot", argv)  # prompt unchanged
        self.assertEqual(self.texts(lines[0]), "how are you?")
        self.assertNotIn("hi there", json.dumps(lines))
        self.assertEqual(cb.STORE.get("conv-1")["turns"], 2)

    def test_tool_result_round_trip(self):
        self.reply(turn_events("one moment", [{"name": "text_to_speech", "arguments": {"x": "hello"}}]), 0)
        status, out = self.post(self.body([{"role": "user", "content": "say hello"}]))
        msg = out["choices"][0]["message"]
        self.assertEqual(out["choices"][0]["finish_reason"], "tool_calls")
        call = msg["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "text_to_speech")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"x": "hello"})

        self.reply(turn_events("sent the voice note"), 1)
        hist = [{"role": "user", "content": "say hello"}, self.as_hermes(msg), {"role": "tool", "tool_call_id": call["id"], "content": '{"ok": true, "file": "/tmp/a.ogg"}'}]
        status, out = self.post(self.body(hist))
        self.assertEqual(status, 200)
        self.assertEqual(out["choices"][0]["message"]["content"], "sent the voice note")
        argv, lines, _ = self.call(1)
        self.assertIn("--resume", argv)
        text = self.texts(lines[0])
        self.assertIn(f'<tool_result tool_call_id="{call["id"]}" name="text_to_speech">', text)
        self.assertIn("/tmp/a.ogg", text)
        self.assertNotIn("say hello", text)

    def test_duplicated_tools_are_dropped_from_the_prompt(self):
        self.reply(turn_events("ok"))
        self.post(self.body([{"role": "user", "content": "x"}]))
        _, _, append = self.call(0)
        tools = append.split("## Hermes tools", 1)[1]
        for gone in ("### terminal", "### read_file", "### web_search"):
            self.assertNotIn(gone, tools)
        for kept in ("### text_to_speech", "### memory", "### send_message"):
            self.assertIn(kept, tools)
        self.assertIn("may name terminal, read_file, web_search.", append)

    def test_edited_history_starts_a_new_session_with_the_transcript(self):
        self.reply(turn_events("first answer"))
        self.post(self.body([{"role": "user", "content": "q1"}]))
        sid0 = cb.STORE.get("conv-1")["uuid"]
        hist = [{"role": "user", "content": "q1 (edited)"}, {"role": "assistant", "content": "an answer from elsewhere"}, {"role": "user", "content": "q2"}]
        status, _ = self.post(self.body(hist))
        self.assertEqual(status, 200)
        argv, lines, _ = self.call(1)
        sid1 = argv[argv.index("--session-id") + 1]
        self.assertNotEqual(sid0, sid1)
        text = self.texts(lines[0])
        self.assertIn("<user>\nq1 (edited)\n</user>", text)
        self.assertIn("an answer from elsewhere", text)
        self.assertTrue(text.rstrip().endswith("q2"))
        self.assertEqual(cb.STORE.get("conv-1")["uuid"], sid1)

    def test_altered_reply_still_resumes_via_last_fed_message(self):
        self.reply(turn_events("**bold** answer"))
        self.post(self.body([{"role": "user", "content": "q1"}]))
        hist = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "bold answer"}, {"role": "user", "content": "q2"}]
        self.post(self.body(hist))
        argv, lines, _ = self.call(1)
        self.assertIn("--resume", argv)
        self.assertEqual(self.texts(lines[0]), "q2")

    def test_rotated_key_after_compression_keeps_the_session(self):
        self.reply(turn_events("Here is the long, detailed plan you asked for."))
        _, out = self.post(self.body([{"role": "user", "content": "plan it"}], key="sess-a"))
        sid = cb.STORE.get("sess-a")["uuid"]
        hist = [{"role": "user", "content": "[summary of earlier turns]"}, self.as_hermes(out["choices"][0]["message"]), {"role": "user", "content": "next step?"}]
        self.post(self.body(hist, key="sess-b"))
        argv, lines, _ = self.call(1)
        self.assertEqual(argv[argv.index("--resume") + 1], sid)
        self.assertEqual(self.texts(lines[0]), "next step?")
        self.assertIsNone(cb.STORE.get("sess-a"))
        self.assertEqual(cb.STORE.get("sess-b")["uuid"], sid)

    def test_short_common_reply_is_not_adopted(self):
        self.reply(turn_events("Done."))
        _, out = self.post(self.body([{"role": "user", "content": "do it"}], key="sess-a"))
        hist = [{"role": "user", "content": "other chat"}, self.as_hermes(out["choices"][0]["message"]), {"role": "user", "content": "hi"}]
        self.post(self.body(hist, key="sess-b"))
        self.assertIn("--session-id", self.call(1)[0])

    def test_missing_claude_session_falls_back_to_a_new_one(self):
        self.reply(turn_events("a1"), 0)
        _, out = self.post(self.body([{"role": "user", "content": "q1"}]))
        (self.dir / "reply-1.jsonl").write_text("No conversation found with session ID: x\n")
        (self.dir / "exit-1").write_text("1")
        self.reply(turn_events("a2"), 2)
        hist = [{"role": "user", "content": "q1"}, self.as_hermes(out["choices"][0]["message"]), {"role": "user", "content": "q2"}]
        status, out = self.post(self.body(hist))
        self.assertEqual(status, 200)
        self.assertEqual(out["choices"][0]["message"]["content"], "a2")
        self.assertIn("--resume", self.call(1)[0])
        argv, lines, _ = self.call(2)
        self.assertIn("--session-id", argv)
        self.assertIn("<user>\nq1\n</user>", self.texts(lines[0]))

    def test_changed_system_prompt_turns_snapshot_off(self):
        self.reply(turn_events("a"))
        _, out = self.post(self.body([{"role": "user", "content": "q1"}]))
        b = self.body([{"role": "user", "content": "q1"}, self.as_hermes(out["choices"][0]["message"]), {"role": "user", "content": "q2"}])
        b["messages"][0] = {"role": "system", "content": "You are Mica. New memory entry."}
        self.post(b)
        argv, _, append = self.call(1)
        self.assertEqual(argv[argv.index("--system-prompt-snapshot") + 1], "off")
        self.assertIn("New memory entry", append)

    def test_images_become_content_blocks(self):
        self.reply(turn_events("a red square"))
        content = [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}},
            {"type": "image_url", "image_url": {"url": "https://example.com/cat.jpg"}},
        ]
        status, _ = self.post(self.body([{"role": "user", "content": content}]))
        self.assertEqual(status, 200)
        blocks = self.call(0)[1][0]["message"]["content"]
        self.assertEqual(blocks[0], {"type": "text", "text": "what is this?"})
        self.assertEqual(blocks[1], {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}})
        self.assertEqual(blocks[2]["type"], "text")
        self.assertIn("https://example.com/cat.jpg", blocks[2]["text"])

    def test_native_hermes_tool_attempt_is_logged_not_intercepted(self):
        extra = [
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_a", "name": "text_to_speech", "input": {}}], "usage": USAGE}},
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "No such tool available: text_to_speech"}]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_b", "name": "Bash", "input": {"command": "date"}}], "usage": USAGE}},
        ]
        self.reply(turn_events("done", [{"name": "text_to_speech", "arguments": {"x": "hi"}}], extra=extra))
        with self.assertLogs("claude_bridge", "WARNING") as logs:
            status, out = self.post(self.body([{"role": "user", "content": "say hi"}]))
        self.assertEqual(status, 200)
        self.assertEqual(out["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "text_to_speech")
        self.assertTrue(any("natively" in l for l in logs.output))

    def test_not_logged_in_is_401(self):
        self.reply(turn_events(is_error=True, result="Not logged in · Please run /login"))
        status, out = self.post(self.body([{"role": "user", "content": "x"}], stream=True))
        self.assertEqual(status, 401)
        self.assertIn("/login", out["error"]["message"])
        self.assertIsNone(cb.STORE.get("conv-1"))

    def test_heartbeats_while_claude_works(self):
        self.reply(turn_events("finally"))
        (self.dir / "sleep-0").write_text("1.3")
        cb.HEARTBEAT_SECONDS = 0.3
        status, raw = self.post(self.body([{"role": "user", "content": "slow job"}], stream=True, stream_options={"include_usage": True}), raw=True)
        self.assertEqual(status, 200)
        events = [json.loads(l[6:]) for l in raw.decode().split("\n\n") if l.startswith("data: ") and l != "data: [DONE]"]
        empties = [e for e in events if e["choices"] and e["choices"][0]["delta"] == {} and e["choices"][0]["finish_reason"] is None]
        self.assertGreaterEqual(len(empties), 2)
        text = "".join(e["choices"][0]["delta"].get("content") or "" for e in events if e["choices"])
        self.assertEqual(text, "finally")
        self.assertEqual(events[-1]["usage"]["prompt_tokens"], 1003)

    def test_client_disconnect_stops_claude(self):
        self.reply(turn_events("never seen"))
        (self.dir / "sleep-0").write_text("30")
        raw = json.dumps(self.body([{"role": "user", "content": "long job"}], stream=True)).encode()
        s = socket.create_connection(("127.0.0.1", self.port))
        s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(raw) + raw)
        pid_file = self.dir / "0.pid"
        for _ in range(50):
            if pid_file.exists():
                break
            time.sleep(0.1)
        pid = int(pid_file.read_text())
        s.close()
        for _ in range(60):
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, 9)
            self.fail("claude child still running after the client left")

    def test_turns_without_a_key_still_resume(self):
        self.reply(turn_events("a1"))
        b = self.body([{"role": "user", "content": "q1"}])
        del b["claude_bridge_session"]
        _, out = self.post(b)
        b["messages"] += [self.as_hermes(out["choices"][0]["message"]), {"role": "user", "content": "q2"}]
        self.post(b)
        self.assertIn("--resume", self.call(1)[0])

    def test_models_advertise_a_large_window(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/v1/models") as r:
            self.assertEqual(json.load(r)["data"][0]["context_length"], 1000000)


def load_login_plugin():
    spec = importlib.util.spec_from_file_location("claude_login_under_test", ROOT / "plugin" / "claude-login" / "__init__.py", submodule_search_locations=[str(ROOT / "plugin" / "claude-login")])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class LoginRelay(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="home-", dir=TMP))
        self.env_saved = {k: os.environ.get(k) for k in ("HOME", "CLAUDE_BRIDGE_CLAUDE_BIN", "CLAUDE_BRIDGE_LOGIN_USERS")}
        os.environ.update({"HOME": str(self.home), "CLAUDE_BRIDGE_CLAUDE_BIN": str(FAKE_LOGIN), "CLAUDE_BRIDGE_LOGIN_USERS": "42"})
        self.mod = load_login_plugin()

    def tearDown(self):
        for k, v in self.env_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for s in list(self.mod.PENDING.values()):
            s.cancel()

    def test_clean_strips_escapes_and_redraws(self):
        sample = ("Opening browser to sign in…\r\nIf the browser didn't open, visit: \x1b]8;;https://x/y\x07\x1b[94mhttps://x/y\x1b[39m\x1b]8;;\x07\r\nPaste code here if prompted > ")  # shape of a real `claude auth login` capture, 2.1.292
        lines = self.mod.relay.clean(sample + "\r⠋\r⠙\r")
        self.assertEqual(lines[0], "Opening browser to sign in…")
        self.assertTrue(lines[1].startswith("If the browser didn't open, visit: https://"))
        self.assertNotIn("\x1b", "".join(lines))
        self.assertEqual(lines[-1], "Paste code here if prompted >")

    def test_pty_relay_with_fake_login(self):
        sent = []
        done = []
        s = self.mod.relay.LoginSession([str(FAKE_LOGIN), "auth", "login", "--claudeai"], self.mod.claude_env(), send=sent.append, on_exit=done.append, batch=0.2).start()
        for _ in range(50):
            if any("Paste code" in m for m in sent):
                break
            time.sleep(0.1)
        self.assertTrue(any("https://claude.com/cai/oauth/authorize?code=true&state=abc" in m for m in sent), sent)
        s.write("GOODCODE#123")
        self.assertTrue(s.wait(10))
        self.assertEqual(done, [0])
        self.assertTrue(any("Login successful." in m for m in sent))
        self.assertFalse(any("GOODCODE" in m for m in sent))  # pasted code is never echoed back

    def test_gateway_flow_intercepts_the_code(self):
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        sent = []

        class Adapter:
            async def send(self, chat_id, content, reply_to=None, metadata=None):
                sent.append((chat_id, content))

        class Gateway:
            def __init__(self):
                self.adapter = Adapter()

            def _adapter_for_source(self, source):
                return self.adapter

        gw = Gateway()
        src = types.SimpleNamespace(platform=types.SimpleNamespace(value="discord"), chat_id="c1", user_id="42", thread_id=None)

        def dispatch(text):
            async def go():
                r = self.mod.on_dispatch(event=types.SimpleNamespace(text=text, source=src), gateway=gw, session_store=None)
                if r is None and text.startswith("/"):
                    return r, self.mod.command(text.split(maxsplit=1)[1] if " " in text else "")
                return r, None
            return asyncio.run_coroutine_threadsafe(go(), loop).result(10)

        r, reply = dispatch("/login")
        self.assertIsNone(r)
        self.assertIn("Starting Claude Code sign-in", reply)
        for _ in range(50):
            if any("Paste code" in m for _, m in sent):
                break
            time.sleep(0.1)
        self.assertTrue(any("oauth/authorize" in m for _, m in sent))
        r, _ = dispatch("GOODCODE#123")
        self.assertEqual(r, {"action": "skip", "reason": "claude login input"})
        for _ in range(100):
            if any("signed in" in m for _, m in sent):
                break
            time.sleep(0.1)
        self.assertTrue(any("Claude Code is signed in (claude.ai, max)." in m for _, m in sent), sent)
        self.assertFalse(any("GOODCODE" in m for _, m in sent))
        self.assertFalse(any("someone@example.com" in m for _, m in sent))
        self.assertEqual(dispatch("hello again")[0], None)  # login over: normal messages flow to the agent
        _, reply = dispatch("/login status")
        self.assertIn("signed in", reply)
        _, reply = dispatch("/login logout")
        self.assertIn("not signed in", reply)
        loop.call_soon_threadsafe(loop.stop)

    def test_login_is_denied_without_allowlist(self):
        os.environ["CLAUDE_BRIDGE_LOGIN_USERS"] = ""
        src = types.SimpleNamespace(platform="discord", chat_id="c1", user_id="7", thread_id=None)

        async def go():
            self.mod.on_dispatch(event=types.SimpleNamespace(text="/login", source=src), gateway=object())
            return self.mod.command("")
        reply = asyncio.run(go())
        self.assertIn("CLAUDE_BRIDGE_LOGIN_USERS=7", reply)
        self.assertEqual(self.mod.PENDING, {})


if __name__ == "__main__":
    unittest.main()
