# hermes-claude-bridge

Use your Claude subscription with Hermes, within the Claude subscription TOS
(as of 2026-09-03), via `claude -p`. Hermes stays the front end (Discord,
sessions, memory, cron, voice). Claude Code is the harness: every turn runs
inside a persistent Claude Code session with all of its own tools, MCP
servers, connectors, skills and CLAUDE.md.

**READ THE CLAUDE TOS BEFORE USING.** One person's subscription, for that
person. Do NOT hammer it, do not schedule heartbeats every 15 minutes, do not
put it behind a bot other people talk to. Modest individual use only. Please
don't ruin it.

No legal assurances. Not a recommendation. Probably don't use this in
business contexts. This is just me sharing my code.

Policy pages to read first, and re-read later, because the rules changed
three times in 2026:

- https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan
- https://code.claude.com/docs/en/legal-and-compliance
- https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan

Built for [Hermes Agent](https://github.com/NousResearch/hermes-agent)
(written against v0.20.4). v1.1.0, where Hermes owned the loop and Claude
Code's tools were off, is in the git history.

## What it is

| Path | Role |
| --- | --- |
| `claude_bridge.py` | OpenAI-compatible server on `127.0.0.1:8790`. Stdlib only, Python 3.9+. |
| `plugin/claude-cli/` | Hermes model-provider plugin `claude-cli`: tags each request with its conversation, forwards `/model` and `/effort`, starts the bridge if nothing answers on the port. |
| `plugin/claude-login/` | Hermes plugin: `/login` (and `/claude-login`) signs Claude Code in from chat, and relays Claude Code's permission requests to the chat. |
| `launchd/` | Optional macOS LaunchAgent template. |
| `tests/` | 62 tests against fake `claude` scripts. No usage spent. |

## How it works

1. Hermes sends an OpenAI chat request with its tools. The provider plugin
   adds `claude_bridge_session`, a key that is stable for the conversation.
2. The bridge maps that key to a Claude Code session id and finds the last
   message it produced in the incoming history. Only what came after it (new
   user text, Hermes tool results, images) is sent, via `--resume`. Unknown
   conversation or edited history: a new session, seeded with the most
   recent part of the Hermes transcript (`CLAUDE_BRIDGE_BOOTSTRAP_MAX_CHARS`,
   with a note that older history was left out). The session is recorded as
   soon as Claude Code starts it, so a retried turn resumes instead of
   running twice. Hermes's background review and curator forks run in a
   `--fork-session` copy and never touch your session.
3. Each turn is one spawn:
   ```
   claude -p --input-format stream-json --output-format stream-json --verbose \
     --include-partial-messages --permission-mode auto --permission-prompt-tool stdio \
     --model <m> --effort <e> --append-system-prompt-file <hermes prompt + tool list> \
     --json-schema <reply contract> (--session-id <uuid> | --resume <uuid> [--fork-session])
   ```
   in `~/workspace`. Claude Code uses its own tools freely. Hermes tools it
   lacks (`text_to_speech`, `memory`, `send_message`, `cronjob_manage`, ...)
   come back through the reply contract `{"content", "tool_calls"}`; Hermes
   runs them and the results arrive as the next turn. Hermes tools Claude Code
   duplicates (`terminal`, `read_file`, `web_search`, `delegate_task`, ...) are
   not offered.
4. Requests without tools (titles, summaries) run stateless with everything
   off (`--safe-mode --tools ""`).
5. Approvals: auto mode decides most actions itself. Whatever it would ask
   a person about (ask rules, protected paths, `AskUserQuestion`, connector
   tools that require approval, repeated classifier blocks) arrives on the
   control channel and is posted to the chat that sent the turn:
   ```
   [3] Claude Code wants to use Bash:
   rm -rf build/
   (Remove build output)
   Reply y / n, or give a reason to deny.
   ```
   `y`/`yes` allows, `n`/`no` denies, anything else denies with your text as
   the reason Claude sees (exact match after trim + lowercase). Questions take
   an option number or free text. Only the person who sent the turn, or a
   `CLAUDE_BRIDGE_LOGIN_USERS` admin, can answer; the answer never reaches the
   agent. With several open, name one: `y 3`, `n 3`, `3 <reason>`. No answer
   in 10 minutes, or no chat to ask (CLI, cron before any message): denied.
6. Background work: a turn that leaves background tasks running (a
   `run_in_background` command, a background agent) replies right away. The
   `claude` child keeps running for up to `CLAUDE_BRIDGE_BG_WAIT`, and what
   Claude says when the work finishes is posted to the chat as
   `[Background follow-up] ...` (Hermes tools it asks for then are not run).
   A new message in that conversation waits up to 10 s, then stops the
   background work and says so. When the limit passes, the bridge closes
   the child's input; Claude Code then stops leftover background shells
   (documented behaviour, not observed here).
7. Streamed replies get an empty keep-alive chunk every 10 s, so long turns
   do not trip Hermes's stale-stream timer. Usage limit: HTTP 429. Not logged
   in: 401 "run /login". Errors after the first keep-alive arrive as an
   in-stream OpenAI error whose message starts with the status line
   (`429 Too Many Requests: ...`), which Hermes classifies the same way. No
   fallback model on purpose.
8. Context: compaction is Claude Code's job (its auto-compact stays on; never
   turn it off). Hermes compression must be off for this bot (below), and
   the bridge advertises a 1M context window.

