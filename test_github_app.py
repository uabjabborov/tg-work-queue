import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from telegram.error import Forbidden, TimedOut

from database import Database
from github_app import GitHubApp, GitHubConnectionError, get_github_app
from github_commands import handle_github_command
from review_metadata import ReviewMetadataClient, refresh_review_metadata
from scheduler import send_reminder

with patch.object(Database, "_init_db"):
    import bot


class GitHubAppTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.pem = cls.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()).decode()

    def setUp(self):
        self.app = GitHubApp("123", self.pem)
        self.requests = []
        self.repo_admin = True
        self.oauth_error = None
        self.suspended = False

    def respond(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == "/app":
            payload = {"client_id": "client-id", "slug": "queue-app"}
        elif path == "/login/device/code":
            payload = {"device_code": "device-secret", "user_code": "ABCD-EFGH", "expires_in": 900, "interval": 5}
        elif path == "/login/oauth/access_token":
            payload = ({"error": self.oauth_error} if self.oauth_error else
                       {"access_token": "user-secret", "refresh_token": "refresh-secret"})
        elif path == "/user":
            payload = {"id": 99, "login": "alice"}
        elif path == "/repos/team/repo":
            payload = {"id": 88, "full_name": "Team/Repo", "permissions": {"admin": self.repo_admin}}
        elif path == "/repos/team/repo/collaborators/alice/permission":
            payload = {"permission": "admin" if self.repo_admin else "read", "user": {"id": 99}}
        elif path == "/repos/team/repo/installation":
            payload = {"id": 77, "suspended_at": "now" if self.suspended else None}
        elif path == "/app/installations/77/access_tokens":
            payload = {"token": "installation-secret"}
        else:
            return httpx.Response(404)
        return httpx.Response(200, json=payload)

    def http(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.respond))

    def test_app_jwt_is_rsa_signed_short_lived_and_clock_skew_tolerant(self):
        claims = jwt.decode(self.app.app_jwt(), self.key.public_key(), algorithms=["RS256"])
        self.assertEqual(claims["iss"], "123")
        self.assertLess(claims["iat"], time.time())
        self.assertLessEqual(claims["exp"], time.time() + 600)

    async def test_device_flow_checks_user_permissions_and_scopes_installation_token(self):
        async with self.http() as http:
            link, code = await self.app.start(http, -1, 7, "Team/Repo")
            self.assertEqual(link, "https://github.com/apps/queue-app/installations/new")
            self.assertEqual(code, "ABCD-EFGH")
            connection = await self.app.finish(http, -1, 7)
        self.assertEqual(connection, {"repository": "team/repo", "repository_id": 88,
                                      "installation_id": 77, "github_user_id": 99})
        self.assertEqual(self.app.pending, {})
        repo_request = next(request for request in self.requests if request.url.path == "/repos/team/repo")
        self.assertEqual(repo_request.headers["Authorization"], "Bearer user-secret")
        token_request = self.requests[-1]
        self.assertEqual(json.loads(token_request.content), {"repository_ids": [88], "permissions": {"pull_requests": "read"}})
        self.assertNotIn("device-secret", repr(connection))
        self.assertNotIn("user-secret", repr(connection))

    async def test_device_flow_is_bound_to_chat_and_user_and_can_only_finish_once(self):
        async with self.http() as http:
            await self.app.start(http, -1, 7, "team/repo")
            for chat, user in ((-2, 7), (-1, 8)):
                with self.assertRaises(GitHubConnectionError):
                    await self.app.finish(http, chat, user)
            await self.app.finish(http, -1, 7)
            with self.assertRaises(GitHubConnectionError):
                await self.app.finish(http, -1, 7)

    async def test_pending_slowdown_denial_and_expiration(self):
        async with self.http() as http:
            await self.app.start(http, -1, 7, "team/repo")
            self.oauth_error = "authorization_pending"
            with self.assertRaises(GitHubConnectionError):
                await self.app.finish(http, -1, 7)
            count = len(self.requests)
            with self.assertRaises(GitHubConnectionError):
                await self.app.finish(http, -1, 7)
            self.assertEqual(len(self.requests), count)
            login = self.app.pending[(-1, 7)]
            login.next_poll = 0
            self.oauth_error = "slow_down"
            with self.assertRaises(GitHubConnectionError):
                await self.app.finish(http, -1, 7)
            self.assertEqual(login.interval, 10)
            login.next_poll = 0
            self.oauth_error = "access_denied"
            with self.assertRaises(GitHubConnectionError):
                await self.app.finish(http, -1, 7)
            self.assertEqual(self.app.pending, {})
            await self.app.start(http, -1, 7, "team/repo")
            self.app.pending[(-1, 7)].expires_at = 0
            with self.assertRaises(GitHubConnectionError):
                await self.app.finish(http, -1, 7)

    async def test_read_access_or_suspended_installation_cannot_connect_repository(self):
        async with self.http() as http:
            for repo_admin, suspended in ((False, False), (True, True)):
                self.repo_admin, self.suspended = repo_admin, suspended
                await self.app.start(http, -1, 7, "team/repo")
                with self.assertRaises(GitHubConnectionError):
                    await self.app.finish(http, -1, 7)
                self.assertEqual(self.app.pending, {})

    async def test_cancel_during_verification_prevents_completion(self):
        async with self.http() as http:
            await self.app.start(http, -1, 7, "team/repo")

            async def verify(*args):
                self.app.cancel_repository(-1, "team/repo")
                return {"repository": "team/repo"}

            with patch.object(self.app, "verify_repository", side_effect=verify):
                with self.assertRaises(GitHubConnectionError):
                    await self.app.finish(http, -1, 7)

    async def test_api_redirects_and_errors_do_not_leak_credentials(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(302, headers={"Location": "https://other.example/"}, text="secret-provider-body")

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as http:
            with self.assertRaises(GitHubConnectionError) as caught:
                await self.app.request(http, "GET", "/user", "secret-token")
        self.assertEqual(len(requests), 1)
        self.assertNotIn("secret", str(caught.exception))

    def test_app_configuration_loads_only_the_operator_key_file(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "app.pem"
            key_path.write_text(self.pem)
            with patch("github_app._configuration", None), patch("github_app._app", None), patch.dict(
                "os.environ", {"GITHUB_APP_ID": "123", "GITHUB_APP_PRIVATE_KEY_FILE": str(key_path)}, clear=True
            ):
                configured = get_github_app()
                self.assertIsInstance(configured, GitHubApp)
                self.assertIs(configured, get_github_app())
            with patch("github_app._configuration", None), patch("github_app._app", None), patch.dict(
                "os.environ", {"GITHUB_TOKEN": "unrelated-team-secret"}, clear=True
            ):
                self.assertIsNone(get_github_app())
            key_path.write_text("invalid-key")
            with patch("github_app._configuration", None), patch("github_app._app", None), patch.dict(
                "os.environ", {"GITHUB_APP_ID": "123", "GITHUB_APP_PRIVATE_KEY_FILE": str(key_path)}, clear=True
            ):
                self.assertIsNone(get_github_app())


class ConnectionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Database(str(Path(directory.name) / "test.db"))
        self.app = SimpleNamespace(installation_token=AsyncMock(return_value="scoped-token"))
        self.client = ReviewMetadataClient()
        self.client.fetch = AsyncMock(return_value=("Private title", "Private description"))

    def add(self, chat_id=1, repo="repo", number=12):
        self.db.add_task(chat_id, f"{repo}/{number}", f"https://github.com/team/{repo}/pull/{number}",
                         ["@alice"], "@alice", 7)
        return self.db.get_tasks(chat_id)[-1]

    def connect(self, chat_id=1):
        self.db.connect_github(chat_id, "team/repo", 88, 77, 7, 99)

    async def test_private_access_requires_exact_chat_and_repository_binding(self):
        task = self.add()
        other_chat = self.add(chat_id=2)
        other_repo = self.add(repo="other")
        self.connect()

        async def fetch(http, url, **kwargs):
            return ("Private title", "Private description") if kwargs.get("token") else None

        self.client.fetch.side_effect = fetch
        with patch("review_metadata.get_github_app", return_value=self.app):
            await refresh_review_metadata(self.db, [task, other_chat, other_repo], self.client)
        self.assertEqual(task.title, "Private title")
        self.assertIsNone(other_chat.title)
        self.assertIsNone(other_repo.title)
        self.app.installation_token.assert_awaited_once()
        self.assertIsNone(self.db.get_review_metadata(1, task.url))
        self.assertIsNone(self.db.get_tasks(1)[0].title)
        with self.db._get_connection() as conn:
            stored = "\n".join(conn.iterdump())
        self.assertNotIn("Private description", stored)
        self.assertNotIn("scoped-token", stored)

    async def test_shared_repository_in_a_batch_gets_one_scoped_token(self):
        tasks = [self.add(number=number) for number in (12, 13)]
        self.connect()
        with patch("review_metadata.get_github_app", return_value=self.app):
            await refresh_review_metadata(self.db, tasks, self.client)
        self.app.installation_token.assert_awaited_once()
        self.assertTrue(all(task.title == "Private title" for task in tasks))

    async def test_revocation_or_missing_configuration_hides_previous_private_details(self):
        task = self.add()
        self.connect()
        with patch("review_metadata.get_github_app", return_value=self.app):
            await refresh_review_metadata(self.db, [task], self.client)
            self.assertEqual(task.title, "Private title")
            self.app.installation_token.side_effect = GitHubConnectionError("Revoked")
            await refresh_review_metadata(self.db, [task], self.client)
            self.assertIsNone(task.title)
        task.title, task.description = "Stale private title", "Stale private body"
        with patch("review_metadata.get_github_app", return_value=None):
            await refresh_review_metadata(self.db, [task], self.client)
        self.assertIsNone(task.title)
        self.assertIsNone(task.description)

    async def test_disconnect_during_fetch_discards_inflight_private_data(self):
        task = self.add()
        self.connect()

        async def fetch(*args, **kwargs):
            self.db.disconnect_github(1, "team/repo")
            return "Private title", "Private body"

        self.client.fetch.side_effect = fetch
        with patch("review_metadata.get_github_app", return_value=self.app):
            await refresh_review_metadata(self.db, [task], self.client)
        self.assertIsNone(task.title)
        self.assertIsNone(self.db.get_review_metadata(1, task.url))

    async def test_disconnect_clears_only_its_chat_and_keeps_queue(self):
        task = self.add()
        self.add(chat_id=2)
        self.connect()
        self.connect(chat_id=2)
        self.db.save_review_metadata(1, task.url, "Cached", "Body")
        self.db.save_review_metadata(2, task.url, "Other cached", "Other body")
        self.assertTrue(self.db.disconnect_github(1, "team/repo"))
        self.assertIsNone(self.db.get_github_connection(1, "team/repo"))
        self.assertIsNotNone(self.db.get_github_connection(2, "team/repo"))
        self.assertIsNone(self.db.get_review_metadata(1, task.url))
        self.assertEqual(self.db.get_review_metadata(2, task.url)["title"], "Other cached")
        self.assertEqual(len(self.db.get_tasks(1)), 1)

    async def test_private_pr_response_must_match_bound_repository_id(self):
        client = ReviewMetadataClient()
        task = self.add()
        for repository_id in (88, 999):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                200, json={"title": "Title", "body": "Body", "base": {"repo": {"id": repository_id}}}
            ))) as http:
                result = await client.fetch(http, task.url, token="scoped", repository_id=88)
            self.assertEqual(result, ("Title", "Body") if repository_id == 88 else None)

    async def test_reminders_recheck_connected_repository_access(self):
        self.add()
        self.connect()
        application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        with patch("review_metadata.get_github_app", return_value=self.app), patch(
            "review_metadata.ReviewMetadataClient", return_value=self.client
        ):
            await send_reminder(1, application, self.db)
            self.assertIn("Private title", application.bot.send_message.await_args.kwargs["text"])
            self.app.installation_token.side_effect = GitHubConnectionError("Revoked")
            await send_reminder(1, application, self.db)
            self.assertNotIn("Private title", application.bot.send_message.await_args.kwargs["text"])


class GitHubCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Database(str(Path(directory.name) / "test.db"))
        self.reply = AsyncMock(return_value=True)
        self.context = SimpleNamespace(bot=SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="administrator"))))
        self.app = SimpleNamespace(start=AsyncMock(return_value=("https://github.com/apps/test/installations/new", "ABCD-EFGH")),
                                   finish=AsyncMock(return_value={"repository": "team/repo", "repository_id": 88,
                                                                 "installation_id": 77, "github_user_id": 99}),
                                   cancel=lambda *args: None, cancel_repository=lambda *args: None)

    def update(self, text):
        return SimpleNamespace(effective_chat=SimpleNamespace(id=-1, type="supergroup", title="Team"),
                               effective_user=SimpleNamespace(id=7, is_bot=False),
                               message=SimpleNamespace(text=text, reply_text=AsyncMock(), is_topic_message=False))

    async def send(self, text):
        update = self.update(text)
        with patch("github_commands.get_github_app", return_value=self.app):
            await handle_github_command(update, self.context, self.db, self.reply)
        return update

    async def test_start_only_shares_code_privately_and_finish_connects_exact_repository(self):
        update = await self.send("!wgithub connect team/repo")
        update.message.reply_text.assert_not_awaited()
        self.assertTrue(self.reply.await_args.kwargs["verify_private"])
        self.assertIn("ABCD-EFGH", self.reply.await_args.args[2][0])
        self.assertIn("every member", self.reply.await_args.args[2][0])
        self.assertIsNone(self.db.get_github_connection(-1, "team/repo"))
        await self.send("!wgithub finish")
        self.assertEqual(self.db.get_github_connection(-1, "team/repo")["connected_by"], 7)
        await self.send("!wgithub disconnect team/repo")
        self.assertIsNone(self.db.get_github_connection(-1, "team/repo"))

    async def test_non_admin_cannot_start_finish_or_disconnect(self):
        self.context.bot.get_chat_member.return_value.status = "member"
        for text in ("!wgithub connect team/repo", "!wgithub finish", "!wgithub disconnect team/repo", "!wgithub"):
            update = await self.send(text)
            self.assertIn("administrator", update.message.reply_text.await_args.args[0])
        self.app.start.assert_not_awaited()
        self.app.finish.assert_not_awaited()

    async def test_admin_revocation_during_authorization_prevents_binding(self):
        self.context.bot.get_chat_member.side_effect = [SimpleNamespace(status="administrator"), SimpleNamespace(status="member")]
        await self.send("!wgithub finish")
        self.assertIsNone(self.db.get_github_connection(-1, "team/repo"))

    async def test_failed_private_delivery_cancels_device_login(self):
        self.reply.return_value = False
        with patch.object(self.app, "cancel") as cancel:
            await self.send("!wgithub connect team/repo")
        cancel.assert_called_once_with(-1, 7)

    async def test_committed_actions_report_outcome_when_private_send_or_edit_fails(self):
        telegram_bot = self.context.bot
        telegram_bot.id = 99
        sent = SimpleNamespace(message_id=0, api_kwargs={
            "ephemeral_message_id": 321, "receiver_user": {"id": 7}})
        for failing_method in ("send_message", "_post"):
            for error in (Forbidden("not permitted"), TimedOut()):
                with self.subTest(method=failing_method, error=error):
                    telegram_bot.send_message = AsyncMock(return_value=sent)
                    telegram_bot._post = AsyncMock(return_value=True)
                    getattr(telegram_bot, failing_method).side_effect = error
                    for command, outcome in (("finish", "connected to this chat"),
                                             ("disconnect team/repo", "disconnected from this chat"),
                                             ("cancel", "has been cancelled")):
                        update = self.update("!wgithub " + command)
                        with patch("github_commands.get_github_app", return_value=self.app):
                            await handle_github_command(update, self.context, self.db, bot.reply_privately)
                        notice = update.message.reply_text.await_args.args[0]
                        self.assertIn(outcome, notice)
                        self.assertNotIn("team/repo", notice)
                        self.assertNotIn("try again", notice)
                        self.assertNotIn("send the command again", notice)
                        connection = self.db.get_github_connection(-1, "team/repo")
                        self.assertEqual(connection is not None, command == "finish")
                    self.assertTrue(all(call.args[1] == "Preparing your private reply…"
                                        for call in telegram_bot.send_message.await_args_list))

    async def test_failed_direct_message_delivery_cancels_device_login(self):
        update = self.update("!wgithub connect team/repo")
        update.effective_chat.type = "private"
        update.message.reply_text.side_effect = Forbidden("blocked")
        with patch("github_commands.get_github_app", return_value=self.app), patch.object(self.app, "cancel") as cancel:
            await handle_github_command(update, self.context, self.db, bot.reply_privately)
        cancel.assert_called_once_with(-1, 7)


if __name__ == "__main__":
    unittest.main()
