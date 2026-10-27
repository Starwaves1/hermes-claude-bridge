# hermes-claude-bridge

Run [Hermes Agent](https://github.com/NousResearch/hermes-agent) on Claude Code.
Hermes keeps doing what it's good at: Discord, sessions, cron, voice. Every
turn goes to a Claude Code session that has all of its own tools, MCP
servers, connectors, skills and CLAUDE.md. It uses your Claude subscription
through the unmodified `claude -p` binary.

**READ THE CLAUDE TOS BEFORE USING.** One person's subscription, for that
person. Do NOT hammer it, do not schedule heartbeats every 15 minutes, do not
put it behind a bot other people talk to. Modest individual use only. Please
don't ruin it.

No legal assurances. Not a recommendation. Probably don't use this in
business contexts. This is just me sharing my code.

The rules changed three times in 2026. Read these first, and again later:

- https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan
- https://code.claude.com/docs/en/legal-and-compliance
- https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan

Written against Hermes v0.20.4. v1, where Hermes ran the loop and Claude had
no tools, is in the git history.

## How it works

- Each Hermes conversation gets one Claude Code session. Later turns resume
  it and send only the new messages.
- Claude Code runs in auto mode and uses its own tools. Hermes tools it
  already has (terminal, files, web search, delegation) are not offered.
  The rest (`text_to_speech`, `send_message`, `cronjob_manage`, ...) come
  back to Hermes as tool calls, and Hermes runs them.
- When Claude Code wants permission for something, the question shows up in
  the chat. Reply `y`, `yes`, `n`, `no`, or type a reason to deny.
- Claude Code compacts its own context. Turn Hermes compression off.
- Side requests with no tools (titles, summaries) run in a stateless call
  with everything switched off.
- Usage limit returns HTTP 429. Logged out returns 401. There is no fallback
  model.

| Path | What it is |
| --- | --- |
| `claude_bridge.py` | The bridge, an OpenAI-compatible server on `127.0.0.1:8790`. Python stdlib only. |
| `plugin/claude-cli/` | Hermes provider plugin. Starts the bridge and tells it which conversation each request belongs to. |
| `plugin/claude-login/` | `/login` from chat, and the approval prompts. |
| `tests/` | Runs against a fake `claude`. Spends no usage. |

## Install (Hermes Docker image)

Give the container at least 3 GiB of memory. Then:

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

Keep the plugins as symlinks into the clone. That's how the plugin finds the
bridge.

Add this to `/opt/data/config.yaml`:

```yaml
model:
  provider: claude-cli
  default: opus              # or sonnet, fable, a full model id
fallback_providers: []
compression:
  enabled: false
plugins:
  enabled: [claude-login]    # add to your existing list
auxiliary:                   # point side tasks at a cheap model you already use
  title_generation: {provider: <provider>, model: <model>}
  # ...same for the other auxiliary.* tasks you want off Claude
```

Restart the container. The plugin starts the bridge and restarts it if it
dies. Logs are in `/opt/data/claude-bridge/`.

To update, `git pull` in `/opt/data/hermes-claude-bridge` and restart.

## Using it

Send `/login` in a DM with the bot. Open the link, sign in, and paste the
code back as a normal message. The code goes to Claude Code, never to the
agent. `/login status`, `/login logout` and `/login cancel` also work. Newer
Hermes has its own `/login`, so use `/claude-login` there.

Approval prompts look like this:

```
[3] Claude Code wants to use Bash:
rm -rf build/
Reply y / n, or give a reason to deny.
```

Only the person who sent the turn, or someone in `CLAUDE_BRIDGE_LOGIN_USERS`,
can answer. With more than one open, add the number (`y 3`). No answer in 10
minutes means no. Cron and CLI turns have nobody to ask, so their prompts are
denied.

If Claude starts background work, you get the reply right away. When the
work finishes, Claude's follow-up is posted as `[Background follow-up] ...`.
A new message stops background work that's still running.

`/new` starts a fresh Claude Code session.

## Settings

Environment variables, all optional:

| Variable | Default | |
| --- | --- | --- |
| `CLAUDE_BRIDGE_LOGIN_USERS` | nobody | User ids that may `/login` and answer any approval |
| `CLAUDE_BRIDGE_WORKDIR` | `~/workspace` | Where Claude Code works |
| `CLAUDE_BRIDGE_PERMISSION_MODE` | `auto` | Claude Code permission mode |
| `CLAUDE_BRIDGE_DROP_TOOLS` | built-in list | Hermes tools not offered (replaces the list) |
| `CLAUDE_BRIDGE_KEEP_TOOLS` | none | Hermes tools to offer anyway |
| `CLAUDE_BRIDGE_APPROVAL_TIMEOUT` | 600 | Seconds to wait for a reply |
| `CLAUDE_BRIDGE_BG_WAIT` | 1800 | Seconds background work may keep running |
| `CLAUDE_BRIDGE_MAX_CONCURRENCY` | 2 | Claude Code processes at once |
| `CLAUDE_BRIDGE_TIMEOUT` | 1800 | Seconds per turn |

The rest are at the top of `claude_bridge.py`.

## Worth knowing

- Never add `--bare`. It skips the subscription login and bills an API key.
- Claude Code runs as the Hermes user and sees the gateway's environment,
  bot tokens included. The container is the sandbox. Don't use host
  networking.
- A bridge started some other way, like the macOS LaunchAgent template in
  `launchd/`, can't relay approvals. Every prompt gets denied. Set
  `CLAUDE_BRIDGE_AUTOSTART=0` if you go that route.
- Claude Code's auto-updater is off inside the bridge. Update it with
  `claude update`.

## License

MIT
