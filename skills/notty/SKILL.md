---
name: notty
description: Send a Discord notification for the current task when the user explicitly invokes $notty or asks to use notty. Notification only; no remote chat or approval handling.
---

# notty

Apply this skill only to the task that invoked it. Do the requested work normally;
send one short Discord notification when the task finishes or cannot continue.
Do not send a startup notification or turn it on for later requests.

- Use `done` for completed work, `failed` for an unsuccessful task, or `blocked`
  when you need the user's input. Describe the actual outcome and relevant checks;
  do not claim success if the work or tests failed.
- Keep the message in the user's language. Include a brief task description,
  outcome, and any actionable next step. Send a summary, not code, transcripts,
  credentials, or complete logs.
- Run the bundled `scripts/send.py` using its absolute path, resolved relative to
  this skill's directory. Use `--agent codex` or `--agent claude` as appropriate
  and `--workspace` with the absolute path of the session's original working
  directory (not a subdirectory you changed into while working). PC name is
  detected automatically. Session names (Codex's displayed `Thread name`) are
  read from local CLI metadata; pass
  `--session` if the current session name is known but cannot be detected. Do not
  invent a session name from the task. If the actual web session URL is known,
  pass it with `--session-url`; do not derive a web URL from a local UUID or
  session name. Otherwise the heading links to the agent's web page.
  Pass the message through stdin to
  avoid shell expansion of generated text:

  ```sh
  python3 /absolute/path/to/notty/scripts/send.py --status done --agent codex --workspace /absolute/path/to/my-project <<'NOTTY_MESSAGE'
  Fixed the sign-in bug. All eight related tests passed.
  NOTTY_MESSAGE
  ```

- The script reads the webhook from local configuration or an environment
  variable. Do not read or print the webhook URL yourself.
- If sending fails, report that the notification was not delivered, alongside
  the task result. Do not automatically retry an uncertain send.
- Still give the user the normal terminal response. This skill cannot receive
  Discord replies or intercept CLI permission dialogs, crashes, or interrupts.
