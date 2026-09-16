"""Unit + loopback tests for claude_bridge.py. Run: python3 -m unittest tests/test_bridge.py

The HTTP tests point the bridge at a fake `claude` script that replays a
canned stream-json event stream, so nothing touches the real binary or the
subscription.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="bridge-test-"))
FAKE = TMP / "fake-claude.sh"
FAKE.write_text(
    "#!/bin/sh\n"
    "# fake claude: records argv + stdin, replays the canned event stream in FAKE_REPLY\n"
    "printf '%s\\n' \"$@\" > \"$FAKE_ARGS\"\n"
    "cat > \"$FAKE_STDIN\"\n"
    "cat \"$FAKE_REPLY\"\n"
    "if [ -f \"$FAKE_EXIT\" ]; then exit \"$(cat \"$FAKE_EXIT\")\"; fi\n"
    "exit 0\n"
)
FAKE.chmod(0o755)
os.environ["CLAUDE_BRIDGE_CLAUDE_BIN"] = str(FAKE)
os.environ["CLAUDE_BRIDGE_STATE_DIR"] = str(TMP / "state")
os.environ["CLAUDE_BRIDGE_WORKDIR"] = str(TMP / "work")
os.environ["CLAUDE_BRIDGE_LOG"] = str(TMP / "bridge.log")
sys.path.insert(0, str(ROOT))
import claude_bridge as cb  # noqa: E402

ARGS = TMP / "args.txt"
STDIN = TMP / "stdin.txt"
REPLY = TMP / "reply.jsonl"
EXIT = TMP / "exit.txt"


ORIG_CHILD_ENV = cb.child_env


def _fake_env(**over):
    base = ORIG_CHILD_ENV()
    base.update({"FAKE_ARGS": str(ARGS), "FAKE_STDIN": str(STDIN), "FAKE_REPLY": str(REPLY), "FAKE_EXIT": str(EXIT)})
    base.update(over)
    return base


cb.child_env = _fake_env  # the fake needs to know where to write

USAGE_LAST = {"input_tokens": 10, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 0, "output_tokens": 7}


# -- stream-json event builders (shapes copied from claude 2.1.259 output) --
def ev_init(model="claude-fable-5-1"):
    return {"type": "system", "subtype": "init", "model": model, "tools": ["StructuredOutput"], "apiKeySource": "none"}


def ev_rate():
    return {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {"five_hour": {"utilization": 0.07}, "seven_day": {"utilization": 0.5}}}}


def ev_start():
    return {"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}}


def ev_text_delta(text):
    return {"type": "stream_event", "event": {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": text}}}


def ev_assistant(blocks, usage=None):
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks, "usage": usage or USAGE_LAST}}


def ev_message_delta(usage=None):
    return {"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": usage or USAGE_LAST}}


def ev_stop():
    return {"type": "stream_event", "event": {"type": "message_stop"}}


def ev_user_tool_result(text):
    return {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": text}]}}


def ev_result(result, structured=None, is_error=False, usage=None, num_turns=1, model="claude-fable-5-1"):
    d = {
        "type": "result", "subtype": "error" if is_error else "success", "is_error": is_error, "result": result,
        "duration_api_ms": 1, "num_turns": num_turns, "session_id": "x",
        "usage": usage or {"input_tokens": 20, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 0, "output_tokens": 7},
        "modelUsage": {model: {}},
    }
    if structured is not None:
        d["structured_output"] = structured
    return d


def write_events(events):
    REPLY.write_text("".join(json.dumps(e) + "\n" for e in events))
    if EXIT.exists():
        EXIT.unlink()


def canned(result, structured=None, is_error=False, deltas=None):
    """A one-turn spawn: optional text deltas, then the final result event."""
    events = [ev_init(), ev_rate(), ev_start()]
    for d in deltas or []:
        events.append(ev_text_delta(d))
    blocks = [{"type": "text", "text": "".join(deltas)}] if deltas else []
    if structured is not None:
        blocks.append({"type": "tool_use", "id": "toolu_1", "name": "StructuredOutput", "input": structured})
    events += [ev_assistant(blocks), ev_message_delta(), ev_stop(), ev_result(result, structured, is_error)]
    write_events(events)


class Rendering(unittest.TestCase):
    def test_split_system_and_transcript(self):
        body = {
            "messages": [
                {"role": "system", "content": "SYS A"},
                {"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": "x"}}]},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{\"a\":1}"}}]},
                {"role": "tool", "tool_call_id": "c1", "name": "t", "content": "42"},
            ],
            "tools": [{"type": "function", "function": {"name": "t", "description": "does t", "parameters": {"type": "object", "properties": {"a": {"type": "integer"}}}}}],
        }
        system, prompt, mode, schema = cb.build_prompts(body)
        self.assertEqual(mode, "tools")
        self.assertIs(schema, cb.OUTPUT_SCHEMA)
        self.assertTrue(system.startswith("SYS A"))
        self.assertIn("### t", system)
        self.assertIn("StructuredOutput", system)  # the protocol names the one native tool
        self.assertIn("image attachment omitted", prompt)
        self.assertIn('<tool_result id="c1" name="t">', prompt)
        self.assertIn('"arguments": {"a": 1}', prompt)
        self.assertTrue(prompt.startswith(cb.TRANSCRIPT_PREAMBLE))
        self.assertEqual(cb.tool_name_set(body["tools"]), {"t"})

    def test_no_tools_when_tool_choice_none(self):
        body = {"messages": [{"role": "user", "content": "x"}], "tools": [{"function": {"name": "t"}}], "tool_choice": "none"}
        system, _, mode, _ = cb.build_prompts(body)
        self.assertEqual(mode, "text")
        self.assertNotIn("Tool calling protocol", system)

    def test_effort_and_model(self):
        self.assertEqual(cb.normalize_effort({"reasoning_effort": "xhigh"}), "xhigh")
        self.assertEqual(cb.normalize_effort({"reasoning": {"effort": "ultra"}}), "max")
        self.assertEqual(cb.normalize_effort({}), "medium")
        self.assertEqual(cb.normalize_model("claude-cli/fable"), "fable")
        self.assertEqual(cb.normalize_model("claude-fable-5-1[1m]"), "claude-fable-5-1[1m]")
        self.assertIsNone(cb.normalize_model("gpt-5"))

    def test_error_classification(self):
        self.assertEqual(cb.classify_error("Not logged in · Please run /login").status, 401)
        self.assertEqual(cb.classify_error("Claude usage limit reached. Your limit will reset at 3pm").status, 429)
        self.assertEqual(cb.classify_error("You've hit your limit · resets 5pm").status, 429)
        self.assertEqual(cb.classify_error("some other failure").status, 502)
        limit = cb.classify_error("Claude usage limit reached. Your limit will reset at 3pm").message.lower()
        for banned in ("credit", "quota", "billing", "funds", "payment", "afford"):
            self.assertNotIn(banned, limit)

    def test_child_env_is_clean(self):
        env = ORIG_CHILD_ENV()
        for k in env:
            self.assertFalse(k.startswith("ANTHROPIC"), k)
            self.assertFalse(k.startswith("CLAUDE"), k)
        self.assertEqual(env["USER"], cb.USER)

    def test_usage_prefers_last_call(self):
        run = cb.ClaudeRun()
        run.usage_total = {"input_tokens": 100, "cache_read_input_tokens": 500, "cache_creation_input_tokens": 60, "output_tokens": 90}
        self.assertEqual(cb.usage_from(run)["prompt_tokens"], 660)
        run.usage_last = {"input_tokens": 10, "cache_read_input_tokens": 200, "cache_creation_input_tokens": 30, "output_tokens": 40}
        u = cb.usage_from(run)
        self.assertEqual(u["prompt_tokens"], 240)
        self.assertEqual(u["prompt_tokens_details"]["cached_tokens"], 200)
        self.assertEqual(u["completion_tokens"], 90)  # whole spawn's output is what was paid for


class Loopback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), cb.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def post(self, body, raw=False):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer local-bridge"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
                return r.status, (data if raw else json.loads(data))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    @staticmethod
    def sse_events(raw):
        lines = [l for l in raw.decode().split("\n\n") if l.startswith("data: ")]
        assert lines[-1] == "data: [DONE]", lines[-1]
        return [json.loads(l[6:]) for l in lines[:-1]]

    TOOLS = [{"type": "function", "function": {"name": "terminal", "description": "run", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}}]

    def test_models(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/v1/models") as r:
            ids = [m["id"] for m in json.load(r)["data"]]
        self.assertIn("fable", ids)
        self.assertIn("claude-fable-5-1", ids)

    def test_plain_completion_and_argv(self):
        canned("hello there", deltas=["hello ", "there"])
        status, out = self.post({"model": "fable", "reasoning_effort": "medium", "messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertEqual(out["choices"][0]["message"]["content"], "hello there")
        self.assertEqual(out["choices"][0]["finish_reason"], "stop")
        self.assertEqual(out["usage"]["prompt_tokens"], 15)  # last call's usage, not the result total (30)
        argv = ARGS.read_text().splitlines()
        for flag in ("-p", "--safe-mode", "--tools", "--strict-mcp-config", "--no-session-persistence", "--system-prompt-file", "--include-partial-messages", "--verbose"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertNotIn("--json-schema", argv)
        self.assertNotIn("--bare", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "fable")
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")
        self.assertIn("<user>\nhi\n</user>", STDIN.read_text())

    def test_tool_call_roundtrip(self):
        canned('{"content":"checking","tool_calls":[{"name":"terminal","arguments":{"command":"date"}}]}', structured={"content": "checking", "tool_calls": [{"name": "terminal", "arguments": {"command": "date"}}]})
        body = {"model": "sonnet", "messages": [{"role": "user", "content": "what time is it"}], "tools": self.TOOLS}
        status, out = self.post(body)
        self.assertEqual(status, 200)
        msg = out["choices"][0]["message"]
        self.assertEqual(out["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "terminal")
        self.assertEqual(json.loads(msg["tool_calls"][0]["function"]["arguments"]), {"command": "date"})
        self.assertTrue(msg["tool_calls"][0]["id"].startswith("call_"))
        argv = ARGS.read_text().splitlines()
        self.assertIn("--json-schema", argv)

    def test_native_tool_call_is_intercepted(self):
        # What Opus 4.8 did on 2026-09-03: call the Hermes tool natively. Claude
        # Code would answer "No such tool available" and the model would then
        # tell the user its tools are gone. The bridge must take the first
        # message's tool_use and ignore everything after it.
        write_events([
            ev_init("claude-opus-4-8"), ev_rate(), ev_start(),
            ev_text_delta("one sec"),
            ev_assistant([{"type": "text", "text": "one sec"}]),
            ev_assistant([{"type": "tool_use", "id": "toolu_a", "name": "terminal", "input": {"command": "date"}}]),
            ev_message_delta(), ev_stop(),
            ev_user_tool_result("No such tool available: terminal"),
            ev_start(),
            ev_assistant([{"type": "tool_use", "id": "toolu_b", "name": "StructuredOutput", "input": {"content": "my tools are gone", "tool_calls": []}}]),
            ev_message_delta({"input_tokens": 500, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 9}), ev_stop(),
            ev_result('{"content":"my tools are gone","tool_calls":[]}', {"content": "my tools are gone", "tool_calls": []}, num_turns=2, model="claude-opus-4-8"),
        ])
        body = {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "what time is it"}], "tools": self.TOOLS}
        status, out = self.post(body)
        self.assertEqual(status, 200)
        msg = out["choices"][0]["message"]
        self.assertEqual(out["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(msg["content"], "one sec")
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "terminal")
        self.assertEqual(json.loads(msg["tool_calls"][0]["function"]["arguments"]), {"command": "date"})
        self.assertEqual(out["usage"]["prompt_tokens"], 15)  # first call's usage, the second turn never counted
        self.assertNotIn("gone", json.dumps(out))

    def test_unknown_native_name_is_not_intercepted(self):
        write_events([
            ev_init(), ev_start(),
            ev_assistant([{"type": "tool_use", "id": "toolu_a", "name": "bogus_tool", "input": {}}]),
            ev_message_delta(), ev_stop(),
            ev_user_tool_result("No such tool available: bogus_tool"),
            ev_start(),
            ev_assistant([{"type": "tool_use", "id": "toolu_b", "name": "StructuredOutput", "input": {"content": "fine", "tool_calls": []}}]),
            ev_message_delta(), ev_stop(),
            ev_result('{"content":"fine","tool_calls":[]}', {"content": "fine", "tool_calls": []}, num_turns=2),
        ])
        body = {"model": "fable", "messages": [{"role": "user", "content": "x"}], "tools": self.TOOLS}
        status, out = self.post(body)
        self.assertEqual(status, 200)
        self.assertEqual(out["choices"][0]["message"]["content"], "fine")
        self.assertEqual(out["choices"][0]["finish_reason"], "stop")

    def test_streaming_tools_mode(self):
        canned("streamed answer", structured={"content": "streamed answer", "tool_calls": []})
        body = {"model": "fable", "stream": True, "stream_options": {"include_usage": True}, "messages": [{"role": "user", "content": "x"}], "tools": [{"function": {"name": "t", "parameters": {}}}]}
        status, raw = self.post(body, raw=True)
        self.assertEqual(status, 200)
        events = self.sse_events(raw)
        text = "".join(e["choices"][0]["delta"].get("content") or "" for e in events if e["choices"])
        self.assertEqual(text, "streamed answer")
        self.assertEqual([e for e in events if e["choices"]][-1]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(events[-1]["usage"]["completion_tokens"], 7)

    def test_streaming_text_mode_forwards_deltas_once(self):
        canned("streamed answer", deltas=["stre", "amed ", "answer"])
        body = {"model": "fable", "stream": True, "messages": [{"role": "user", "content": "x"}]}
        status, raw = self.post(body, raw=True)
        self.assertEqual(status, 200)
        events = self.sse_events(raw)
        pieces = [e["choices"][0]["delta"].get("content") for e in events if e["choices"] and e["choices"][0]["delta"].get("content")]
        self.assertEqual(pieces, ["stre", "amed ", "answer"])  # live deltas, no re-send of the final text
        self.assertEqual(events[0]["choices"][0]["delta"].get("role"), "assistant")
        self.assertEqual([e for e in events if e["choices"]][-1]["choices"][0]["finish_reason"], "stop")

    def test_streaming_heartbeat_after_silence(self):
        # a slow spawn: nothing on stdout for a while, then the answer
        slow = TMP / "slow-claude.sh"
        slow.write_text("#!/bin/sh\ncat > /dev/null\nsleep 1.3\ncat \"$FAKE_REPLY\"\n")
        slow.chmod(0o755)
        canned("late", deltas=["late"])
        old_bin, old_hb = cb.CLAUDE_BIN, cb.HEARTBEAT_SECONDS
        cb.CLAUDE_BIN, cb.HEARTBEAT_SECONDS = str(slow), 0.4
        try:
            status, raw = self.post({"model": "fable", "stream": True, "messages": [{"role": "user", "content": "x"}]}, raw=True)
        finally:
            cb.CLAUDE_BIN, cb.HEARTBEAT_SECONDS = old_bin, old_hb
        self.assertEqual(status, 200)
        events = self.sse_events(raw)
        empties = [e for e in events if e["choices"] and e["choices"][0]["delta"] == {} and e["choices"][0]["finish_reason"] is None]
        self.assertGreaterEqual(len(empties), 1)  # keep-alives went out while claude was silent
        text = "".join(e["choices"][0]["delta"].get("content") or "" for e in events if e["choices"])
        self.assertEqual(text, "late")

    def test_error_mapping(self):
        canned("Not logged in · Please run /login", is_error=True)
        status, out = self.post({"model": "fable", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(status, 401)
        self.assertIn("claude auth login", out["error"]["message"])
        canned("Claude usage limit reached. Your limit will reset at 3pm", is_error=True)
        status, out = self.post({"model": "fable", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(status, 429)
        self.assertEqual(out["error"]["code"], "claude_usage_limit")
        # streamed request: the status code must still be a real HTTP status
        status, out = self.post({"model": "fable", "stream": True, "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(status, 429)

    def test_error_without_result_event(self):
        REPLY.write_text("Not logged in · Please run /login\n")
        EXIT.write_text("1")
        try:
            status, out = self.post({"model": "fable", "messages": [{"role": "user", "content": "x"}]})
        finally:
            EXIT.unlink()
        self.assertEqual(status, 401)

    def test_response_format_json_schema(self):
        canned('{"title":"Bridge check"}', structured={"title": "Bridge check"})
        body = {"model": "fable", "temperature": 0.3, "messages": [{"role": "system", "content": "Title this."}, {"role": "user", "content": "hello"}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "session_title", "strict": True, "schema": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"], "additionalProperties": False}}}}
        status, out = self.post(body)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(out["choices"][0]["message"]["content"]), {"title": "Bridge check"})
        argv = ARGS.read_text().splitlines()
        self.assertIn("--json-schema", argv)
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1])["required"], ["title"])

    def test_unknown_model(self):
        status, out = self.post({"model": "gpt-5", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()


class PluginProfile(unittest.TestCase):
    """The Hermes provider profile in plugin/claude-cli must stay call-compatible
    with ``providers.base.ProviderProfile.fetch_models(*, api_key, timeout)``.
    Hermes's ``provider_model_ids()`` wraps the call in a bare ``except`` that
    also skips ``fallback_models``, so one TypeError here empties every model
    picker (scar 2026-09-16). Hermes itself is stubbed: only the signature
    contract is under test."""

    def _load_plugin(self):
        import importlib.util
        import types

        calls: list[dict] = []

        class ProviderProfile:  # mirrors providers/base.py at Hermes v0.21.2
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

            def fetch_models(self, *, api_key=None, timeout=8.0):
                calls.append({"api_key": api_key, "timeout": timeout})
                return ["stub-model"]

        providers = types.ModuleType("providers")
        providers.register_provider = lambda profile: None
        base = types.ModuleType("providers.base")
        base.ProviderProfile = ProviderProfile
        base.OMIT_TEMPERATURE = object()
        providers.base = base
        saved = {k: sys.modules.get(k) for k in ("providers", "providers.base")}
        sys.modules["providers"], sys.modules["providers.base"] = providers, base
        try:
            spec = importlib.util.spec_from_file_location(
                "claude_cli_plugin_under_test", ROOT / "plugin" / "claude-cli" / "__init__.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
        return mod, calls

    def test_fetch_models_matches_base_signature_and_drops_key(self):
        mod, calls = self._load_plugin()
        self.assertEqual(mod.claude_cli.fetch_models(api_key="placeholder"), ["stub-model"])
        self.assertEqual(calls, [{"api_key": None, "timeout": 8.0}])

    def test_fetch_models_tolerates_extra_keywords(self):
        mod, calls = self._load_plugin()
        self.assertEqual(mod.claude_cli.fetch_models(api_key=None, base_url="http://x", timeout=3), ["stub-model"])
        self.assertEqual(calls[0]["timeout"], 3)
