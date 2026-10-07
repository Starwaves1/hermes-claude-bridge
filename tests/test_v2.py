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
import json, os, sys, time
d = os.environ["FAKE_DIR"]
n = len([f for f in os.listdir(d) if f.endswith(".args")])
args = sys.argv[1:]
open(f"{{d}}/{{n}}.args", "w").write("\\n".join(args))
if "--append-system-prompt-file" in args:
    open(f"{{d}}/{{n}}.append", "w").write(open(args[args.index("--append-system-prompt-file") + 1]).read())
open(f"{{d}}/{{n}}.stdin", "w").write(sys.stdin.readline())
open(f"{{d}}/{{n}}.pid", "w").write(str(os.getpid()))
def opt(name):
    p = f"{{d}}/{{name}}-{{n}}"
    return open(p).read() if os.path.exists(p) else None
if opt("sleep"):
    time.sleep(float(opt("sleep")))
if opt("pre"):
    reqs = [l for l in opt("pre").splitlines() if l.strip()]
    for l in reqs:
        sys.stdout.write(l + "\\n")
    sys.stdout.flush()
    got = [sys.stdin.readline() for l in reqs if json.loads(l)["type"] == "control_request"]
    open(f"{{d}}/{{n}}.responses", "w").write("".join(got))
import uuid
sid = args[args.index("--session-id") + 1] if "--session-id" in args else args[args.index("--resume") + 1] if "--resume" in args else "none"
if "--fork-session" in args:
    sid = str(uuid.uuid4())
open(f"{{d}}/{{n}}.sid", "w").write(sid)
rp = f"{{d}}/reply-{{n}}.jsonl"
sys.stdout.write(open(rp if os.path.exists(rp) else f"{{d}}/reply.jsonl").read().replace("__SID__", sid))
sys.stdout.flush()
open(f"{{d}}/{{n}}.replied", "w").write(str(time.time()))
if opt("post"):
    time.sleep(float(opt("postdelay") or 0))
    sys.stdout.write(opt("post").replace("__SID__", sid))
    sys.stdout.flush()
    open(f"{{d}}/{{n}}.posted", "w").write(str(time.time()))
if opt("exit"):
    sys.exit(int(opt("exit")))
sys.stdin.read()
open(f"{{d}}/{{n}}.eof", "w").write(str(time.time()))
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
    import time
    sys.stdout.write("\\r\\nchecking " + code[:3]); sys.stdout.flush(); time.sleep(0.6)
    sys.stdout.write(code[3:] + "\\r\\n")
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

TOKEN = "test-control-token"
USAGE = {"input_tokens": 3, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 100, "output_tokens": 20}