Environment: tool-path `claude` children inherit the bridge's environment
(PATH, proxies, CA bundles, `SSH_AUTH_SOCK`, tokens your tools need) minus
`ANTHROPIC_*`, `CLAUDECODE`, `CLAUDE_CODE_*` and the bridge's own
`CLAUDE_BRIDGE_*` / `CLAUDE_CLI_BRIDGE_*` settings, so an API key is never
billed by accident. Started by the plugin, the bridge has the gateway's
environment, so that includes Hermes's own secrets (bot tokens, provider
keys in `.env`). The stateless text path keeps a minimal environment
(HOME, USER, LOGNAME, PATH, TERM, LANG, TMPDIR).

Control channel: the provider plugin starts the bridge with a random token
handed over an inherited pipe (never env or disk); the approval endpoints
require it. Claude Code's children cannot read it, but any other process
running as the same user could still talk to the bridge's chat endpoint on
loopback. Do not run the container with host networking.

## Install into a Hermes Docker container

Assumes the official image layout (`HOME=/opt/data`, uid 1000). Give the
container at least 3 GiB of memory: Claude Code plus its MCP servers run next
to Hermes.

```sh
C=hermes   # your container name
docker exec -u 1000 -e HOME=/opt/data $C bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
docker exec -u 1000 $C git clone https://github.com/Starwaves1/hermes-claude-bridge /opt/data/hermes-claude-bridge
docker exec -u 1000 $C bash -c '
  mkdir -p /opt/data/plugins/model-providers &&
  ln -sfn /opt/data/hermes-claude-bridge/plugin/claude-cli /opt/data/plugins/model-providers/claude-cli &&
  ln -sfn /opt/data/hermes-claude-bridge/plugin/claude-login /opt/data/plugins/claude-login &&
  echo CLAUDE_CLI_BRIDGE_API_KEY=local-bridge >> /opt/data/.env &&
  echo CLAUDE_BRIDGE_LOGIN_USERS=<your user id> >> /opt/data/.env'
```

No git in the image? `docker cp` the repo to `/opt/data/hermes-claude-bridge`
instead. Keep the plugins as symlinks (or keep the repo at that path): the
provider plugin finds `claude_bridge.py` through them.

`/opt/data/config.yaml`:

```yaml
model:
  provider: claude-cli
  default: opus            # fable, opus, sonnet or a full model id
fallback_providers: []
compression:
  enabled: false           # Claude Code compacts; this also turns off gateway session hygiene
plugins:
  enabled: [claude-login]  # add to your existing list; user plugins are opt-in
auxiliary:                 # keep side tasks on the provider the bot used before
  title_generation: {provider: <your provider>, model: <model>}
  vision:           {provider: <your provider>, model: <model>}
  approval:         {provider: <your provider>, model: <model>}
  # ...and any other auxiliary.* task you use
```

With compression off, Hermes v0.20.4 never compresses or refuses on its own
token estimate (all its preflight checks are gated on `compression.enabled`);
it would only stop on a provider context-overflow error, which the bridge does
not produce. Hermes still keeps and re-sends the whole history each turn, so a
very long-lived chat gets slowly heavier; `/new` starts fresh (Hermes memory
carries over, the Claude Code session does not).

Restart the gateway. The plugin starts the bridge as a detached process and
restarts it if it dies; its log is `/opt/data/claude-bridge/bridge.log`
(startup crashes: `bridge.out`). A bridge left over from a previous gateway
process is replaced. Then in chat (use a DM for `/login`):

- `/login` posts the sign-in link. Open it, approve, paste the code back as a
  normal message. That message goes straight to Claude Code; the agent never
  sees it. Only user ids in `CLAUDE_BRIDGE_LOGIN_USERS` may use it.
