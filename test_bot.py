import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut

from database import Database
from presentation import visible_length
from review_details import ReviewDetails
from scheduler import send_reminder

with patch.object(Database, "_init_db"):
    import bot


class QueueReferenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db = Database(str(Path(temp_dir.name) / "workqueue.db"))
        db_patch = patch.object(bot, "db", self.db)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        self.fetch_details = AsyncMock(return_value=None)
        fetch_patch = patch.object(bot, "fetch_review_details", self.fetch_details)
        fetch_patch.start()
        self.addCleanup(fetch_patch.stop)

    def add_task(self, chat_id=1, task_id="repo/120", url=None, assignees=None, created_by="@author"):
        repo, number = task_id.split("/")
        if url is None:
            url = f"https://github.com/owner/{repo}/pull/{number}"
        if assignees is None:
            assignees = ["@reviewer"]
        return self.db.add_task(chat_id, task_id, url, assignees, created_by)

    async def send_command(self, text, chat_id=1):
        reply = AsyncMock()
        update = SimpleNamespace(
            message=SimpleNamespace(text=text, reply_text=reply),
            effective_chat=SimpleNamespace(id=chat_id),
            effective_user=SimpleNamespace(username="author", first_name="Author"),
        )
        await bot.handle_message(update, None)
        reply.assert_awaited_once()
        return reply.await_args

    async def test_three_commands_accept_qualified_url_and_unique_number(self):
        references = (
            "repo/120",
            "120",
            "http://GITHUB.COM/owner/repo/pull/120///?view=new#comment",
        )
        saved_url = "https://github.com/owner/repo/pull/120?view=old&tab=files"
        link = '<a href="https://github.com/owner/repo/pull/120?view=old&amp;tab=files">repo/120</a>'
        for chat_id, (command, reference) in enumerate(
            ((command, reference) for command in ("wdone", "wbounce", "wassign") for reference in references),
            start=1,
        ):
            with self.subTest(command=command, reference=reference):
                self.add_task(chat_id, "other/1")
                self.add_task(chat_id, "repo/120", saved_url, created_by="<Author> & Team")
                suffix = " @alice @bob" if command == "wassign" else ""
                reply = await self.send_command(f"!{command} {reference}{suffix}", chat_id)

                if command == "wassign":
                    self.assertEqual(reply.args[0], f"{link} → @alice, @bob")
                    tasks = self.db.get_tasks(chat_id)
                    self.assertEqual([task.task_id for task in tasks], ["other/1", "repo/120"])
                    self.assertEqual(tasks[1].assignees, ["@alice", "@bob"])
                    self.assertEqual(tasks[1].created_by, "<Author> & Team")
                else:
                    expected = f"Removed {link} (added by &lt;Author&gt; &amp; Team)"
                    if command == "wbounce":
                        expected += "\nChanges required."
                    self.assertEqual(reply.args[0], expected)
                    self.assertEqual([task.task_id for task in self.db.get_tasks(chat_id)], ["other/1"])

                self.assertEqual(reply.kwargs["parse_mode"], "HTML")
                self.assertTrue(reply.kwargs["disable_web_page_preview"])

    async def test_bare_number_never_uses_queue_sequence(self):
        self.add_task(task_id="repo/120")

        missing = await self.send_command("!wdone 1")
        self.assertIn("not found in this chat", missing.args[0])
        self.assertEqual([task.task_id for task in self.db.get_tasks(1)], ["repo/120"])

        self.add_task(task_id="other/1")

        reply = await self.send_command("!wdone 1")

        self.assertIn(">other/1</a>", reply.args[0])
        self.assertEqual([task.task_id for task in self.db.get_tasks(1)], ["repo/120"])

    async def test_large_unknown_number_keeps_queue(self):
        self.add_task()

        reply = await self.send_command(f"!wdone {'9' * 5000}")

        self.assertIn("not found in this chat", reply.args[0])
        self.assertEqual([task.task_id for task in self.db.get_tasks(1)], ["repo/120"])

    async def test_ambiguous_number_lists_qualified_links_without_changes(self):
        self.add_task(task_id="monorepo/120")
        self.add_task(task_id="backend/120")
        before = self.db.get_tasks(1)

        for command in ("wdone", "wbounce", "wassign"):
            with self.subTest(command=command):
                suffix = " @alice" if command == "wassign" else ""
                reply = await self.send_command(f"!{command} 120{suffix}")
                self.assertIn("matches multiple reviews", reply.args[0])
                self.assertIn('>monorepo/120</a>', reply.args[0])
                self.assertIn('>backend/120</a>', reply.args[0])
                self.assertEqual(reply.kwargs["parse_mode"], "HTML")
                self.assertEqual(self.db.get_tasks(1), before)

    async def test_references_are_restricted_to_current_chat(self):
        self.add_task(chat_id=2)
        references = ("repo/120", "120", "https://github.com/owner/repo/pull/120")

        for command in ("wdone", "wbounce", "wassign"):
            for reference in references:
                with self.subTest(command=command, reference=reference):
                    suffix = " @alice" if command == "wassign" else ""
                    reply = await self.send_command(f"!{command} {reference}{suffix}", chat_id=1)
                    self.assertIn("not found in this chat", reply.args[0])
                    self.assertEqual(len(self.db.get_tasks(2)), 1)

    async def test_unknown_invalid_and_legacy_references_leave_queue_untouched(self):
        self.add_task()
        before = self.db.get_tasks(1)
        references = ("999", "repo/999", "https://github.com/owner/repo/pull/999", "bad<ref>&", "#1")

        for command in ("wdone", "wbounce", "wassign"):
            for reference in references:
                with self.subTest(command=command, reference=reference):
                    suffix = " @alice" if command == "wassign" else ""
                    reply = await self.send_command(f"!{command} {reference}{suffix}")
                    self.assertIn("PR/MR reference", reply.args[0])
                    self.assertIn("!w", reply.args[0])
                    if reference == "#1":
                        self.assertIn("Queue numbers", reply.args[0])
                    if reference == "bad<ref>&":
                        self.assertIn("bad&lt;ref&gt;&amp;", reply.args[0])
                    self.assertEqual(reply.kwargs["parse_mode"], "HTML")
                    self.assertEqual(self.db.get_tasks(1), before)

    async def test_qualified_reference_is_exact(self):
        self.add_task()
        for reference in ("Repo/120", "repo/120/", "repo/12"):
            with self.subTest(reference=reference):
                await self.send_command(f"!wdone {reference}")
                self.assertEqual(len(self.db.get_tasks(1)), 1)

        reply = await self.send_command("!wdone repo/120")
        self.assertIn(">repo/120</a>", reply.args[0])
        self.assertEqual(self.db.get_tasks(1), [])

    async def test_url_identity_includes_host_and_full_project_path(self):
        saved_url = "https://gitlab.example.com/teams/one/repo/-/merge_requests/120/?view=old"
        self.add_task(url=saved_url)
        wrong_urls = (
            "https://gitlab.example.com/teams/two/repo/-/merge_requests/120",
            "https://other.example.com/teams/one/repo/-/merge_requests/120",
            "https://gitlab.example.com/teams/one/repo/-/merge_requests/120/notes",
        )
        for reference in wrong_urls:
            with self.subTest(reference=reference):
                reply = await self.send_command(f"!wbounce {reference}")
                self.assertEqual(len(self.db.get_tasks(1)), 1)
                self.assertNotIn("Removed", reply.args[0])

        reply = await self.send_command(
            "!wbounce http://GITLAB.EXAMPLE.COM/teams/one/repo/-/merge_requests/120///?view=new#discussion"
        )
        self.assertIn(">repo/120</a>", reply.args[0])
        self.assertIn("Changes required.", reply.args[0])
        self.assertEqual(self.db.get_tasks(1), [])

    async def test_add_list_and_reminder_share_clickable_escaped_labels(self):
        url = "https://github.com/owner/repo/pull/120?view=1&tab=files"
        link = '<a href="https://github.com/owner/repo/pull/120?view=1&amp;tab=files">repo/120</a>'

        added = await self.send_command(f"!wadd {url} @alice @bob")
        self.assertEqual(added.args[0], f"{link} → @alice, @bob")
        self.add_task(
            task_id="other/1",
            assignees=["@<reviewer>"],
            created_by="<Alice> & Bob",
        )

        listed = await self.send_command("!w")
        self.assertIn(f"{link} → @alice, @bob (by @author)", listed.args[0])
        self.assertIn("@&lt;reviewer&gt; (by &lt;Alice&gt; &amp; Bob)", listed.args[0])
        self.assertNotIn("[#", listed.args[0])

        send_message = AsyncMock()
        application = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))
        await send_reminder(1, application, self.db)
        send_message.assert_awaited_once()
        reminder = send_message.await_args.kwargs
        self.assertEqual(reminder["text"], "<b>📋 Reminder: Pending Reviews</b>\n\n" + listed.args[0])
        self.assertEqual(reminder["parse_mode"], "HTML")
        self.assertTrue(reminder["disable_web_page_preview"])

    async def test_missing_references_show_updated_usage(self):
        for command in ("wdone", "wbounce", "wassign"):
            with self.subTest(command=command):
                reply = await self.send_command(f"!{command}")
                self.assertIn("PR/MR reference", reply.args[0])
                self.assertNotIn("#1", reply.args[0])

        help_reply = await self.send_command("!whelp")
        self.assertIn("PR/MR reference", help_reply.args[0])
        self.assertNotIn("#1", help_reply.args[0])

        legacy_reply = await self.send_command("!wassign #1")
        self.assertIn("Queue numbers", legacy_reply.args[0])
        self.assertIn("!w", legacy_reply.args[0])

    def test_url_parsing_rejects_non_review_paths_and_malformed_hosts(self):
        self.assertEqual(
            bot.extract_task_id("https://gitlab.example.com/team/project/-/merge_requests/123/?foo=bar#note"),
            "project/123",
        )
        self.assertIsNone(bot.extract_task_id("https://github.com/owner/repo/pull/123/files"))
        self.assertIsNone(bot.extract_task_id("ftp://github.com/owner/repo/pull/123"))
        self.assertIsNone(bot.extract_task_id("https://[broken/owner/repo/pull/123"))


class ReviewDetailsCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db = Database(str(Path(temp_dir.name) / "workqueue.db"))
        db_patch = patch.object(bot, "db", self.db)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        self.details = {}
        self.fetch_details = AsyncMock(side_effect=lambda client, url, use_tokens=False: self.details.get(url))
        fetch_patch = patch.object(bot, "fetch_review_details", self.fetch_details)
        fetch_patch.start()
        self.addCleanup(fetch_patch.stop)

    def add_task(self, number, assignees, chat_id=1, title=None, description=None):
        url = f"https://github.com/owner/repo/pull/{number}"
        self.db.add_task(chat_id, f"repo/{number}", url, assignees, "@author", title=title, description=description)
        return url

    def make_update(self, text, chat_type="supergroup", username="Alice", user_id=7, anonymous=False, topic=None,
                    chat_id=1):
        return SimpleNamespace(
            message=SimpleNamespace(
                text=text, reply_text=AsyncMock(),
                sender_chat=object() if anonymous else None,
                is_topic_message=topic is not None, message_thread_id=topic,
            ),
            effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
            effective_user=SimpleNamespace(id=user_id, username=username, first_name="Alice", is_bot=False),
        )

    def make_bot(self, send_message=None, bot_status="administrator", member_statuses=None):
        def chat_member(chat_id, user_id):
            if user_id == 99:
                return SimpleNamespace(status=bot_status)
            status = (member_statuses or {}).get(chat_id, "member")
            if isinstance(status, Exception):
                raise status
            return SimpleNamespace(status=status, is_member=False)

        return SimpleNamespace(
            id=99,
            # Telegram returns message ID 0 for ephemeral messages.
            send_message=send_message or AsyncMock(return_value=SimpleNamespace(message_id=0)),
            delete_message=AsyncMock(),
            get_chat_member=AsyncMock(side_effect=chat_member),
        )

    async def send(self, update, telegram_bot=None):
        context = SimpleNamespace(bot=telegram_bot or self.make_bot())
        await bot.handle_message(update, context)
        return context.bot

    async def test_wadd_saves_title_and_list_backfills_older_entries(self):
        new_url = "https://github.com/owner/repo/pull/2"
        old_url = self.add_task(1, ["@bob"])
        self.details[new_url] = ReviewDetails("Add <cache> & docs", "Body")
        update = self.make_update(f"!wadd {new_url} @bob")
        await self.send(update)

        update.message.reply_text.assert_awaited_once()
        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            f'<a href="{new_url}">repo/2</a> — Add &lt;cache&gt; &amp; docs → @bob',
        )
        self.assertEqual(self.db.get_tasks(1)[1].description, "Body")

        self.details[old_url] = ReviewDetails("Old title", "")
        self.fetch_details.reset_mock()
        listing = self.make_update("!w")
        await self.send(listing)

        self.assertEqual([call.args[1] for call in self.fetch_details.await_args_list], [old_url])
        text = listing.message.reply_text.await_args.args[0]
        self.assertIn(">repo/1</a> — Old title → @bob (by @author)", text)
        self.assertIn(">repo/2</a> — Add &lt;cache&gt; &amp; docs → @bob (by @Alice)", text)
        self.assertEqual(self.db.get_tasks(1)[0].title, "Old title")

    async def test_lookups_use_tokens_only_in_allowed_chats(self):
        self.add_task(1, ["@alice"], chat_id=1)
        self.add_task(2, ["@alice"], chat_id=-100)
        with patch.dict("os.environ", {"ALLOWED_CHAT_IDS": "-100"}):
            await self.send(self.make_update("!wadd https://github.com/owner/repo/pull/3", chat_id=1))
            await self.send(self.make_update("!wadd https://github.com/owner/repo/pull/4", chat_id=-100))
            await self.send(self.make_update("!wme", chat_type="private", chat_id=7))

        calls = {call.args[1].rsplit("/", 1)[-1]: call.kwargs["use_tokens"] for call in self.fetch_details.await_args_list}
        self.assertEqual(calls, {"3": False, "4": True, "1": False, "2": True})

    async def test_refresh_keeps_saved_description_when_pr_body_is_empty(self):
        url = self.add_task(1, ["@alice"], title="Old", description="Agent notes")
        self.details[url] = ReviewDetails("New title", "")

        await self.send(self.make_update("!wme"))

        task, = self.db.get_tasks(1)
        self.assertEqual((task.title, task.description), ("New title", "Agent notes"))

    async def test_refresh_limits_concurrent_lookups(self):
        for number in range(1, 13):
            self.add_task(number, ["@alice"])
        running = peak = 0

        async def slow_fetch(client, url, use_tokens=False):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1

        self.fetch_details.side_effect = slow_fetch
        await self.send(self.make_update("!wme"))

        self.assertEqual(self.fetch_details.await_count, 12)
        self.assertEqual(peak, bot.LOOKUP_CONCURRENCY)

    async def test_updates_run_in_order_per_chat_and_concurrently_across_chats(self):
        processor = bot.PerChatUpdateProcessor()
        events = []

        async def handler(name, delay):
            events.append(f"start {name}")
            await asyncio.sleep(delay)
            events.append(f"end {name}")

        def update(chat_id):
            return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id))

        await asyncio.gather(
            processor.do_process_update(update(1), handler("wadd", 0.05)),
            processor.do_process_update(update(1), handler("wassign", 0)),
            processor.do_process_update(update(2), handler("other chat", 0)),
            processor.do_process_update(SimpleNamespace(effective_chat=None), handler("no chat", 0)),
        )

        self.assertLess(events.index("end wadd"), events.index("start wassign"))
        self.assertLess(events.index("end other chat"), events.index("end wadd"))
        self.assertLess(events.index("end no chat"), events.index("end wadd"))
        self.assertEqual(processor._locks, {})

    async def test_duplicate_wadd_is_rejected_without_fetching(self):
        url = self.add_task(1, ["@bob"])
        update = self.make_update(f"!wadd {url}")

        await self.send(update)

        self.fetch_details.assert_not_awaited()
        self.assertIn("already exists", update.message.reply_text.await_args.args[0])

    async def test_long_lists_and_reminders_are_split_under_telegrams_limit(self):
        for number in range(1, 61):
            self.add_task(number, ["@alice", "@bob"], title=f"{number} " + "Refactor the order pipeline " * 5)
        update = self.make_update("!w")

        await self.send(update)

        replies = [call.args[0] for call in update.message.reply_text.await_args_list]
        self.assertGreater(len(replies), 1)
        self.assertTrue(all(visible_length(reply) <= 4096 for reply in replies))
        self.assertEqual(sum(reply.count("(by @author)") for reply in replies), 60)

        send_message = AsyncMock()
        await send_reminder(1, SimpleNamespace(bot=SimpleNamespace(send_message=send_message)), self.db)
        reminders = [call.kwargs["text"] for call in send_message.await_args_list]
        self.assertGreater(len(reminders), 1)
        self.assertTrue(reminders[0].startswith("<b>📋 Reminder: Pending Reviews</b>\n"))
        self.assertTrue(all(visible_length(reminder) <= 4096 for reminder in reminders))
        self.assertEqual(sum(reminder.count("(by @author)") for reminder in reminders), 60)

    async def test_wme_sends_only_the_senders_reviews_as_ephemeral_message(self):
        mine = self.add_task(1, ["@bob", "@alice"], title="Stale title")
        self.add_task(2, ["@bob"])
        self.add_task(3, ["@ALICE"], description="Saved <b>body</b>")
        self.add_task(4, ["@alice"], chat_id=2)
        self.details[mine] = ReviewDetails("Fresh title", "Line one\nLine two")
        update = self.make_update("!WME", topic=55)

        telegram_bot = await self.send(update)

        telegram_bot.send_message.assert_awaited_once()
        args, kwargs = telegram_bot.send_message.await_args
        self.assertEqual(args[0], 1)
        self.assertEqual(kwargs["api_kwargs"], {"ephemeral_message_parameters": {"receiver_user_id": 7}})
        self.assertEqual(kwargs["message_thread_id"], 55)
        self.assertEqual(kwargs["parse_mode"], "HTML")
        self.assertTrue(kwargs["disable_web_page_preview"])
        self.assertEqual(args[1], (
            "<b>Assigned to you (2)</b>\n\n"
            f'<a href="{mine}">repo/1</a> — Fresh title → @alice, @bob (by @author)\n'
            "<blockquote expandable>Line one\nLine two</blockquote>\n\n"
            '<a href="https://github.com/owner/repo/pull/3">repo/3</a> → @ALICE (by @author)\n'
            "<blockquote expandable>Saved &lt;b&gt;body&lt;/b&gt;</blockquote>"
        ))
        update.message.reply_text.assert_not_awaited()
        telegram_bot.delete_message.assert_not_awaited()
        self.assertEqual(self.db.get_tasks(1)[0].title, "Fresh title")

    async def test_wme_splits_long_lists_and_keeps_every_description(self):
        for number in range(1, 31):
            self.add_task(number, ["@alice"], title="T" * 120, description="D" * 300)
        update = self.make_update("!wme")

        telegram_bot = await self.send(update)

        messages = [call.args[1] for call in telegram_bot.send_message.await_args_list]
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(visible_length(message) <= 4096 for message in messages))
        self.assertEqual(sum(message.count("<blockquote expandable>") for message in messages), 30)
        self.assertTrue(messages[0].startswith("<b>Assigned to you (30)</b>"))

    async def test_wme_without_assignments_or_username_still_replies_privately(self):
        self.add_task(1, ["@bob"])
        for username, expected in (("alice", "No reviews are assigned to you"), (None, "Set a Telegram username")):
            with self.subTest(username=username):
                update = self.make_update("!wme", username=username)
                telegram_bot = await self.send(update)
                self.assertIn(expected, telegram_bot.send_message.await_args.args[1])
                self.assertIn("ephemeral_message_parameters", telegram_bot.send_message.await_args.kwargs["api_kwargs"])
                update.message.reply_text.assert_not_awaited()

    async def test_wme_in_private_chat_lists_assignments_from_groups_the_sender_is_in(self):
        self.add_task(1, ["@alice"], chat_id=-100)
        self.add_task(2, ["@bob"], chat_id=-100)
        self.add_task(3, ["@Alice"], chat_id=-200)
        self.add_task(4, ["@alice"], chat_id=-300, title="Left group")
        self.add_task(5, ["@alice"], chat_id=-400, title="Unknown group")
        self.add_task(6, ["@alice"], chat_id=-500)
        update = self.make_update("!wme", chat_type="private", chat_id=7)
        statuses = {-200: "administrator", -300: "left", -400: BadRequest("Chat not found"), -500: "restricted"}

        telegram_bot = await self.send(update, self.make_bot(member_statuses=statuses))

        telegram_bot.send_message.assert_not_awaited()
        self.assertEqual(sorted(call.args[0] for call in telegram_bot.get_chat_member.await_args_list),
                         [-500, -400, -300, -200, -100])
        reply = update.message.reply_text.await_args.args[0]
        self.assertIn("Assigned to you (2)", reply)
        self.assertIn(">repo/1</a>", reply)
        self.assertIn(">repo/3</a>", reply)
        for hidden in (">repo/2</a>", "Left group", "Unknown group", ">repo/6</a>"):
            self.assertNotIn(hidden, reply)

    async def test_wme_deletes_reply_that_telegram_posted_publicly(self):
        self.add_task(1, ["@alice"], title="Secret plan")
        telegram_bot = self.make_bot(AsyncMock(return_value=SimpleNamespace(message_id=321)))
        update = self.make_update("!wme")

        await self.send(update, telegram_bot)

        telegram_bot.delete_message.assert_awaited_once_with(1, 321)
        reply = update.message.reply_text.await_args.args[0]
        self.assertIn("doesn't support private replies", reply)
        self.assertNotIn("Secret plan", reply)

    async def test_wme_failure_messages_match_the_cause(self):
        self.add_task(1, ["@alice"], title="Secret plan")
        cases = (
            (BadRequest("Not enough rights"), "member", "Make me a group admin"),
            (Forbidden("Forbidden"), "left", "Make me a group admin"),
            (BadRequest("Can't parse entities"), "administrator", "try again later"),
            (TimedOut(), "administrator", "didn't confirm"),
            (RetryAfter(5), "administrator", "try again later"),
        )
        for error, status, expected in cases:
            with self.subTest(error=error, status=status):
                update = self.make_update("!wme")
                await self.send(update, self.make_bot(AsyncMock(side_effect=error), bot_status=status))
                reply = update.message.reply_text.await_args.args[0]
                self.assertIn(expected, reply)
                self.assertNotIn("Secret plan", reply)

    async def test_private_commands_reject_anonymous_admins(self):
        for command in ("!wme", "!wdetails"):
            with self.subTest(command=command):
                anonymous = self.make_update(command, anonymous=True)
                telegram_bot = await self.send(anonymous)
                telegram_bot.send_message.assert_not_awaited()
                self.assertIn("anonymously", anonymous.message.reply_text.await_args.args[0])

    async def test_wdetails_privately_lists_the_whole_queue_with_descriptions(self):
        first = self.add_task(1, ["@bob"], title="Saved title", description="First <body>")
        second = self.add_task(2, [])
        self.add_task(3, ["@alice"], chat_id=2, title="Other chat")
        self.details[first] = ReviewDetails("Should not refetch", "")
        self.details[second] = ReviewDetails("Fetched title", "Second body")
        update = self.make_update("!wdetails")

        telegram_bot = await self.send(update)

        self.assertEqual([call.args[1] for call in self.fetch_details.await_args_list], [second])
        telegram_bot.send_message.assert_awaited_once()
        args, kwargs = telegram_bot.send_message.await_args
        self.assertEqual(kwargs["api_kwargs"], {"ephemeral_message_parameters": {"receiver_user_id": 7}})
        self.assertEqual(args[1], (
            "<b>Review queue (2)</b>\n\n"
            f'<a href="{first}">repo/1</a> — Saved title → @bob (by @author)\n'
            "<blockquote expandable>First &lt;body&gt;</blockquote>\n\n"
            f'<a href="{second}">repo/2</a> — Fetched title (by @author)\n'
            "<blockquote expandable>Second body</blockquote>"
        ))
        update.message.reply_text.assert_not_awaited()

    async def test_wdetails_empty_queue_and_private_chat(self):
        update = self.make_update("!wdetails")
        telegram_bot = await self.send(update)
        self.assertEqual(telegram_bot.send_message.await_args.args[1], "No tasks in the queue.")

        self.add_task(1, ["@alice"], chat_id=-100, title="Group secret")
        private = self.make_update("!wdetails", chat_type="private", chat_id=7)
        telegram_bot = await self.send(private)
        telegram_bot.send_message.assert_not_awaited()
        reply = private.message.reply_text.await_args.args[0]
        self.assertIn("in a group", reply)
        self.assertNotIn("Group secret", reply)

    def test_visible_length_ignores_markup_and_counts_like_telegram(self):
        self.assertEqual(visible_length('<a href="https://example.com/very/long">repo/1</a> &lt;b&gt;'), 10)
        self.assertEqual(visible_length("👍"), 2)

    def test_migration_tolerates_a_concurrent_process_adding_columns(self):
        class StaleSchemaConnection(sqlite3.Connection):
            """Reports the pre-migration schema, as if another process added the columns since."""
            def execute(self, sql, *args):
                if sql == "PRAGMA table_info(tasks)":
                    return iter([{"name": "created_by_id"}])
                return super().execute(sql, *args)

        def stale_connection(database):
            conn = sqlite3.connect(database.db_path, factory=StaleSchemaConnection)
            conn.row_factory = sqlite3.Row
            return conn

        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "shared.db")
            Database(path)
            with patch.object(Database, "_get_connection", stale_connection):
                Database(path)
            self.assertEqual(Database(path).get_tasks(1), [])

    def test_existing_databases_gain_detail_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "old.db")
            with sqlite3.connect(path) as conn:
                conn.execute("""
                    CREATE TABLE tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, seq_num INTEGER NOT NULL,
                        task_id TEXT NOT NULL, url TEXT NOT NULL, assigned_to TEXT NOT NULL,
                        created_by TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("""
                    INSERT INTO tasks (chat_id, seq_num, task_id, url, assigned_to, created_by)
                    VALUES (1, 1, 'repo/1', 'https://github.com/o/repo/pull/1', 'unassigned', '@author')
                """)
            db = Database(path)
            task, = db.get_tasks(1)
            self.assertIsNone(task.title)
            db.update_task_details(task.id, "Title", "Body")
            self.assertEqual((db.get_tasks(1)[0].title, db.get_tasks(1)[0].description), ("Title", "Body"))


if __name__ == "__main__":
    unittest.main()
