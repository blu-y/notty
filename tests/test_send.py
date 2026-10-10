import contextlib
import importlib.util
import io
import json
import os
from http.client import IncompleteRead
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit


SCRIPT = Path(__file__).resolve().parents[1] / "skills/notty/scripts/send.py"
spec = importlib.util.spec_from_file_location("notty_send", SCRIPT)
notty = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notty)
WEBHOOK = "https://discord.com/api/webhooks/123/test-secret-token"


class SendTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_confirmation_query_preserves_thread(self):
        endpoint = notty.webhook_endpoint(WEBHOOK + "?thread_id=456&wait=false")
        self.assertEqual(parse_qs(urlsplit(endpoint).query),
                         {"thread_id": ["456"], "wait": ["true"]})

    def test_rejects_non_discord_urls_without_exposing_them(self):
        for url in ("http://discord.com/api/webhooks/123/secret",
                    "https://discord.com.evil.example/api/webhooks/123/secret",
                    "https://discord.com@evil.example/api/webhooks/123/secret",
                    "https://discord.com/api/webhooks/123/sec\nret",
                    "https://discord.com/api/webhooks/123/secret#fragment"):
            with self.subTest(url=url), self.assertRaises(notty.NottyError) as caught:
                notty.webhook_endpoint(url)
            self.assertNotIn("secret", str(caught.exception))

    def test_notification_keeps_unicode_and_disables_mentions(self):
        payload = notty.notification_payload("Tests passed 😀 @everyone", "done", "unicode-project", "codex")
        self.assertIn("Tests passed 😀", payload["content"])
        self.assertIn("/unicode-project ·", payload["content"])
        self.assertIn(">_ Codex", payload["content"])
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_long_message_respects_discord_limit_including_emoji(self):
        payload = notty.notification_payload("Unicode😀" * 1000, "done", "project")
        self.assertLessEqual(len(payload["content"].encode("utf-16-le")) // 2, 2000)
        self.assertFalse(payload["content"].startswith("\n"))
        self.assertTrue(payload["content"].endswith("…"))
        self.assertNotIn("\u200b", payload["content"])
        payload["content"].encode("utf-8")

    def test_empty_message_is_rejected(self):
        with self.assertRaises(notty.NottyError):
            notty.notification_payload(" \n ", "done", "project")

    def test_notification_includes_pc_workspace_and_session(self):
        with patch.object(notty.socket, "gethostname", return_value="my-pc"):
            payload = notty.notification_payload("Task complete", "done", "my-workspace",
                                                 "codex", session="Fix sign-in bug")
        for field in ("· *my-pc*", "/my-workspace ·", "[Fix sign-in bug]"):
            self.assertIn(field, payload["content"])

    def test_exact_layout_with_link_and_without_bold_or_outer_blank_lines(self):
        with patch.object(notty.Path, "home", return_value=Path("/Users/alex")), \
                patch.object(notty.getpass, "getuser", return_value="alex"):
            payload = notty.notification_payload(
                "This is a test message.", "info", "/Users/alex/GitHub/notty", "codex",
                pc="workstation.local", session="Research notty")
        self.assertEqual(payload["content"],
                         "[>_ Codex](https://chatgpt.com/codex/cloud)\n"
                         "alex:\\~/GitHub/notty · *workstation.local*\n\n"
                         "[Research notty]\n\nThis is a test message.")
        self.assertEqual(payload["content"].strip(), payload["content"])

    def test_workspace_outside_home_keeps_absolute_path(self):
        with patch.object(notty.Path, "home", return_value=Path("/Users/alex")), \
                patch.object(notty.getpass, "getuser", return_value="alex"):
            payload = notty.notification_payload("Test", "done", "/srv/workspace", "claude")
        self.assertIn("[✽ Claude Code](https://claude.ai/code)\nalex:/srv/workspace ·", payload["content"])

    def test_session_url_is_used_for_heading_only(self):
        url = "https://claude.ai/code/session_01Example"
        payload = notty.notification_payload("Test", "info", "/srv/workspace", "claude",
                                             session="Session name", session_url=url)
        self.assertTrue(payload["content"].startswith(f"[✽ Claude Code]({url})\n"))
        self.assertIn("\n\n[Session name]\n\n", payload["content"])

    def test_session_url_cannot_break_markdown_link(self):
        payload = notty.notification_payload("Test", "info", "/srv/workspace", "codex",
                                             session_url="https://chatgpt.com/codex/cloud?name=(test)")
        self.assertIn("?name=%28test%29)", payload["content"])
        for url in ("javascript:alert(1)", "https://", "https://[broken",
                    "https://user:secret@example.com", "https://example.com/\npath"):
            with self.subTest(url=url), self.assertRaises(notty.NottyError):
                notty.notification_payload("Test", "info", "/srv/workspace", "codex",
                                           session_url=url)

    def test_metadata_cannot_change_markdown_or_insert_lines(self):
        payload = notty.notification_payload("**Body stays bold**", "info", "/srv/my_project",
                                             "claude", pc="my_pc", session="[link](url)\n*bold*")
        self.assertIn(r"/srv/my\_project", payload["content"])
        self.assertIn(r"*my\_pc*", payload["content"])
        self.assertIn(r"[\[link\](url) \*bold\*]", payload["content"])
        self.assertTrue(payload["content"].endswith("**Body stays bold**"))

    def test_truncation_preserves_link_and_rejects_oversized_metadata(self):
        payload = notty.notification_payload("😀" * 2000, "info", "/srv/project", "codex",
                                             session="Release checks")
        self.assertTrue(payload["content"].startswith("[>_ Codex](https://chatgpt.com/codex/cloud)\n"))
        self.assertIn("[Release checks]\n\n", payload["content"])
        self.assertLessEqual(len(payload["content"].encode("utf-16-le")), 4000)
        with self.assertRaises(notty.NottyError):
            notty.notification_payload("Body", "info", "/srv/project", "codex", session="x" * 2000)

    def test_codex_metadata_reads_only_the_requested_session_and_newest_database(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"CODEX_HOME": directory}, clear=True):
            for version, title in ((5, "Old name"), (10, "Current session name")):
                with contextlib.closing(sqlite3.connect(Path(directory) / f"state_{version}.sqlite")) as db, db:
                    db.execute("CREATE TABLE threads (id TEXT, name TEXT, title TEXT, cwd TEXT)")
                    db.execute("INSERT INTO threads VALUES (?, ?, ?, ?)",
                               ("session-123", title, "Initial user prompt", "/projects/workspace"))
                    db.execute("INSERT INTO threads VALUES (?, ?, ?, ?)",
                               ("other-session", "Another session", "Another prompt", "/other"))
            self.assertEqual(notty.session_metadata("codex", "session-123"),
                             {"name": "Current session name", "cwd": "/projects/workspace"})
            self.assertEqual(notty.session_metadata("codex", "missing-session"), {})

    def test_codex_initial_prompt_is_not_used_as_thread_name(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"CODEX_HOME": directory}, clear=True):
            with contextlib.closing(sqlite3.connect(Path(directory) / "state_5.sqlite")) as db, db:
                db.execute("CREATE TABLE threads (id TEXT, name TEXT, title TEXT, cwd TEXT)")
                db.execute("INSERT INTO threads VALUES (?, ?, ?, ?)",
                           ("session-123", None, "Initial user prompt", "/projects/workspace"))
            self.assertEqual(notty.session_metadata("codex", "session-123"),
                             {"name": None, "cwd": "/projects/workspace"})

    def test_claude_metadata_uses_latest_saved_session_name(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": directory}, clear=True):
            transcript = Path(directory) / "projects/encoded-project/session-123.jsonl"
            transcript.parent.mkdir(parents=True)
            records = [{"type": "user", "message": "Do not use this prompt as the name"},
                       {"type": "custom-title", "customTitle": "Old name"},
                       {"type": "custom-title", "customTitle": "New session name"}]
            transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n{partial",
                                  encoding="utf-8")
            self.assertEqual(notty.session_metadata("claude", "session-123"),
                             {"name": "New session name"})
            self.assertEqual(notty.session_metadata("claude", "missing-session"), {})

    def test_claude_metadata_falls_back_to_ai_title_without_custom_title(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": directory}, clear=True):
            transcript = Path(directory) / "projects/encoded-project/session-123.jsonl"
            transcript.parent.mkdir(parents=True)
            records = [{"type": "ai-title", "aiTitle": "Generated name"},
                       {"type": "custom-title", "customTitle": "Custom name"},
                       {"type": "ai-title", "aiTitle": "Later generated name"}]
            transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n",
                                  encoding="utf-8")
            self.assertEqual(notty.session_metadata("claude", "session-123"),
                             {"name": "Custom name"})
            transcript.write_text("\n".join(json.dumps(record) for record in records[::2]) + "\n",
                                  encoding="utf-8")
            self.assertEqual(notty.session_metadata("claude", "session-123"),
                             {"name": "Later generated name"})

    def test_metadata_failure_does_not_stop_notifications(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"CODEX_HOME": directory}, clear=True):
            (Path(directory) / "state_5.sqlite").write_text("invalid database")
            (Path(directory) / "state_backup.sqlite").touch()
            self.assertEqual(notty.session_metadata("codex", "session-123"), {})

    def test_invalid_transcript_encoding_falls_back_to_unnamed_session(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": directory}):
            transcript = Path(directory) / "projects/example/session-123.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_bytes(b'\xff\xfe')
            self.assertEqual(notty.session_metadata("claude", "session-123"), {})

    def test_cli_uses_original_workspace_and_session_metadata(self):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "session-123"}, clear=True), \
                patch.object(notty.sys, "argv", ["send.py", "--message", "test"]), \
                patch.object(notty, "session_metadata", return_value={
                    "name": "Current session", "cwd": "/projects/original-workspace"}), \
                patch.object(notty, "load_webhook", return_value=WEBHOOK), \
                patch.object(notty, "send_notification") as send, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(notty.main(), 0)
            self.assertIn(":/projects/original-workspace ·", send.call_args.args[1]["content"])
            self.assertIn("[Current session]", send.call_args.args[1]["content"])

    def test_cli_explicit_names_override_metadata_and_project_alias_works(self):
        with patch.dict(os.environ, {"NOTTY_SESSION_NAME": "Environment name"}, clear=True), \
                patch.object(notty.sys, "argv", ["send.py", "--message", "test", "--agent", "claude",
                    "--project", "/projects/custom-workspace", "--session", "Custom name",
                    "--session-id", "claude-session"]), \
                patch.object(notty, "session_metadata", return_value={"name": "Saved name"}) as metadata, \
                patch.object(notty, "load_webhook", return_value=WEBHOOK), \
                patch.object(notty, "send_notification") as send, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(notty.main(), 0)
            metadata.assert_called_once_with("claude", "claude-session")
            self.assertIn(":/projects/custom-workspace ·", send.call_args.args[1]["content"])
            self.assertIn("[Custom name]", send.call_args.args[1]["content"])

    def test_cli_unknown_name_shows_session_id_without_inventing_a_title(self):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "session-123"}, clear=True), \
                patch.object(notty.sys, "argv", ["send.py", "--message", "test"]), \
                patch.object(notty, "session_metadata", return_value={}), \
                patch.object(notty, "load_webhook", return_value=WEBHOOK), \
                patch.object(notty, "send_notification") as send, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(notty.main(), 0)
            self.assertIn("[Unnamed session (session-123)]", send.call_args.args[1]["content"])

    def test_cli_session_links_use_explicit_url_or_web_id_without_guessing_local_uuid(self):
        cases = (
            (["--agent", "claude", "--session-id", "session_01Test"],
             "[✽ Claude Code](https://claude.ai/code/session_01Test)"),
            (["--agent", "claude", "--session-id", "12345678-1234-1234-1234-123456789012"],
             "[✽ Claude Code](https://claude.ai/code)"),
            (["--agent", "codex", "--session-url", "https://chatgpt.com/codex/cloud/example"],
             "[>_ Codex](https://chatgpt.com/codex/cloud/example)"),
        )
        for arguments, heading in cases:
            with self.subTest(arguments=arguments), patch.dict(os.environ, {}, clear=True), \
                    patch.object(notty.sys, "argv", ["send.py", "--message", "test", *arguments]), \
                    patch.object(notty, "session_metadata", return_value={}), \
                    patch.object(notty, "load_webhook", return_value=WEBHOOK), \
                    patch.object(notty, "send_notification") as send, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(notty.main(), 0)
                self.assertEqual(send.call_args.args[1]["content"].splitlines()[0], heading)

    def test_configuration_and_environment_override(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            config = Path(directory) / "config.json"
            config.write_text(json.dumps({"webhook_url": WEBHOOK}))
            self.assertEqual(notty.load_webhook(config), WEBHOOK)
            with patch.dict(os.environ, {"NOTTY_WEBHOOK_URL": WEBHOOK + "-override"}):
                self.assertEqual(notty.load_webhook(config), WEBHOOK + "-override")

    def test_invalid_configuration_does_not_expose_credentials(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            config = Path(directory) / "config.json"
            for contents in ("secret-token not-json", '[]', '{"webhook_url": 123}'):
                config.write_text(contents)
                with self.assertRaises(notty.NottyError) as caught:
                    notty.load_webhook(config)
                self.assertNotIn("secret-token", str(caught.exception))

    def test_configuration_discovery_prefers_project_then_installation_then_user(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "project/subdirectory"
            install = root / "installation/skills/notty/scripts/send.py"
            home = root / "home"
            work.mkdir(parents=True)
            install.parent.mkdir(parents=True)
            user_config = home / ".config/notty/config.json"
            user_config.parent.mkdir(parents=True)
            project_config = work.parent / ".notty.json"
            install_config = root / "installation/.notty.json"
            configs = (project_config, install_config, user_config)
            for index, config in enumerate(configs):
                config.write_text(json.dumps({"webhook_url": WEBHOOK + str(index)}))
            with patch.object(notty.Path, "cwd", return_value=work), \
                    patch.object(notty.Path, "home", return_value=home), \
                    patch.object(notty, "__file__", str(install)):
                for index, config in enumerate(configs):
                    self.assertEqual(notty.load_webhook(), WEBHOOK + str(index))
                    config.unlink()
                with self.assertRaises(notty.NottyError):
                    notty.load_webhook()

    def test_explicit_configuration_never_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps({"webhook_url": WEBHOOK}))
            with patch.dict(os.environ, {"NOTTY_CONFIG_FILE": str(config)}):
                self.assertEqual(notty.load_webhook(), WEBHOOK)
                with self.assertRaises(notty.NottyError):
                    notty.load_webhook(Path(directory) / "missing.json")
            config.write_bytes(b'\xff\xfe')
            with self.assertRaises(notty.NottyError):
                notty.load_webhook(config)

    def test_redirects_never_forward_the_webhook_request(self):
        handler = notty.NoRedirects()
        handler.parent = Mock()
        with io.BytesIO() as response:
            result = handler.http_error_302(notty.request.Request(WEBHOOK, data=b'{}'), response,
                                            302, "Found", {"location": "https://example.invalid/target"})
        self.assertIsNone(result)
        handler.parent.open.assert_not_called()

    def test_post_is_confirmed_and_contains_utf8_payload(self):
        payload = notty.notification_payload("Unicode message 😀", "done", "project")
        with patch.object(notty.request, "build_opener") as build:
            build.return_value.open.return_value.__enter__.return_value = io.BytesIO(b'{"id":"789"}')
            notty.send_notification(WEBHOOK, payload)
            req = build.return_value.open.call_args.args[0]
            self.assertEqual(req.get_method(), "POST")
            self.assertEqual(json.loads(req.data), payload)
            self.assertEqual(parse_qs(urlsplit(req.full_url).query)["wait"], ["true"])

    def test_network_and_http_errors_never_retry_or_print_webhook(self):
        failures = (HTTPError(WEBHOOK, 401, "bad token", {}, None),
                    HTTPError(WEBHOOK, 429, "rate limited", {}, None),
                    URLError(WEBHOOK), TimeoutError(WEBHOOK), IncompleteRead(b'partial'))
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(notty.request, "build_opener") as build, \
                    patch.dict(os.environ, {"NOTTY_WEBHOOK_URL": WEBHOOK}, clear=True), \
                    patch.object(notty.sys, "argv", ["send.py", "--message", "test"]), \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                build.return_value.open.side_effect = failure
                self.assertEqual(notty.main(), 1)
                self.assertNotIn("test-secret-token", stderr.getvalue())
                self.assertEqual(build.return_value.open.call_count, 1)
                if isinstance(failure, HTTPError):
                    self.assertTrue(failure.closed)

    def test_success_requires_a_discord_message_id(self):
        for response in (b'{}', b'[]', b'not-json', b'{"id":true}', b'{"id":""}', b'{"id":"invalid"}'):
            with self.subTest(response=response), patch.object(notty.request, "build_opener") as build:
                build.return_value.open.return_value.__enter__.return_value = io.BytesIO(response)
                with self.assertRaises(notty.NottyError):
                    notty.send_notification(WEBHOOK, {})

    def test_invalid_input_encoding_fails_without_sending(self):
        with patch.object(notty.sys, "argv", ["send.py", "--message", "\udcff"]), \
                patch.object(notty, "send_notification") as send, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(notty.main(), 1)
            self.assertIn("UTF-8", stderr.getvalue())
            send.assert_not_called()

    def test_stdin_message(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(notty.sys, "argv", ["send.py", "--status", "blocked"]), \
                patch.object(notty.sys, "stdin", io.StringIO("Which configuration should I use?")), \
                patch.object(notty, "load_webhook", return_value=WEBHOOK), \
                patch.object(notty, "send_notification") as send, \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(notty.main(), 0)
            self.assertIn("Which configuration", send.call_args.args[1]["content"])
            self.assertIn("delivered", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