- `/login status`, `/login logout`, `/login cancel`.
- Approval prompts appear in the chat during a turn; reply as above.

Newer Hermes versions have a built-in `/login` (Nous account); use
`/claude-login` there.

On a Mac you can run the bridge from the LaunchAgent template instead (set
`CLAUDE_BRIDGE_AUTOSTART=0` for Hermes then). Such a bridge has no control
token, so chat approvals are off and every prompt is denied. `claude auth login` in a
terminal works anywhere.

## Knobs

| Env var | Default | Meaning |
| --- | --- | --- |
| `CLAUDE_BRIDGE_CLAUDE_BIN` | `~/.local/bin/claude` | the binary to spawn |
| `CLAUDE_BRIDGE_WORKDIR` | `~/workspace` | cwd of every Claude Code session (sessions resume per cwd) |
| `CLAUDE_BRIDGE_STATE_DIR` | `~/.hermes/claude-bridge` or `~/claude-bridge` | log, session map, scratch files |
| `CLAUDE_BRIDGE_PERMISSION_MODE` | `auto` | Claude Code permission mode |
| `CLAUDE_BRIDGE_DROP_TOOLS` | see `DEFAULT_DROP_TOOLS` | comma list of Hermes tools not offered (replaces the default) |
| `CLAUDE_BRIDGE_KEEP_TOOLS` | empty | comma list to offer anyway |
| `CLAUDE_BRIDGE_MAX_CONCURRENCY` | 2 | parallel `claude` children |
| `CLAUDE_BRIDGE_TIMEOUT` | 1800 | seconds per turn |
| `CLAUDE_BRIDGE_CONTEXT_LENGTH` | 1000000 | what `/v1/models` advertises; Claude Code compacts itself |
| `CLAUDE_BRIDGE_HEARTBEAT` | 10 | seconds of silence before a keep-alive chunk |
| `CLAUDE_BRIDGE_AUTOSTART` | 1 | the provider plugin starts the bridge when its port is dead |
| `CLAUDE_BRIDGE_LOGIN_USERS` | empty (deny all) | user ids allowed to run `/login` and to answer any approval |
| `CLAUDE_BRIDGE_APPROVAL_TIMEOUT` | 600 | seconds to wait for an approval before denying |
| `CLAUDE_BRIDGE_BG_WAIT` | 1800 | seconds a detached child may keep running background work |
| `CLAUDE_BRIDGE_BOOTSTRAP_MAX_CHARS` | 100000 | most Hermes history rendered into a new session |
| `CLAUDE_BRIDGE_SESSION_TTL_DAYS` | 30 | drop session-map entries unused this long |
| `CLAUDE_BRIDGE_QUEUE_WAIT` | 120 | seconds a request waits for a free slot or its session |
| `CLAUDE_BRIDGE_LOGIN_TIMEOUT` | 600 | seconds `/login` waits for the code |
| `CLAUDE_BRIDGE_LOG` | `<state dir>/bridge.log` | log file |
| `CLAUDE_BRIDGE_SCRIPT` | found via the plugin symlink | path to `claude_bridge.py` for autostart |
| `CLAUDE_CLI_BRIDGE_BASE_URL` | `http://127.0.0.1:8790/v1` | where the plugins reach the bridge |
| `CLAUDE_BRIDGE_DUMP_DIR` | off | writes full request content, debugging only |
| `CLAUDE_BRIDGE_FAKE_ERROR` | off | `limit`, `login` or `crash` to test error paths |

## Gotchas

- `--bare` disables the subscription login and bills an API key. Never add it.
- On macOS the keychain login needs `USER` in the child environment.
- Auto mode means Claude Code acts without asking for most things. It runs
  as the Hermes user, inside the container. A classifier block is reported
  to Claude, not to you; only actions that would prompt a person reach chat.
- Host approvals need `--permission-prompt-tool stdio` (the flag the Agent
  SDK passes; `--help` doesn't list it, the CLI reference does). Without it
  `-p` denies every prompt silently; `--permission-prompts host` alone is
  not enough.
- Claude Code records the system prompt at a session's first turn. When
  Hermes's prompt changes, the bridge passes `--system-prompt-snapshot off`
  for that session from then on.
- Claude Code sums token usage over its internal calls. The bridge reports
  the last call's input, which is the real context size.
- The usage-limit message avoids the words Hermes treats as "out of credits"
  (quota, billing, credits, funds), which would trigger a silent provider
  switch.
- The child runs with `DISABLE_AUTOUPDATER=1`; update Claude Code yourself.
- `hermes -z` one-shots never send reasoning effort; `/effort` works in
  gateway sessions.

## License

MIT