def turn_events(content="", calls=None, session="__SID__", extra=None, is_error=False, result=None):
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
        self.saved = {k: getattr(cb, k) for k in ("CLAUDE_BIN", "child_env", "harness_env", "STORE", "WORKDIR", "SCRATCH_DIR", "HEARTBEAT_SECONDS")}
        self.saved_token = cb.CONTROL["token"]
        cb.CONTROL["token"] = TOKEN
        self.saved_help = dict(cb._HELP)
        cb.CLAUDE_BIN = str(FAKE)
        orig_env = self.saved["child_env"]
        cb.child_env = lambda: {**orig_env(), "FAKE_DIR": str(self.dir)}
        orig_harness = self.saved["harness_env"]
        cb.harness_env = lambda: {**orig_harness(), "FAKE_DIR": str(self.dir)}
        cb.STORE = cb.SessionStore(self.dir / "sessions.json")
        cb.WORKDIR = self.dir / "workspace"
        cb.SCRATCH_DIR = self.dir / "scratch"
        cb._HELP["text"] = "--system-prompt-snapshot <on|off>"

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(cb, k, v)
        cb.CONTROL["token"] = self.saved_token
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
        for flag, val in (("--input-format", "stream-json"), ("--output-format", "stream-json"), ("--permission-mode", "auto"), ("--permission-prompt-tool", "stdio"), ("--model", "fable")):
            self.assertEqual(argv[argv.index(flag) + 1], val)
        for banned in ("--safe-mode", "--strict-mcp-config", "--tools", "--disallowedTools", "--bare", "--setting-sources", "--no-session-persistence", "--system-prompt-file", "--permission-prompts", "--max-turns"):
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

    ME = {"platform": "discord", "chat_id": "c1", "user_id": "42"}

    def test_rotated_key_after_compression_keeps_the_session(self):
        self.reply(turn_events("Here is the long, detailed plan you asked for."))
        _, out = self.post(self.body([{"role": "user", "content": "plan it"}], key="sess-a", claude_bridge_origin=self.ME))
        sid = cb.STORE.get("sess-a")["uuid"]
        hist = [{"role": "user", "content": "[summary of earlier turns]"}, self.as_hermes(out["choices"][0]["message"]), {"role": "user", "content": "next step?"}]
        self.post(self.body(hist, key="sess-b", claude_bridge_origin=self.ME))
        argv, lines, _ = self.call(1)
        self.assertEqual(argv[argv.index("--resume") + 1], sid)
        self.assertEqual(self.texts(lines[0]), "next step?")
        self.assertIsNone(cb.STORE.get("sess-a"))
        self.assertEqual(cb.STORE.get("sess-b")["uuid"], sid)

    def test_adoption_never_crosses_people_or_unknown_origins(self):
        self.reply(turn_events("Here is the long, detailed plan you asked for."))
        _, out = self.post(self.body([{"role": "user", "content": "plan it"}], key="sess-a", claude_bridge_origin=self.ME))
        reply = self.as_hermes(out["choices"][0]["message"])
        hist = [{"role": "user", "content": "plan it"}, reply, {"role": "user", "content": "mine now"}]
        self.post(self.body(hist, key="sess-x", claude_bridge_origin={**self.ME, "user_id": "77"}))  # someone else, same chat
        self.assertIn("--session-id", self.call(1)[0])
        self.post(self.body(hist, key="sess-y"))  # origin unknown
        self.assertIn("--session-id", self.call(2)[0])
        later = hist + [{"role": "assistant", "content": "a newer reply from somewhere else entirely"}, {"role": "user", "content": "go"}]
        self.post(self.body(later, key="sess-z", claude_bridge_origin=self.ME))  # match must be the LAST assistant message
        self.assertIn("--session-id", self.call(3)[0])
        self.assertIsNotNone(cb.STORE.get("sess-a"))

    def test_one_claude_session_is_never_resumed_twice_at_once(self):
        self.reply(turn_events("a1"))
        _, out = self.post(self.body([{"role": "user", "content": "q1"}]))
        sid = cb.STORE.get("conv-1")["uuid"]
        hold = cb.STORE.turn_lock("uuid:" + sid)
        hold.acquire()
        saved = cb.QUEUE_WAIT
        cb.QUEUE_WAIT = 0.3
        try:
            hist = [{"role": "user", "content": "q1"}, self.as_hermes(out["choices"][0]["message"]), {"role": "user", "content": "q2"}]
            status, out = self.post(self.body(hist))
        finally:
            cb.QUEUE_WAIT = saved
            hold.release()
        self.assertEqual(status, 503)
        self.assertEqual(out["error"]["code"], "session_busy")

    def test_hermes_fork_runs_in_a_fork_and_leaves_the_user_session_alone(self):
        self.reply(turn_events("Here is the long, detailed plan you asked for."), 0)
        _, out = self.post(self.body([{"role": "user", "content": "plan it"}]))
        user = cb.STORE.get("conv-1")
        review = [{"role": "user", "content": "plan it"}, self.as_hermes(out["choices"][0]["message"]), {"role": "user", "content": "Review the conversation above and update memory."}]
        self.reply(turn_events("", [{"name": "memory", "arguments": {"x": "likes plans"}}]), 1)
        fb = self.body(review, key="fork:conv-1:abc", claude_bridge_fork={"parent": "conv-1"}, claude_bridge_origin=self.ME)
        _, fout = self.post(fb)
        argv, lines, _ = self.call(1)
        self.assertEqual(argv[argv.index("--resume") + 1], user["uuid"])
        self.assertIn("--fork-session", argv)
        self.assertEqual(self.texts(lines[0]), "Review the conversation above and update memory.")
        fork_sid = (self.dir / "1.sid").read_text()
        self.assertEqual(cb.STORE.get("fork:conv-1:abc")["uuid"], fork_sid)
        self.assertEqual(cb.STORE.get("fork:conv-1:abc")["origin"], {})  # forks never ask the user for approvals
        after = cb.STORE.get("conv-1")
        self.assertEqual((after["uuid"], after["last"], after["fed"], after["turns"]), (user["uuid"], user["last"], user["fed"], user["turns"]))
        call = fout["choices"][0]["message"]["tool_calls"][0]
        self.reply(turn_events("saved"), 2)
        fb["messages"] += [self.as_hermes(fout["choices"][0]["message"]), {"role": "tool", "tool_call_id": call["id"], "content": "ok"}]
        self.post(fb)
        argv, _, _ = self.call(2)
        self.assertEqual(argv[argv.index("--resume") + 1], fork_sid)
        self.assertNotIn("--fork-session", argv)

    def test_long_history_bootstrap_is_capped(self):
        saved = cb.BOOTSTRAP_MAX_CHARS
        cb.BOOTSTRAP_MAX_CHARS = 2000
        try:
            hist = []
            for i in range(40):
                hist += [{"role": "user", "content": f"question {i} " + "x" * 200}, {"role": "assistant", "content": f"answer {i} " + "y" * 200}]
            self.reply(turn_events("ok"))
            self.post(self.body(hist + [{"role": "user", "content": "latest"}], key="long"))
        finally:
            cb.BOOTSTRAP_MAX_CHARS = saved
        text = self.texts(self.call(0)[1][0])
        self.assertLess(len(text), 3000)
        self.assertIn("older message(s) omitted", text)
        self.assertIn("answer 39", text)
        self.assertNotIn("question 0 ", text)
        self.assertTrue(text.rstrip().endswith("latest"))

    def test_error_after_heartbeat_keeps_its_status_in_the_stream(self):
        self.reply(turn_events(is_error=True, result="Claude usage limit reached. Your limit will reset at 3pm"))
        (self.dir / "sleep-0").write_text("1.0")
        cb.HEARTBEAT_SECONDS = 0.3
        status, raw = self.post(self.body([{"role": "user", "content": "x"}], stream=True), raw=True)
        self.assertEqual(status, 200)
        err = [json.loads(l[6:]) for l in raw.decode().split("\n\n") if l.startswith("data: {\"error\"")][0]["error"]
        self.assertEqual((err["status"], err["type"], err["code"]), (429, "rate_limit_error", "claude_usage_limit"))
        self.assertTrue(err["message"].startswith("429 Too Many Requests: Claude usage limit reached"))

    def test_approval_endpoints_need_the_control_token(self):
        url = f"http://127.0.0.1:{self.port}/v1/approvals?after=0&wait=0"
        for headers in ({}, {"X-Bridge-Token": "wrong"}):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5)
            self.assertEqual(cm.exception.code, 401)
            cm.exception.close()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/approvals/anything", data=b'{"user_id":"42","text":"y"}', headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 401)
        cm.exception.close()
        with urllib.request.urlopen(urllib.request.Request(url, headers={"X-Bridge-Token": TOKEN}), timeout=5) as r:
            self.assertIn("instance", json.load(r))
        cb.CONTROL["token"] = ""
        cb.APPROVALS.last_poll = time.time()
        self.assertEqual(cb.APPROVALS.ask("z", {"tool_name": "Bash", "input": {}}, self.ME, timeout=1)["message"], "no approval channel")

    def test_child_env_inherits_but_strips_billing_and_control(self):
        over = {"ANTHROPIC_API_KEY": "sk", "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "x", "CLAUDE_BRIDGE_LOGIN_USERS": "1",
                "HTTPS_PROXY": "http://proxy:3128", "SSH_AUTH_SOCK": "/tmp/agent.sock", "SSL_CERT_FILE": "/etc/ca.pem", "GH_TOKEN": "gh"}
        saved = {k: os.environ.get(k) for k in over}
        os.environ.update(over)
        try:
            env = self.saved["harness_env"]()
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        for gone in ("ANTHROPIC_API_KEY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_BRIDGE_LOGIN_USERS"):
            self.assertNotIn(gone, env)
        for kept in ("HTTPS_PROXY", "SSH_AUTH_SOCK", "SSL_CERT_FILE", "GH_TOKEN"):
            self.assertEqual(env[kept], over[kept])
        self.assertIn(f"{cb.HOME}/.local/bin", env["PATH"].split(":"))
        self.assertNotIn(TOKEN, json.dumps(env))

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
        entry = cb.STORE.get("conv-1")  # recorded at init, before the failure
        self.assertEqual(entry["last"], "")
        self.reply(turn_events("hello"))
        status, out = self.post(self.body([{"role": "user", "content": "x"}]))
        self.assertEqual(status, 200)
        argv, lines, _ = self.call(1)
        self.assertEqual(argv[argv.index("--resume") + 1], entry["uuid"])  # the retry resumes the started session
        self.assertEqual(self.texts(lines[0]), cb.RESEND_NUDGE)  # and does not deliver "x" a second time

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

    # -- approvals --------------------------------------------------------

    def control(self, rid, tool, inp, **extra):
        return json.dumps({"type": "control_request", "request_id": rid, "request": {"subtype": "can_use_tool", "tool_name": tool, "display_name": tool, "input": inp, "tool_use_id": "toolu_" + rid, **extra}})

    def answer_when_asked(self, replies, user="42"):
        """Play the Hermes plugin: long-poll the bridge and answer each prompt."""
        seen = []

        def run():
            after = 0
            for text in replies:
                while True:
                    with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/approvals?after={after}&wait=5", headers={"X-Bridge-Token": TOKEN}), timeout=10) as r:
                        data = json.load(r)
                    after = data["seq"]
                    pending = [i for i in data["approvals"] if i["state"] == "pending"]
                    if pending:
                        break
                item = pending[0]
                seen.append(item)
                req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/approvals/{item['id']}", data=json.dumps({"user_id": user, "platform": "discord", "text": text}).encode(), headers={"Content-Type": "application/json", "X-Bridge-Token": TOKEN})
                with urllib.request.urlopen(req, timeout=10) as r:
                    seen.append(json.load(r))
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t, seen

    def origin_body(self, text, **kw):
        return self.body([{"role": "user", "content": text}], claude_bridge_origin={"platform": "discord", "chat_id": "c1", "user_id": "42"}, **kw)

    def test_permission_request_relayed_and_answered(self):
        cmd = {"command": "rm -rf build/", "description": "Remove build output"}
        (self.dir / "pre-0").write_text(self.control("r1", "Bash", cmd) + "\n" + self.control("r2", "Write", {"file_path": "/etc/hosts", "content": "x"}) + "\n")
        self.reply(turn_events("cleaned up"))
        cb.APPROVALS.last_poll = time.time()
        t, seen = self.answer_when_asked(["y", "use /tmp instead"])
        status, out = self.post(self.origin_body("clean the build"))
        t.join(10)
        self.assertEqual(status, 200)
        self.assertIn("Claude Code wants to use Bash:\nrm -rf build/\n(Remove build output)\nReply y / n, or give a reason to deny.", seen[0]["text"])
        self.assertEqual((seen[0]["platform"], seen[0]["chat_id"], seen[0]["user_id"]), ("discord", "c1", "42"))
        self.assertEqual(seen[1], {"ok": True, "decision": "allow"})
        self.assertIn("/etc/hosts", seen[2]["text"])
        responses = [json.loads(l)["response"] for l in (self.dir / "0.responses").read_text().splitlines()]
        self.assertEqual(responses[0], {"subtype": "success", "request_id": "r1", "response": {"behavior": "allow", "updatedInput": cmd}})
        self.assertEqual(responses[1]["response"], {"behavior": "deny", "message": "use /tmp instead"})
        self.assertTrue((self.dir / "0.eof").exists())  # stdin closed after the result

    def test_question_relayed_and_answered(self):
        q = {"questions": [{"question": "Which color?", "header": "Color", "options": [{"label": "Red", "description": "warm"}, {"label": "Blue", "description": "cool"}], "multiSelect": False}]}
        (self.dir / "pre-0").write_text(self.control("q1", "AskUserQuestion", q, requires_user_interaction=True) + "\n")
        self.reply(turn_events("blue it is"))
        cb.APPROVALS.last_poll = time.time()
        t, seen = self.answer_when_asked(["2"])
        status, _ = self.post(self.origin_body("pick a color"))
        t.join(10)
        self.assertEqual(status, 200)
        self.assertIn("Which color?\n   1) Red — warm\n   2) Blue — cool", seen[0]["text"])
        resp = json.loads((self.dir / "0.responses").read_text())["response"]["response"]
        self.assertEqual(resp, {"behavior": "allow", "updatedInput": {**q, "answers": {"Which color?": "Blue"}}})

    def test_no_approval_channel_denies_at_once(self):
        (self.dir / "pre-0").write_text(self.control("r1", "Bash", {"command": "make"}) + "\n")
        self.reply(turn_events("could not build"))
        cb.APPROVALS.last_poll = 0.0
        status, _ = self.post(self.origin_body("build"))
        self.assertEqual(status, 200)
        resp = json.loads((self.dir / "0.responses").read_text())["response"]["response"]
        self.assertEqual(resp, {"behavior": "deny", "message": "no approval channel"})
        cb.APPROVALS.last_poll = time.time()
        b = self.body([{"role": "user", "content": "cli turn"}], key="cli")  # no origin: CLI or cron
        (self.dir / "pre-1").write_text(self.control("r2", "Bash", {"command": "make"}) + "\n")
        self.post(b)
        self.assertEqual(json.loads((self.dir / "1.responses").read_text())["response"]["response"]["message"], "no approval channel")

    def test_background_task_detaches_and_follow_up_goes_to_the_chat(self):
        bg = [{"type": "system", "subtype": "task_started", "task_id": "t1", "is_backgrounded": True, "description": "build", "session_id": "s"},
              {"type": "system", "subtype": "task_started", "task_id": "amb", "is_backgrounded": True, "ambient": True, "description": "watcher", "session_id": "s"}]
        self.reply(turn_events("started the build in the background", extra=bg))
        later = [{"type": "system", "subtype": "task_notification", "task_id": "t1", "status": "completed", "summary": "ok", "session_id": "s"},
                 {"type": "system", "subtype": "background_tasks_changed", "tasks": [{"task_id": "amb", "task_type": "local_bash", "description": "watcher", "ambient": True}], "session_id": "s"}]
        (self.dir / "post-0").write_text("".join(json.dumps(e) + "\n" for e in later) + turn_events("build finished: 0 errors"))
        (self.dir / "postdelay-0").write_text("1.5")
        cb.APPROVALS.last_poll = time.time()
        t0 = time.time()
        status, out = self.post(self.origin_body("build it"))
        self.assertEqual(status, 200)
        self.assertLess(time.time() - t0, 1.4)  # the reply did not wait for the background task
        self.assertEqual(out["choices"][0]["message"]["content"], "started the build in the background")
        notes = []
        for _ in range(60):
            notes = [i for i in cb.APPROVALS.poll(0, 0)[1] if i["state"] == "notice" and "0 errors" in i["text"]]
            if notes and (self.dir / "0.eof").exists():
                break
            time.sleep(0.1)
        self.assertEqual(notes[0]["text"], "[Background follow-up] build finished: 0 errors")
        self.assertEqual((notes[0]["platform"], notes[0]["chat_id"]), ("discord", "c1"))
        self.assertTrue((self.dir / "0.eof").exists())  # child closed once the task was done

    def test_next_turn_stops_running_background_work(self):
        bg = [{"type": "system", "subtype": "task_started", "task_id": "t1", "is_backgrounded": True, "description": "server", "session_id": "s"}]
        self.reply(turn_events("dev server is up", extra=bg), 0)
        cb.APPROVALS.last_poll = time.time()
        _, out = self.post(self.origin_body("start the server"))
        pid = int((self.dir / "0.pid").read_text())
        self.reply(turn_events("stopped it"), 1)
        hist = [{"role": "user", "content": "start the server"}, self.as_hermes(out["choices"][0]["message"]), {"role": "user", "content": "now stop"}]
        status, out2 = self.post(self.body(hist, claude_bridge_origin={"platform": "discord", "chat_id": "c1", "user_id": "42"}))
        self.assertEqual(status, 200)
        self.assertIn("--resume", self.call(1)[0])
        with self.assertRaises(OSError):
            os.kill(pid, 0)
        self.assertTrue(any("was stopped" in i["text"] for i in cb.APPROVALS.poll(0, 0)[1] if i["state"] == "notice"))

    def test_models_advertise_a_large_window(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/v1/models") as r:
            self.assertEqual(json.load(r)["data"][0]["context_length"], 1000000)


class ApprovalRules(unittest.TestCase):
    BASH = {"tool_name": "Bash", "input": {"command": "ls"}}

    def setUp(self):
        self.saved_token = cb.CONTROL["token"]
        cb.CONTROL["token"] = TOKEN

    def tearDown(self):
        cb.CONTROL["token"] = self.saved_token

    def test_reply_parsing_is_exact(self):
        for yes in ("y", "yes", " Y ", "YES\n"):
            self.assertEqual(cb.decide(self.BASH, yes), {"behavior": "allow", "updatedInput": {"command": "ls"}})
        for no in ("n", "no", " No "):
            self.assertEqual(cb.decide(self.BASH, no)["behavior"], "deny")
        self.assertEqual(cb.decide(self.BASH, "yes please"), {"behavior": "deny", "message": "yes please"})
        self.assertEqual(cb.decide(self.BASH, "  not on prod  "), {"behavior": "deny", "message": "not on prod"})

    def test_question_answers(self):
        q = {"tool_name": "AskUserQuestion", "input": {"questions": [{"question": "Q?", "options": [{"label": "A"}, {"label": "B"}], "multiSelect": True}]}}
        self.assertEqual(cb.decide(q, "1, 2")["updatedInput"]["answers"], {"Q?": "A, B"})
        self.assertEqual(cb.decide(q, "neither, use C")["updatedInput"]["answers"], {"Q?": "neither, use C"})

    def test_long_input_is_truncated(self):
        text = cb.describe_request({"tool_name": "Bash", "input": {"command": "echo " + "x" * 2000}})
        self.assertLess(len(text), 700)
        self.assertIn("…", text)

    def test_only_owner_or_admin_may_answer_and_timeout_denies(self):
        ap = cb.Approvals()
        ap.last_poll = time.time()
        origin = {"platform": "discord", "chat_id": "c", "user_id": "42"}
        got = {}
        t = threading.Thread(target=lambda: got.setdefault("d", ap.ask("r", self.BASH, origin, timeout=5)))
        t.start()
        for _ in range(50):
            if "r" in ap.items:
                break
            time.sleep(0.02)
        self.assertEqual(ap.answer("r", "99", "discord", "y")[0], 403)
        saved = os.environ.get("CLAUDE_BRIDGE_LOGIN_USERS")
        os.environ["CLAUDE_BRIDGE_LOGIN_USERS"] = "discord:7"
        try:
            self.assertEqual(ap.answer("r", "7", "discord", "n"), (200, {"ok": True, "decision": "deny"}))
        finally:
            if saved is None:
                os.environ.pop("CLAUDE_BRIDGE_LOGIN_USERS", None)
            else:
                os.environ["CLAUDE_BRIDGE_LOGIN_USERS"] = saved
        t.join(5)
        self.assertEqual(got["d"]["behavior"], "deny")
        self.assertEqual(ap.answer("r", "42", "discord", "y")[0], 404)  # already answered
        self.assertEqual(ap.ask("r2", self.BASH, origin, timeout=0.2), {"behavior": "deny", "message": "no answer from user"})
        self.assertEqual(ap.poll(0, 0)[1][-1]["state"], "timeout")

    def test_cancel_returns_none(self):
        ap = cb.Approvals()
        ap.last_poll = time.time()
        threading.Timer(0.2, ap.cancel, args=("r",)).start()
        self.assertIsNone(ap.ask("r", self.BASH, {"platform": "discord", "chat_id": "c", "user_id": "1"}, timeout=5))


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

    def test_plugin_relays_approvals_and_consumes_the_answer(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), cb.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.mod.BRIDGE = f"http://127.0.0.1:{srv.server_address[1]}/v1"
        self.mod._token = lambda: TOKEN
        saved_token = cb.CONTROL["token"]
        cb.CONTROL["token"] = TOKEN
        self.addCleanup(cb.CONTROL.__setitem__, "token", saved_token)
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        sent = []

        class Adapter:
            async def send(self, chat_id, content, reply_to=None, metadata=None):
                sent.append((chat_id, content))

        class Plat:
            value = "discord"
        plat = Plat()
        gw = types.SimpleNamespace(adapters={plat: Adapter()})
        stop = threading.Event()
        self.mod._GATEWAY.update(gateway=gw, loop=loop, poller=True)
        threading.Thread(target=self.mod.poll_approvals, args=(stop, 1), daemon=True).start()
        cb.APPROVALS.last_poll = time.time()
        got = {}
        t = threading.Thread(target=lambda: got.setdefault("d", cb.APPROVALS.ask("pr1", {"tool_name": "Bash", "input": {"command": "make deploy"}}, {"platform": "discord", "chat_id": "c1", "user_id": "42"}, timeout=10)))
        t.start()
        for _ in range(100):
            if any("make deploy" in m for _, m in sent):
                break
            time.sleep(0.05)
        self.assertTrue(any("make deploy" in m for _, m in sent), sent)

        def dispatch(text, uid):
            src = types.SimpleNamespace(platform=plat, chat_id="c1", user_id=uid, thread_id=None)
            async def go():
                return self.mod.on_dispatch(event=types.SimpleNamespace(text=text, source=src), gateway=gw)
            return asyncio.run_coroutine_threadsafe(go(), loop).result(10)

        os.environ["CLAUDE_BRIDGE_LOGIN_USERS"] = ""
        self.assertIsNone(dispatch("y", "99"))  # someone else in the chat: not an answer
        self.assertIsNone(dispatch("/status", "42"))  # slash commands pass through
        self.assertEqual(dispatch("not today", "42"), {"action": "skip", "reason": "claude approval answer"})
        t.join(5)
        self.assertEqual(got["d"], {"behavior": "deny", "message": "not today"})
        for _ in range(50):
            if any(m.endswith("] Denied.") for _, m in sent):
                break
            time.sleep(0.05)
        self.assertTrue(any(m.endswith("] Denied.") for _, m in sent), sent)
        self.assertIsNone(dispatch("hello", "42"))  # nothing pending: normal message
        stop.set()
        srv.shutdown()
        loop.call_soon_threadsafe(loop.stop)

    def test_split_code_echo_is_still_redacted(self):
        sent = []
        s = self.mod.relay.LoginSession([str(FAKE_LOGIN), "auth", "login", "--claudeai"], self.mod.claude_env(), send=sent.append, batch=0.2).start()
        for _ in range(50):
            if any("Paste code" in m for m in sent):
                break
            time.sleep(0.1)
        s.write("GOODCODE#123")
        self.assertTrue(s.wait(10))
        joined = "\n".join(sent)
        self.assertIn("••••••", joined)
        for leak in ("GOO", "DCODE", "#123"):
            self.assertNotIn(leak, joined)

    def _gateway(self):
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        self.addCleanup(loop.call_soon_threadsafe, loop.stop)
        sent = []

        class Adapter:
            async def send(self, chat_id, content, reply_to=None, metadata=None):
                sent.append((chat_id, content))

        class Plat:
            value = "discord"
        plat = Plat()
        gw = types.SimpleNamespace(adapters={plat: Adapter()}, _adapter_for_source=lambda src: gw.adapters[plat])

        def dispatch(text, uid="42", command=False):
            src = types.SimpleNamespace(platform=plat, chat_id="c1", user_id=uid, thread_id=None)

            async def go():
                r = self.mod.on_dispatch(event=types.SimpleNamespace(text=text, source=src), gateway=gw)
                if command and r is None:
                    return r, self.mod.command(text.split(maxsplit=1)[1] if " " in text else "")
                return r, None
            return asyncio.run_coroutine_threadsafe(go(), loop).result(10)
        self.mod._GATEWAY.update(gateway=gw, loop=loop, poller=True)
        return dispatch, sent

    def test_message_after_a_failed_sign_in_is_held_back_once(self):
        dispatch, sent = self._gateway()
        dispatch("/login", command=True)
        for _ in range(50):
            if any("Paste code" in m for _, m in sent):
                break
            time.sleep(0.1)
        self.assertEqual(dispatch("WRONGCODE")[0], {"action": "skip", "reason": "claude login input"})
        for _ in range(100):
            if any("not signed in" in m for _, m in sent):
                break
            time.sleep(0.1)
        self.assertEqual(dispatch("LATECODE99")[0], {"action": "skip", "reason": "claude login ended"})
        self.assertTrue(any("held back" in m for _, m in sent))
        self.assertIsNone(dispatch("hello agent")[0])  # only once

    def test_several_open_approvals_need_an_id(self):
        o = types.SimpleNamespace(platform="discord", chat_id="c1", user_id="42", thread_id="")
        self.mod.APPROVALS[("discord", "c1")] = [
            {"id": "a", "short": "3", "user_id": "42", "tool": "Bash"},
            {"id": "b", "short": "4", "user_id": "42", "tool": "Write"},
            {"id": "c", "short": "5", "user_id": "77", "tool": "Bash"},
        ]
        self.addCleanup(self.mod.APPROVALS.clear)
        self.assertEqual(self.mod.pick_approval(o, "y"), (None, "y", True))
        self.assertEqual(self.mod.pick_approval(o, "y 4")[:2], (self.mod.APPROVALS[("discord", "c1")][1], "y"))
        self.assertEqual(self.mod.pick_approval(o, "3 not on prod")[1], "not on prod")
        self.assertIsNone(self.mod.pick_approval(o, "n 5")[0])  # someone else's request, and 42 is no admin
        self.mod.APPROVALS[("discord", "c1")] = [{"id": "a", "short": "3", "user_id": "42", "tool": "Bash"}, {"id": "c", "short": "5", "user_id": "77", "tool": "Bash"}]
        self.assertEqual(self.mod.pick_approval(o, "3 is fine")[:2], (self.mod.APPROVALS[("discord", "c1")][0], "3 is fine"))  # one own item: bare reply

    def test_command_uses_the_session_context_when_hermes_sets_it(self):
        dispatch, _ = self._gateway()
        sc = types.ModuleType("gateway.session_context")
        env = {"HERMES_SESSION_PLATFORM": "discord", "HERMES_SESSION_CHAT_ID": "c9", "HERMES_SESSION_USER_ID": "43"}
        sc.get_session_env = lambda name, default="": env.get(name, default)
        gwmod = types.ModuleType("gateway")
        gwmod.session_context = sc
        saved = {k: sys.modules.get(k) for k in ("gateway", "gateway.session_context")}
        sys.modules["gateway"], sys.modules["gateway.session_context"] = gwmod, sc
        try:
            self.assertIn("not enabled for your user id (43)", self.mod.command("status"))
            env["HERMES_SESSION_USER_ID"] = "42"
            self.assertIn("signed in", self.mod.command("status"))
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def test_long_messages_are_split_for_chat_limits(self):
        text = "\n".join(f"line {i} " + "z" * 90 for i in range(60)) + "\n" + "w" * 5000
        parts = self.mod.chunks(text)
        self.assertTrue(all(len(p) <= 1900 for p in parts))
        self.assertEqual("".join(parts).replace("\n", ""), text.replace("\n", ""))

    def test_poller_starts_over_when_the_bridge_restarts(self):
        calls = []
        script = [(200, {"instance": "A", "seq": 7, "approvals": []}), (200, {"instance": "B", "seq": 2, "approvals": []}), (200, {"instance": "B", "seq": 2, "approvals": []})]
        stop = threading.Event()

        def fake(method, path, body=None, timeout=10):
            calls.append(path)
            if len(calls) >= len(script):
                stop.set()
            return script[min(len(calls), len(script)) - 1]
        self.mod.bridge_call = fake
        self.mod.poll_approvals(stop, 0)
        self.assertEqual(calls[:3], ["/approvals?after=0&wait=0", "/approvals?after=7&wait=0", "/approvals?after=0&wait=0"])

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
