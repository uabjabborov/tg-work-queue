import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from telegram.error import Forbidden

from database import Database
from presentation import my_reviews, visible_length
from review_metadata import ReviewMetadataClient, refresh_review_metadata
from scheduler import send_reminder

with patch.object(Database, "_init_db"):
    import bot

GITHUB_URL = "https://github.com/team/repo/pull/12"
GITLAB_URL = "http://git.example:8443/gitlab/team/subgroup/repo/-/merge_requests/0012?tab=x#note"


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_github_requests_are_anonymous_even_with_tokens_in_environment(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"title": "PR title", "body": "PR body"})

        with patch.dict("os.environ", {"GITHUB_TOKEN": "github-secret", "GH_TOKEN": "gh-secret",
                                       "GITLAB_URL": "https://git.example:8443/gitlab", "GITLAB_TOKEN": "gitlab-secret"}):
            client = ReviewMetadataClient()
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                self.assertEqual(await client.fetch(http, GITHUB_URL), ("PR title", "PR body"))
                self.assertIsNone(await client.fetch(http, GITLAB_URL))
        self.assertEqual(len(requests), 1)
        github = requests[0]
        self.assertEqual(str(github.url), "https://api.github.com/repos/team/repo/pulls/12")
        self.assertNotIn("Authorization", github.headers)
        self.assertNotIn("PRIVATE-TOKEN", github.headers)

    def test_non_github_hosts_and_unsafe_paths_are_not_requested(self):
        client = ReviewMetadataClient()
        for url in (
            "https://evil.example/team/repo/-/merge_requests/1",
            "https://git.example/team/repo/-/merge_requests/1",
            "https://git.example:8443/gitlab-other/team/repo/-/merge_requests/1",
            "https://user@git.example:8443/gitlab/team/repo/-/merge_requests/1",
            "https://git.example:8443/gitlab/team/%2e%2e/repo/-/merge_requests/1",
            "https://github.com/%2e%2e/repo/pull/1",
        ):
            with self.subTest(url=url):
                self.assertIsNone(client.request_for(url))
        self.assertIsNone(ReviewMetadataClient().request_for(GITLAB_URL))
        self.assertNotIn("Authorization", ReviewMetadataClient().request_for(GITHUB_URL)[1])

    async def test_errors_malformed_payloads_and_redirects_fall_back(self):
        client = ReviewMetadataClient()
        for response in (
            httpx.Response(401), httpx.Response(403), httpx.Response(404), httpx.Response(429),
            httpx.Response(500), httpx.Response(302, headers={"Location": "https://evil.example"}),
            httpx.Response(200, text="not json"), httpx.Response(200, json=[]),
            httpx.Response(200, json={"title": 123}),
            httpx.Response(200, json={"title": "title", "body": ["bad"]}),
        ):
            calls = []

            def respond(request):
                calls.append(request)
                return response

            async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=False) as http:
                self.assertIsNone(await client.fetch(http, GITHUB_URL))
                self.assertEqual(len(calls), 1)

    async def test_timeouts_and_empty_descriptions(self):
        client = ReviewMetadataClient()
        http = SimpleNamespace(get=AsyncMock(side_effect=httpx.ReadTimeout("timed out")))
        self.assertIsNone(await client.fetch(http, GITHUB_URL))
        for body in (None, ""):
            response = httpx.Response(200, json={"title": "Title", "body": body},
                                      request=httpx.Request("GET", "https://api.github.com"))
            http.get = AsyncMock(return_value=response)
            self.assertEqual(await client.fetch(http, GITHUB_URL), ("Title", ""))


class MetadataIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Database(str(Path(directory.name) / "test.db"))
        self.client = ReviewMetadataClient()
        self.client.fetch = AsyncMock(return_value=("Fix <login> & logout", "Description <b>literal</b> & details"))

    def add(self, number=12, chat_id=1):
        url = f"https://github.com/team/repo/pull/{number}"
        self.db.add_task(chat_id, f"repo/{number}", url, ["@alice"], "@alice", 7)
        return self.db.get_tasks(chat_id)[-1]

    async def test_batch_deadline_keeps_completed_details_and_cancels_pending_work(self):
        tasks = [self.add(number=i) for i in range(1, 21)]
        started, cancelled = [], []

        async def fetch(http, url):
            started.append(url)
            if url == tasks[0].url:
                return "Fast title", "Fast description"
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(url)

        self.client.fetch.side_effect = fetch
        with patch("review_metadata.BATCH_TIMEOUT", 0.03):
            await asyncio.wait_for(refresh_review_metadata(self.db, tasks, self.client), 2)
        self.assertEqual(tasks[0].title, "Fast title")
        self.assertEqual(len(started), 5)
        self.assertCountEqual(cancelled, started[1:])
        self.assertTrue(all(task.title is None for task in tasks[1:]))
        self.assertTrue(all(self.db.get_review_metadata(1, task.url) is None for task in tasks[1:]))

    async def test_token_request_is_cancelled_at_batch_deadline(self):
        tasks = [self.add(number=i) for i in range(1, 6)]
        self.db.connect_github(1, "team/repo", 88, 77, 7, 99)
        cancelled = asyncio.Event()

        async def token(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        app = SimpleNamespace(installation_token=AsyncMock(side_effect=token))
        with patch("review_metadata.get_github_app", return_value=app), patch("review_metadata.BATCH_TIMEOUT", 0.03):
            await asyncio.wait_for(refresh_review_metadata(self.db, tasks, self.client), 2)
        self.assertTrue(cancelled.is_set())
        app.installation_token.assert_awaited_once()
        self.client.fetch.assert_not_awaited()

    async def test_persistent_cache_duplicates_refresh_and_stale_fallback(self):
        task = self.add()
        duplicate = self.db.get_tasks(1)[0]
        await refresh_review_metadata(self.db, [task, duplicate], self.client)
        self.client.fetch.assert_awaited_once()
        self.assertEqual(duplicate.title, task.title)
        reopened = Database(self.db.db_path)
        self.assertEqual(reopened.get_tasks(1)[0].description, task.description)
        await refresh_review_metadata(reopened, [task], self.client)
        self.client.fetch.assert_awaited_once()

        stale = datetime.now(timezone.utc) - timedelta(minutes=6)
        self.db.save_review_metadata(1, task.url, "Cached title", "Cached body", now=stale)
        self.client.fetch.return_value = None
        await refresh_review_metadata(self.db, [task], self.client)
        self.assertEqual((task.title, task.description), ("Cached title", "Cached body"))
        await refresh_review_metadata(self.db, [task], self.client)
        self.assertEqual(self.client.fetch.await_count, 2)

        self.db.save_review_metadata(1, task.url, None, None, now=stale)
        self.client.fetch.return_value = ("New title", "")
        await refresh_review_metadata(self.db, [task], self.client)
        self.assertEqual((task.title, task.description), ("New title", ""))

    async def test_failed_first_lookup_is_cached_without_losing_queue_entry(self):
        task = self.add()
        self.client.fetch.return_value = None
        await refresh_review_metadata(self.db, [task], self.client)
        await refresh_review_metadata(self.db, [task], self.client)
        self.client.fetch.assert_awaited_once()
        self.assertIsNone(self.db.get_tasks(1)[0].title)

    async def test_other_chats_cannot_read_or_update_cached_details(self):
        first = self.add(chat_id=1)
        second = self.add(chat_id=2)
        await refresh_review_metadata(self.db, [first], self.client)
        self.assertIsNone(self.db.get_review_metadata(2, first.url))
        self.assertIsNone(self.db.get_tasks(2)[0].title)

        self.client.fetch.return_value = None
        await refresh_review_metadata(self.db, [second], self.client)
        self.assertIsNone(second.title)
        self.assertEqual(self.db.get_tasks(1)[0].title, first.title)
        self.assertEqual(self.client.fetch.await_count, 2)

        self.db.save_review_metadata(2, second.url, "Other chat title", "Other chat description")
        self.assertEqual(self.db.get_tasks(1)[0].title, first.title)
        self.assertEqual(self.db.get_tasks(2)[0].title, "Other chat title")
        for task in (first, second):
            self.db.complete_task(task.chat_id, task.id, "done", "@reviewer", 8)
        finished = self.db.get_finished_reviews(7, "@alice", datetime.now(timezone.utc) - timedelta(days=1))
        self.assertEqual([(review.chat_id, review.title) for review in finished],
                         [(1, first.title), (2, "Other chat title")])

    async def test_mixed_chat_batch_does_not_reuse_metadata_across_chats(self):
        first = self.add(chat_id=1)
        second = self.add(chat_id=2)
        await refresh_review_metadata(self.db, [first, second, first], self.client)
        self.assertEqual(self.client.fetch.await_count, 2)
        self.assertIsNotNone(self.db.get_review_metadata(1, first.url))
        self.assertIsNotNone(self.db.get_review_metadata(2, second.url))

    def test_legacy_shared_cache_is_discarded_without_changing_queue_or_history(self):
        first = self.add(chat_id=1)
        self.add(chat_id=2)
        self.db.complete_task(1, first.id, "done", "@reviewer", 8)
        with self.db._get_connection() as conn:
            conn.execute("DROP TABLE review_metadata")
            conn.execute("CREATE TABLE review_metadata (url TEXT PRIMARY KEY, title TEXT, description TEXT, checked_at TEXT)")
            conn.execute("INSERT INTO review_metadata VALUES (?, ?, ?, ?)",
                         (GITHUB_URL, "Private legacy title", "Private legacy description",
                          datetime.now(timezone.utc).isoformat()))
        migrated = Database(self.db.db_path)
        self.assertIsNone(migrated.get_review_metadata(1, GITHUB_URL))
        self.assertIsNone(migrated.get_review_metadata(2, GITHUB_URL))
        self.assertEqual(len(migrated.get_tasks(2)), 1)
        self.assertIsNone(migrated.get_tasks(2)[0].title)
        finished = migrated.get_finished_reviews(7, "@alice", datetime.now(timezone.utc) - timedelta(days=1))
        self.assertEqual(len(finished), 1)
        self.assertIsNone(finished[0].title)

    async def test_gitlab_queue_commands_work_without_metadata_requests(self):
        update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
        with patch.object(bot, "db", self.db), patch(
            "review_metadata.ReviewMetadataClient", return_value=self.client
        ):
            await bot.handle_wadd(update, 1, [("repo/12", GITLAB_URL, ["@alice"])], "@alice", 7)
            await bot.handle_w(update, 1)
        self.client.fetch.assert_not_awaited()
        self.assertIn("repo/12", update.message.reply_text.await_args.args[0])
        self.assertEqual(self.db.get_tasks(1)[0].url, GITLAB_URL)
        self.assertIsNone(self.db.get_review_metadata(1, GITLAB_URL))

    async def test_descriptions_appear_in_personal_view_only(self):
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=1, type="private"),
            effective_user=SimpleNamespace(id=7, username="alice", first_name="Alice", is_bot=False),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        with patch.object(bot, "db", self.db), patch(
            "review_metadata.ReviewMetadataClient", return_value=self.client
        ):
            await bot.handle_wadd(update, 1, [("repo/12", GITHUB_URL, ["@alice"])], "@alice", 7)
            added = update.message.reply_text.await_args.args[0]
            self.assertIn("Fix &lt;login&gt; &amp; logout", added)
            self.assertNotIn("Description", added)

            await bot.handle_w(update, 1)
            listing = update.message.reply_text.await_args.args[0]
            self.assertIn("Fix &lt;login&gt;", listing)
            self.assertNotIn("Description", listing)

            await bot.handle_wme(update, SimpleNamespace(), 1)
            personal = update.message.reply_text.await_args.args[0]
            self.assertIn("Description &lt;b&gt;literal&lt;/b&gt;", personal)

        application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        await send_reminder(1, application, self.db)
        self.assertIn("Fix &lt;login&gt;", application.bot.send_message.await_args.kwargs["text"])
        self.assertNotIn("Description", application.bot.send_message.await_args.kwargs["text"])
        self.client.fetch.assert_awaited_once()

    async def test_group_descriptions_are_addressed_only_to_the_requester(self):
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=1, type="supergroup"),
            effective_user=SimpleNamespace(id=7, username="alice", first_name="Alice", is_bot=False),
            message=SimpleNamespace(reply_text=AsyncMock(), is_topic_message=False),
        )
        telegram_bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=0, api_kwargs={
                "ephemeral_message_id": 321, "receiver_user": {"id": 7}})),
            _post=AsyncMock(return_value=True),
        )
        with patch.object(bot, "db", self.db), patch(
            "review_metadata.ReviewMetadataClient", return_value=self.client
        ):
            await bot.handle_wadd(update, 1, [("repo/12", GITHUB_URL, ["@alice"])], "@alice", 7)
            self.assertNotIn("Description", update.message.reply_text.await_args.args[0])
            update.message.reply_text.reset_mock()
            await bot.handle_wme(update, SimpleNamespace(bot=telegram_bot), 1)

        update.message.reply_text.assert_not_awaited()
        telegram_bot.send_message.assert_awaited_once()
        args, kwargs = telegram_bot.send_message.await_args
        self.assertNotIn("Description", args[1])
        self.assertIn("Description &lt;b&gt;literal&lt;/b&gt; &amp; details",
                      telegram_bot._post.await_args.kwargs["data"]["text"])
        self.assertEqual(kwargs["api_kwargs"], {"ephemeral_message_parameters": {"receiver_user_id": 7}})

    async def test_failed_private_delivery_does_not_fall_back_to_public_descriptions(self):
        self.add()
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=1, type="supergroup"),
            effective_user=SimpleNamespace(id=7, username="alice", first_name="Alice", is_bot=False),
            message=SimpleNamespace(reply_text=AsyncMock(), is_topic_message=False),
        )
        telegram_bot = SimpleNamespace(
            id=99,
            send_message=AsyncMock(side_effect=Forbidden("not allowed")),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        )
        with patch.object(bot, "db", self.db), patch(
            "review_metadata.ReviewMetadataClient", return_value=self.client
        ):
            await bot.handle_wme(update, SimpleNamespace(bot=telegram_bot), 1)

        notice = update.message.reply_text.await_args.args[0]
        self.assertIn("couldn't reply privately", notice)
        self.assertNotIn("Description", notice)
        self.assertNotIn("literal", notice)

    async def test_finished_reviews_keep_metadata_and_old_items_get_enriched(self):
        for number, outcome in ((12, "done"), (13, "bounce")):
            task = self.add(number)
            if number == 12:
                await refresh_review_metadata(self.db, [task], self.client)
            self.db.complete_task(1, task.id, outcome, "@reviewer", 8)
        finished = self.db.get_finished_reviews(7, "@alice", datetime.now(timezone.utc) - timedelta(days=1))
        self.assertIsNotNone(finished[0].title)
        self.assertIsNone(finished[1].title)
        await refresh_review_metadata(self.db, finished, self.client)
        messages = "\n".join(my_reviews([], finished))
        self.assertEqual(messages.count("Description &lt;b&gt;literal&lt;/b&gt;"), 2)

    async def test_wme_does_not_fetch_reviews_from_groups_the_sender_left(self):
        self.add(12, chat_id=2)
        self.add(13, chat_id=3)
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=1, type="private"),
            effective_user=SimpleNamespace(id=7, username="alice", first_name="Alice", is_bot=False),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        context = SimpleNamespace(bot=SimpleNamespace(
            get_chat_member=AsyncMock(side_effect=lambda chat_id, user_id:
                                     SimpleNamespace(status="member" if chat_id == 2 else "left")),
            get_chat=AsyncMock(return_value=SimpleNamespace(title="Team")),
        ))
        with patch.object(bot, "db", self.db), patch(
            "review_metadata.ReviewMetadataClient", return_value=self.client
        ):
            await bot.handle_wme(update, context, 1)
        self.client.fetch.assert_awaited_once()
        self.assertEqual(self.client.fetch.await_args.args[1], GITHUB_URL)
        self.assertNotIn("repo/13", update.message.reply_text.await_args.args[0])

    def test_existing_database_gains_cache_without_losing_queue(self):
        self.add()
        with self.db._get_connection() as conn:
            conn.execute("DROP TABLE review_metadata")
        migrated = Database(self.db.db_path)
        self.assertEqual(migrated.get_tasks(1)[0].url, GITHUB_URL)
        migrated.save_review_metadata(1, GITHUB_URL, "Title", "Body")
        self.assertEqual(migrated.get_tasks(1)[0].title, "Title")

    async def test_long_unicode_descriptions_split_into_valid_telegram_messages(self):
        tasks = [self.add(number) for number in range(10)]
        self.client.fetch.return_value = ("😀<&>" * 300, "😀<&>\n" * 3000)
        await refresh_review_metadata(self.db, tasks, self.client)
        messages = my_reviews(tasks, [])
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(visible_length(message) <= 4096 for message in messages))
        self.assertEqual("\n".join(messages).count("…"), 20)
        self.assertNotIn("�", "\n".join(messages))

    async def test_fetch_concurrency_is_bounded(self):
        tasks = [self.add(number) for number in range(10)]
        active = peak = 0

        async def fetch(http, url):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.001)
            active -= 1
            return "Title", "Body"

        self.client.fetch.side_effect = fetch
        await refresh_review_metadata(self.db, tasks, self.client)
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 4)


if __name__ == "__main__":
    unittest.main()
