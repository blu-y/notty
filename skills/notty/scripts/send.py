#!/usr/bin/env python3
"""Send one task notification to Discord, using only the Python standard library."""

import argparse
from contextlib import closing
import getpass
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import sys
from urllib import error, parse, request


STATUSES = ("done", "failed", "blocked", "info")


class NottyError(Exception):
    pass


class NoRedirects(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_webhook(config_path=None):
    webhook = os.environ.get("NOTTY_WEBHOOK_URL")
    if webhook:
        return webhook

    explicit = config_path or os.environ.get("NOTTY_CONFIG_FILE")
    if explicit:
        path = Path(explicit).expanduser()
    else:
        candidates = [directory / ".notty.json" for directory in
                      (Path.cwd(), *Path.cwd().parents, *Path(__file__).resolve().parents)]
        path = next((candidate for candidate in candidates if candidate.is_file()),
                    Path.home() / ".config/notty/config.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise NottyError("Set NOTTY_WEBHOOK_URL or configure .notty.json first.") from None
    except (OSError, ValueError):
        raise NottyError("Could not read webhook configuration.") from None
    if not isinstance(data, dict) or not isinstance(data.get("webhook_url"), str):
        raise NottyError("Configuration must contain a webhook_url string.")
    return data["webhook_url"]


def webhook_endpoint(webhook):
    try:
        url = parse.urlsplit(webhook)
        valid = (not any(character.isspace() for character in webhook) and
                 url.scheme == "https" and
                 url.netloc in {"discord.com", "discordapp.com"} and
                 re.fullmatch(r"/api(?:/v[0-9]+)?/webhooks/[0-9]+/[A-Za-z0-9_-]+", url.path) and
                 not url.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise NottyError("Expected a Discord HTTPS webhook URL.")
    query = [(key, value) for key, value in parse.parse_qsl(url.query) if key != "wait"]
    query.append(("wait", "true"))
    return parse.urlunsplit(url._replace(query=parse.urlencode(query)))


def session_metadata(agent, session_id):
    if not session_id or not re.fullmatch(r"[\w-]+", session_id):
        return {}
    if agent == "codex":
        root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        databases = sorted((path for path in root.glob("state_*.sqlite")
                            if path.stem.removeprefix("state_").isdigit()),
                           key=lambda path: int(path.stem.split("_")[-1]), reverse=True)
        for path in databases:
            try:
                with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro",
                                             uri=True, timeout=1)) as db:
                    row = db.execute("SELECT name, cwd FROM threads WHERE id = ?",
                                     (session_id,)).fetchone()
                if row:
                    return {"name": row[0], "cwd": row[1]}
            except sqlite3.Error:
                continue
    elif agent == "claude":
        root = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
        for path in (root / "projects").glob(f"*/{session_id}.jsonl"):
            names = {}
            try:
                with path.open(encoding="utf-8") as transcript:
                    for line in transcript:
                        if '"custom-title"' not in line and '"ai-title"' not in line:
                            continue
                        try:
                            record = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(record, dict):
                            continue
                        for kind, field in (("custom-title", "customTitle"), ("ai-title", "aiTitle")):
                            if record.get("type") == kind and isinstance(record.get(field), str):
                                names[kind] = record[field]
                name = names.get("custom-title") or names.get("ai-title")
                if name:
                    return {"name": name}
            except (OSError, UnicodeError):
                continue
    return {}


def metadata_text(value):
    """Keep metadata on one line and display Markdown characters literally."""
    value = re.sub(r"[\r\n\t]+", " ", str(value))
    return re.sub(r"([\\`*_\[\]<>~|])", r"\\\1", value)


def notification_payload(message, status, project, agent=None, *, pc=None, session=None,
                         session_url=None):
    if not message.strip():
        raise NottyError("Notification message cannot be empty.")
    workspace = Path(project).expanduser().absolute()
    try:
        relative = workspace.relative_to(Path.home())
        workspace = "~" if relative == Path(".") else f"~/{relative}"
    except ValueError:
        workspace = str(workspace)
    agent_heading = {"codex": ">_ Codex", "claude": "✽ Claude Code"}.get(agent, "notty")
    session_url = session_url or {"codex": "https://chatgpt.com/codex/cloud",
                                  "claude": "https://claude.ai/code"}.get(agent)
    if session_url:
        try:
            url = parse.urlsplit(session_url)
            valid = (url.scheme == "https" and url.hostname and
                     url.username is None and url.password is None and
                     not any(character.isspace() for character in session_url))
        except ValueError:
            valid = False
        if not valid:
            raise NottyError("Expected an HTTPS session URL without credentials or whitespace.")
        link = parse.quote(session_url, safe="/:?#[]@!$&'*,;=+-._~%")
        agent_heading = f"[{agent_heading}]({link})"
    heading = (f"{agent_heading}\n"
               f"{metadata_text(getpass.getuser())}:{metadata_text(workspace)} · "
               f"*{metadata_text(pc or socket.gethostname())}*\n\n"
               f"[{metadata_text(session or 'Unnamed session')}]\n\n")
    remaining = 4000 - len(heading.encode("utf-16-le"))
    if remaining < 2:
        raise NottyError("Notification metadata is too long; shorten the session name or URL.")
    body = message.strip()
    encoded = body.encode("utf-16-le")
    if len(encoded) > remaining:
        body = encoded[:remaining - 2].decode("utf-16-le", errors="ignore") + "…"
    content = heading + body
    return {"username": "notty", "content": content, "allowed_mentions": {"parse": []}}


def send_notification(webhook, payload):
    req = request.Request(
        webhook_endpoint(webhook),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "notty/0.1"},
        method="POST",
    )
    try:
        with request.build_opener(NoRedirects()).open(req, timeout=15) as response:
            result = json.load(response)
    except error.HTTPError as exc:
        exc.close()
        if exc.code == 429:
            raise NottyError("Discord rate limited the notification. No automatic retry was made.") from None
        raise NottyError(f"Discord rejected the notification (HTTP {exc.code}).") from None
    except (error.URLError, TimeoutError, OSError, HTTPException):
        raise NottyError("Could not confirm delivery to Discord. No automatic retry was made.") from None
    except ValueError:
        raise NottyError("Discord returned an invalid delivery confirmation.") from None
    if (not isinstance(result, dict) or not isinstance(result.get("id"), str)
            or not re.fullmatch(r"[0-9]+", result["id"])):
        raise NottyError("Discord did not return a message confirmation.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message", help="Notification text; defaults to stdin")
    parser.add_argument("--status", choices=STATUSES, default="done")
    parser.add_argument("--workspace", "--project", dest="workspace", help="Workspace directory path")
    parser.add_argument("--agent", choices=("codex", "claude"))
    parser.add_argument("--session", help="Session name; defaults to local CLI metadata")
    parser.add_argument("--session-id", help="Session ID when not available in the environment")
    parser.add_argument("--session-url", help="Known web session URL for the heading link")
    parser.add_argument("--config", help="Path to a local webhook configuration file")
    args = parser.parse_args()
    try:
        if args.message is None and sys.stdin.isatty():
            raise NottyError("Provide --message or pipe notification text through stdin.")
        message = args.message if args.message is not None else sys.stdin.read()
        agent = args.agent
        if not agent:
            agent = ("claude" if os.environ.get("CLAUDE_SESSION_ID") else
                     "codex" if os.environ.get("CODEX_THREAD_ID") else None)
        session_id = args.session_id or os.environ.get(
            "CLAUDE_SESSION_ID" if agent == "claude" else "CODEX_THREAD_ID")
        metadata = session_metadata(agent, session_id)
        workspace = (args.workspace or metadata.get("cwd") or
                     os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd())
        session = (args.session or os.environ.get("NOTTY_SESSION_NAME") or metadata.get("name")
                   or (f"Unnamed session ({session_id})" if session_id else "Unnamed session"))
        session_url = args.session_url
        if not session_url and agent == "claude" and session_id and re.fullmatch(r"session_[\w-]+", session_id):
            session_url = f"https://claude.ai/code/{session_id}"
        payload = notification_payload(message, args.status, workspace, agent, session=session,
                                       session_url=session_url)
        send_notification(load_webhook(args.config), payload)
    except NottyError as exc:
        print(f"notty: {exc}", file=sys.stderr)
        return 1
    except UnicodeError:
        print("notty: Input must be valid UTF-8 text.", file=sys.stderr)
        return 1
    print("notty: notification delivered to Discord.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
