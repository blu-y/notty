# notty

Send task updates from Codex or Claude Code to Discord with a skill and a webhook.
No MCP server, bot, background process, or third-party Python dependencies required.

## Quick start

Requires Python 3.10 or later and Codex CLI or Claude Code. The installation
commands below are for macOS and Linux and use symbolic links.

1. Clone this repository and open a terminal in its root directory.
2. Create a Discord incoming webhook for the destination channel.
3. Copy the example configuration and set its `webhook_url` to your webhook URL:

   ```sh
   cp .notty.example.json .notty.json
   chmod 600 .notty.json
   ```

4. Start Codex or Claude Code in this directory and invoke the skill:

   **Codex**

   ```text
   Run the tests and fix any failures. $notty
   ```

   **Claude Code**

   ```text
   /notty Run the tests and fix any failures.
   ```

The agent sends one summary when the requested task finishes or needs your input.
It keeps the summary in your language and still replies in the terminal.
Notifications apply to the task that invoked notty, not to later tasks.

## Use in other projects

Run these commands from the repository root to make the skills available to your
user account. Run only the commands for the CLI you use:

```sh
# Codex
mkdir -p "$HOME/.agents/skills"
ln -s "$PWD/skills/notty" "$HOME/.agents/skills/notty"

# Claude Code
mkdir -p "$HOME/.claude/skills"
ln -s "$PWD/.claude/skills/notty" "$HOME/.claude/skills/notty"
```

Check for an existing `notty` entry before linking. Restart the CLI if the skill
does not appear. Keep the clone in place; moving or deleting it breaks these links.
The Claude wrapper shares the script and workflow with the Codex skill.

## Configuration

The configuration file contains one field:

```json
{
  "webhook_url": "PasteYourURL"
}
```

notty uses the first available source in this order:

1. `NOTTY_WEBHOOK_URL` environment variable.
2. The file passed to `--config`, or the path in `NOTTY_CONFIG_FILE`.
3. The nearest `.notty.json` in the current directory or its parents.
4. The nearest `.notty.json` alongside the resolved script path or its parents.
5. `~/.config/notty/config.json`.

An explicitly selected file that is missing or invalid is an error; notty does
not silently switch to another destination. A configuration in the clone can
serve as the default when using the skill from other projects. To use a Discord
thread, add `?thread_id=THREAD_ID` to the webhook URL.

The webhook URL is a secret. `.notty.json` and local environment files are ignored
by Git. Keep real URLs out of committed files, prompts, screenshots, and command
arguments. Store custom configuration files outside the repository or add them
to your own ignore rules. notty does not automatically load `.env` files.

## Notification format

```text
[>_ Codex](https://chatgpt.com/codex/cloud)
alex:~/projects/example · *workstation.local*

[Fix sign-in flow]

Fixed the sign-in redirect. All tests passed.
```

Only the agent heading is linked. Claude uses `✽ Claude Code` and defaults to
`https://claude.ai/code`. PC names are italic; the path and session name use plain
text. There are no leading or trailing blank lines. Metadata is escaped so names
containing Markdown characters remain readable. Message bodies support Markdown.

Use `--session-url` when the actual web session URL is known. Claude IDs in the
`session_...` format link to the corresponding web session. Local UUIDs and session
names are never converted into invented web links; the heading uses the service's
landing page instead.

The notification includes your OS username, hostname, workspace path, session
name, and the supplied summary. Paths inside your home directory are shortened
with `~`. Review summaries for sensitive information before sending them.

### Session metadata

- **Codex:** reads `name` and `cwd` for `CODEX_THREAD_ID` from the local
  `$CODEX_HOME/state_*.sqlite` database, defaulting to `~/.codex`. The `title` field
  may contain the initial prompt and is not used as the session name.
- **Claude Code:** the wrapper passes `CLAUDE_SESSION_ID`. notty reads
  `custom-title` records from the matching transcript under
  `$CLAUDE_CONFIG_DIR/projects`, defaulting to `~/.claude/projects`, and falls back
  to `ai-title` records.

Metadata lookup is read-only and best effort: these are local CLI implementation
details and may change. An unavailable name is shown as `Unnamed session`, with
the session ID when available. You can override the name with `--session` or
`NOTTY_SESSION_NAME`. The skill passes the original workspace path explicitly.

## Direct use

Pipe the summary through stdin to preserve newlines and avoid shell expansion:

```sh
python3 skills/notty/scripts/send.py --agent codex --workspace "$PWD" <<'MESSAGE'
The release checks passed. Ready for review.
MESSAGE
```

| Option | Purpose |
| --- | --- |
| `--message TEXT` | Supply the body instead of reading stdin. |
| `--agent codex\|claude` | Set the agent label; otherwise detect it from session environment variables. |
| `--workspace PATH` | Set the original workspace path. `--project` is an alias. |
| `--session NAME` | Override the session name. |
| `--session-id ID` | Override the session ID used for metadata lookup. |
| `--session-url URL` | Use a known HTTPS web session URL for the heading. |
| `--config PATH` | Read a specific JSON configuration file. |
| `--status STATUS` | Accept `done`, `failed`, `blocked`, or `info` for compatibility; not displayed. |

Put the task outcome in the body. Run `python3 skills/notty/scripts/send.py --help`
for the command reference. Exit codes are `0` for confirmed delivery, `1` for a
configuration, payload, or delivery failure, and `2` for invalid CLI arguments.

## Delivery behavior and limits

- Sends one HTTPS request with a 15-second socket timeout and `wait=true`.
  Success requires a Discord message ID in the response.
- Disables mentions, rejects redirects, and restricts webhook destinations to
  Discord's HTTPS webhook endpoints. Errors do not print the webhook URL.
- Never retries automatically, including after rate limits or uncertain delivery.
  A timeout can happen after Discord accepts the message; check the channel before
  sending again. Delivery is not guaranteed to be exactly once.
- Keeps messages within 2,000 UTF-16 code units, truncating the body with an
  ellipsis while preserving the metadata. Oversized metadata is rejected.
- Sends notifications only. It cannot receive Discord replies, approve CLI
  permissions, or detect a killed process. It installs no hooks and depends on
  the agent reaching the notification step.

## Development

Run the offline test suite without installing dependencies:

```sh
python3 -W error -m unittest discover -s tests -v
```

Tests use fake webhooks and mocked HTTP responses. They do not send messages or
need credentials. CI runs the suite on macOS and Linux with Python 3.10 and 3.14.
The direct-use command above sends a real notification when a webhook is configured.

## References

- [Discord webhook API](https://docs.discord.com/developers/resources/webhook#execute-webhook)
- [Codex skills](https://learn.chatgpt.com/docs/build-skills)
- [Claude Code skills](https://code.claude.com/docs/en/skills)
