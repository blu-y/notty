---
name: notty
description: Send the current task result to Discord using a webhook, only when explicitly requested.
---

Follow [the notty workflow](workflow.md) for this task.
The bundled script is `${CLAUDE_SKILL_DIR}/scripts/send.py`.
Use `--agent claude --session-id "${CLAUDE_SESSION_ID}"` so the script can look up
this session's saved name. Use `--workspace "${CLAUDE_PROJECT_DIR}"` for the
original workspace path. Apply the workflow to the following request:

$ARGUMENTS
