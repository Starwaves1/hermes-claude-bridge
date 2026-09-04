# hermes-claude-bridge

Use your Claude subscription with Hermes, within the Claude subscription TOS
(as of 2026-09-03), via the Agent SDK and `claude -p`, to give an
OpenAI-compatible endpoint which harnesses such as Hermes Agent can use.

**READ THE CLAUDE TOS BEFORE USING.** Do NOT hammer it. If you schedule
heartbeats or cron jobs through it, keep them to every 15 minutes or slower.
Don't spread it across multiple users. Use it for modest individual purposes
only. Please don't ruin it.

Single user. No legal assurances. Not a recommendation. This is just me
sharing my code. Probably don't use this in business contexts. Don't abuse
it. It's a thin layer over a recent Agent SDK / Claude Code feature: print
mode with structured output.

Policy pages to read first, and re-read later, because the rules changed
three times in 2026:

- https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan
- https://code.claude.com/docs/en/legal-and-compliance
- https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan

Built for [Hermes Agent](https://github.com/NousResearch/hermes-agent). The
bridge itself is a plain OpenAI-compatible server (chat completions,
streaming, tools, `response_format`), so other harnesses may work. I only
tested Hermes. It works so far. Just a side project: Fable 5.1 and I coded it
in about two hours. Godspeed o7

Tip: point your personal AI agent at this repo and have it set it up for you.

## What it is

| Path | Role |
| --- | --- |
| `claude_bridge.py` | OpenAI-compatible server on `127.0.0.1:8790`. Every chat completion spawns the unmodified `claude` binary in print mode with Claude Code's own tools disabled. Stdlib only, Python 3.9+. |
| `plugin/claude-cli/` | Hermes Agent model-provider plugin (`claude-cli`). Forwards `/model` and `/effort`. |
| `launchd/` | macOS LaunchAgent template so the bridge is always up. |
| `tests/` | 18 tests against a fake `claude` script. No usage spent. |

Claude is the brain only. Hermes owns the agent loop, its tools, memory,
skills and approvals. Nothing runs inside the `claude` child.

## How it works

1. Hermes sends a normal OpenAI chat request.
2. The bridge writes the system messages to a system-prompt file, renders the
   rest of the transcript into stdin, and spawns:
   ```
   claude -p --safe-mode --tools "" --strict-mcp-config --disable-slash-commands \
     --no-session-persistence --output-format stream-json --verbose \
     --include-partial-messages --model <model> --effort <effort> \
     --system-prompt-file <file> [--json-schema <schema>]
   ```
3. Tools: Hermes's tool schemas go into the system prompt. The model answers
   through Claude Code's one structured-output tool with
   `{"content": "...", "tool_calls": [{"name", "arguments"}]}`, which the
   bridge maps to OpenAI `tool_calls`. If a model calls a Hermes tool
   natively anyway, the bridge intercepts that `tool_use` event and returns
   it as the tool call.
4. The bridge reads the child's event stream, forwards text deltas live,
   sends keep-alive chunks during silence, and reports the last API call's
   token usage so Hermes's context accounting is right.
5. Usage limit reached: HTTP 429 with a plain message. Not logged in: 401.
   There is no fallback model on purpose. A weaker model silently taking over
   is worse than an error.

The child environment is HOME, USER, LOGNAME, PATH, TERM, LANG, TMPDIR and
nothing else. No `ANTHROPIC_*` variable can reach it, so an API key is never
billed by accident.

## Setup (macOS, tested with Hermes Agent v0.20.5 and Claude Code 2.1.259)

Requirements: Claude Code installed and logged in (`claude auth login`),
Python 3.9+, Hermes Agent.

```sh
git clone https://github.com/Starwaves1/hermes-claude-bridge
cd hermes-claude-bridge
python3 -m unittest tests/test_bridge.py          # no usage spent
python3 claude_bridge.py --port 8790              # or the LaunchAgent below
curl 127.0.0.1:8790/health
```

LaunchAgent (optional): copy `launchd/com.hermes-claude-bridge.plist.template`
to `~/Library/LaunchAgents/com.hermes-claude-bridge.plist`, replace
`__HOME__`, `__USER__` and `__REPO__`, then
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hermes-claude-bridge.plist`.

Hermes:

```sh
ln -s "$PWD/plugin/claude-cli" ~/.hermes/plugins/model-providers/claude-cli
echo 'CLAUDE_CLI_BRIDGE_API_KEY=local-bridge' >> ~/.hermes/.env   # placeholder Hermes requires; the bridge ignores it
```

In `~/.hermes/config.yaml`:

```yaml
model:
  provider: claude-cli
  default: fable          # or opus, sonnet, haiku, or a full model id
fallback_providers: []
```

Pin `auxiliary.<task>.provider: claude-cli` for the aux tasks you want on
Claude too (compression, title generation, ...). Restart the gateway. Every
call is logged in `~/.hermes/claude-bridge/bridge.log` with model, effort,
tool calls, token counts and the subscription windows the binary reports
(`5h=13% 7d=51%`).

Do not keep Hermes's built-in `anthropic` OAuth provider configured next to
this. Different rules apply to it.

## Knobs

| Env var | Default | Meaning |
| --- | --- | --- |
| `CLAUDE_BRIDGE_CLAUDE_BIN` | `~/.local/bin/claude` | the binary to spawn |
| `CLAUDE_BRIDGE_STATE_DIR` | `~/.hermes/claude-bridge` | log and working directory |
| `CLAUDE_BRIDGE_MAX_CONCURRENCY` | 3 | parallel `claude` children |
| `CLAUDE_BRIDGE_TIMEOUT` | 900 | seconds per spawn |
| `CLAUDE_BRIDGE_CONTEXT_LENGTH` | 200000 | what `/v1/models` advertises |
| `CLAUDE_BRIDGE_HEARTBEAT` | 10 | seconds of silence before a keep-alive chunk |
| `CLAUDE_BRIDGE_DUMP_DIR` | off | writes full request content per call, debugging only |
| `CLAUDE_BRIDGE_FAKE_ERROR` | off | `limit`, `login` or `crash` to test error paths without usage |

## Gotchas

- `--bare` disables the subscription login and bills an API key. The bridge
  uses `--safe-mode`, which isolates your `CLAUDE.md`, hooks, MCP and memory
  from every call but keeps the login.
- The keychain login needs `USER` in the child environment. HOME and PATH
  alone report "Not logged in".
- Tool-trained models may call the listed tool names natively. Claude Code
  answers "No such tool available" and the model then tells you its tools are
  gone. The prompt header and the interception in step 3 handle it.
- Claude Code sums token usage across its internal turns. Reporting that sum
  made Hermes think a 160k context was 660k and compress on every turn. The
  bridge reports the last call's usage instead.
- Hermes aborts context compression after 30 seconds without streamed
  output. Hence the live deltas and keep-alives.
- The usage-limit message deliberately avoids the words Hermes's auxiliary
  router treats as "out of credits" (quota, billing, credits, funds), because
  those trigger a silent retry on the next provider.
- Images are not passed to Claude. Text only for now.
- `hermes -z` one-shots never send reasoning effort. Gateway and WebUI
  sessions do, so `/effort` works there.

## License

MIT
