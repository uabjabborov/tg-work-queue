import sqlite3
import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut

from database import Database
from leaderboard import TASHKENT
from presentation import visible_length
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



class WmeCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db = Database(str(Path(temp_dir.name) / "workqueue.db"))
        db_patch = patch.object(bot, "db", self.db)
        db_patch.start()
        self.addCleanup(db_patch.stop)

    def add_task(self, number, assignees, chat_id=1):
        url = f"https://github.com/owner/repo/pull/{number}"
        self.db.add_task(chat_id, f"repo/{number}", url, assignees, "@author")
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
            get_chat=AsyncMock(side_effect=lambda chat_id: SimpleNamespace(title=f"Team {chat_id}")),
        )

    async def send(self, update, telegram_bot=None):
        context = SimpleNamespace(bot=telegram_bot or self.make_bot())
        await bot.handle_message(update, context)
        return context.bot

    async def test_wme_sends_only_the_senders_reviews_as_ephemeral_message(self):
        mine = self.add_task(1, ["@bob", "@alice"])
        self.add_task(2, ["@bob"])
        self.add_task(3, ["@ALICE"])
        self.add_task(4, ["@alice"], chat_id=2)
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
            "<b>Assigned to you (2)</b>\n"
            f'<a href="{mine}">repo/1</a> → @alice, @bob (by @author)\n'
            '<a href="https://github.com/owner/repo/pull/3">repo/3</a> → @ALICE (by @author)'
        ))
        update.message.reply_text.assert_not_awaited()
        telegram_bot.delete_message.assert_not_awaited()

    async def test_wme_splits_long_lists_under_telegrams_limit(self):
        for number in range(1, 301):
            self.add_task(number, ["@alice", "@" + "b" * 30])
        update = self.make_update("!wme")

        telegram_bot = await self.send(update)

        messages = [call.args[1] for call in telegram_bot.send_message.await_args_list]
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(visible_length(message) <= 4096 for message in messages))
        self.assertEqual(sum(message.count("(by @author)") for message in messages), 300)
        self.assertTrue(messages[0].startswith("<b>Assigned to you (300)</b>"))

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
        self.add_task(4, ["@alice"], chat_id=-300)
        self.add_task(5, ["@alice"], chat_id=-400)
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
        for hidden in (">repo/2</a>", ">repo/4</a>", ">repo/5</a>", ">repo/6</a>"):
            self.assertNotIn(hidden, reply)

    async def test_wme_deletes_reply_that_telegram_posted_publicly(self):
        self.add_task(1, ["@alice"])
        telegram_bot = self.make_bot(AsyncMock(return_value=SimpleNamespace(message_id=321)))
        update = self.make_update("!wme")

        await self.send(update, telegram_bot)

        telegram_bot.delete_message.assert_awaited_once_with(1, 321)
        reply = update.message.reply_text.await_args.args[0]
        self.assertIn("doesn't support private replies", reply)
        self.assertNotIn("repo/1", reply)

    async def test_wme_failure_messages_match_the_cause(self):
        self.add_task(1, ["@alice"])
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
                self.assertNotIn("repo/1", reply)

    async def test_wme_rejects_anonymous_admins(self):
        anonymous = self.make_update("!wme", anonymous=True)
        telegram_bot = await self.send(anonymous)
        telegram_bot.send_message.assert_not_awaited()
        self.assertIn("anonymously", anonymous.message.reply_text.await_args.args[0])

    async def command(self, text, username="alice", user_id=7, chat_id=1):
        update = self.make_update(text, username=username, user_id=user_id, chat_id=chat_id)
        telegram_bot = await self.send(update)
        return update, telegram_bot

    async def wme_reply(self, username="alice", user_id=7, chat_id=1):
        _, telegram_bot = await self.command("!wme", username, user_id, chat_id)
        return "\n".join(call.args[1] for call in telegram_bot.send_message.await_args_list)

    async def test_wme_shows_the_submitters_bounced_reviews(self):
        url = "https://github.com/owner/repo/pull/5"
        await self.command(f"!wadd {url} @bob")
        await self.command("!wadd https://github.com/owner/repo/pull/6 @bob")
        await self.command("!wdone repo/6", username="bob", user_id=8)
        await self.command("!wbounce repo/5", username="bob", user_id=8)

        reply = await self.wme_reply()

        self.assertIn("No reviews are assigned to you.", reply)
        self.assertIn("<b>Your PRs/MRs that need changes (1)</b>", reply)
        self.assertIn(f'<a href="{url}">repo/5</a> — bounced by @bob on ', reply)
        self.assertIn("<code>!wadd</code>", reply)
        self.assertNotIn("repo/6", reply)
        # The reviewer and other members don't see it, nor does the same user in another chat.
        self.assertNotIn("repo/5", await self.wme_reply(username="bob", user_id=8))
        self.assertNotIn("repo/5", await self.wme_reply(username="carol", user_id=9))
        self.assertNotIn("repo/5", await self.wme_reply(chat_id=2))

    async def test_bounced_review_clears_when_re_added_and_returns_when_bounced_again(self):
        url = "https://github.com/owner/repo/pull/5"
        await self.command(f"!wadd {url} @bob")
        await self.command("!wbounce 5", username="bob", user_id=8)
        self.assertIn("repo/5", await self.wme_reply())

        await self.command(f"!wadd {url}/ @bob")
        self.assertNotIn("need changes", await self.wme_reply())

        await self.command("!wbounce 5", username="bob", user_id=8)
        self.assertIn("need changes (1)", await self.wme_reply())

        await self.command(f"!wadd {url} @bob")
        await self.command("!wdone 5", username="bob", user_id=8)
        self.assertNotIn("need changes", await self.wme_reply())

    def bounce(self, number, bounced_at, created_by="@alice", created_by_id=7, chat_id=1):
        self.add_task(number, ["@bob"], chat_id=chat_id)
        task = self.db.get_tasks(chat_id)[-1]
        with sqlite3.connect(self.db.db_path) as conn:
            conn.execute("UPDATE tasks SET created_by = ?, created_by_id = ? WHERE id = ?",
                         (created_by, created_by_id, task.id))
        self.db.complete_task(chat_id, task.id, "bounce", "@bob", 8, completed_at=bounced_at)

    async def test_bounced_reviews_match_by_user_id_or_legacy_name(self):
        # 20:30 UTC is 01:30 the next day in Tashkent.
        bounced_at = (datetime.now(timezone.utc) - timedelta(days=3)).replace(hour=20, minute=30)
        for number, created_by, created_by_id in ((1, "@old_name", 7), (2, "@Alice", None), (3, "@alice", 9)):
            self.bounce(number, bounced_at, created_by, created_by_id)

        reply = await self.wme_reply()

        self.assertIn("need changes (2)", reply)
        tashkent_date = (bounced_at + timedelta(days=1)).strftime("%b %d")
        self.assertIn(f">repo/1</a> — bounced by @bob on {tashkent_date}", reply)
        self.assertIn(">repo/2</a>", reply)
        self.assertNotIn(">repo/3</a>", reply)

    async def test_wme_without_username_still_shows_bounced_reviews(self):
        await self.command("!wadd https://github.com/owner/repo/pull/5 @bob", username=None)
        await self.command("!wbounce 5", username="bob", user_id=8)

        reply = await self.wme_reply(username=None)

        self.assertIn("Set a Telegram username", reply)
        self.assertIn("need changes (1)", reply)

    async def test_wme_in_private_chat_hides_bounces_from_groups_the_sender_left(self):
        for chat_id in (-100, -300):
            await self.command("!wadd https://github.com/owner/repo/pull/5 @bob", chat_id=chat_id)
            await self.command("!wbounce 5", username="bob", user_id=8, chat_id=chat_id)
        update = self.make_update("!wme", chat_type="private", chat_id=7)

        telegram_bot = await self.send(update, self.make_bot(member_statuses={-300: "left"}))

        telegram_bot.get_chat_member.assert_has_awaits([call(-100, 7), call(-300, 7)], any_order=True)
        reply = update.message.reply_text.await_args.args[0]
        self.assertIn("need changes (1)", reply)

    async def test_bounces_older_than_the_visible_window_are_hidden(self):
        now = datetime.now(timezone.utc)
        self.bounce(1, now - timedelta(days=bot.BOUNCE_VISIBLE_DAYS, hours=1))
        self.bounce(2, now - timedelta(days=bot.BOUNCE_VISIBLE_DAYS - 1))

        reply = await self.wme_reply()

        self.assertIn("need changes (1)", reply)
        self.assertIn(">repo/2</a>", reply)
        self.assertNotIn(">repo/1</a>", reply)
        self.assertIn(f"older than {bot.BOUNCE_VISIBLE_DAYS} days are hidden", reply)

    async def test_wme_in_private_chat_names_each_group_and_includes_this_chat(self):
        now = datetime.now(timezone.utc)
        self.add_task(45, ["@alice"], chat_id=-100)
        self.add_task(45, ["@alice"], chat_id=-200)
        self.add_task(46, ["@alice"], chat_id=7)
        self.bounce(52, now, chat_id=-200)
        update = self.make_update("!wme", chat_type="private", chat_id=7)

        telegram_bot = await self.send(update)

        self.assertNotIn(7, [call.args[0] for call in telegram_bot.get_chat_member.await_args_list])
        reply = update.message.reply_text.await_args.args[0]
        self.assertEqual(reply, (
            "<b>Assigned to you (3)</b>\n"
            "<i>Team -200</i>\n"
            '<a href="https://github.com/owner/repo/pull/45">repo/45</a> → @alice (by @author)\n'
            "<i>Team -100</i>\n"
            '<a href="https://github.com/owner/repo/pull/45">repo/45</a> → @alice (by @author)\n'
            "<i>This chat</i>\n"
            '<a href="https://github.com/owner/repo/pull/46">repo/46</a> → @alice (by @author)\n'
            "\n<b>Your PRs/MRs that need changes (1)</b>\n"
            "<i>Team -200</i>\n"
            f'<a href="https://github.com/owner/repo/pull/52">repo/52</a> — bounced by @bob on '
            f'{now.astimezone(TASHKENT).strftime("%b %d")}\n'
            "<i>Re-add one with <code>!wadd</code> once it's fixed. Bounces older than 14 days are hidden.</i>"
        ))

    async def test_wme_in_private_chat_warns_when_groups_could_not_be_checked(self):
        for chat_id in (-100, -200, -300, -400):
            self.add_task(1, ["@alice"], chat_id=chat_id)
        statuses = {-200: TimedOut(), -300: RetryAfter(5), -400: Forbidden("bot was kicked")}
        update = self.make_update("!wme", chat_type="private", chat_id=7)

        await self.send(update, self.make_bot(member_statuses=statuses))

        reply = update.message.reply_text.await_args.args[0]
        self.assertIn("Assigned to you (1)", reply)
        self.assertIn("Team -100", reply)
        self.assertIn("couldn't check 2 of your groups", reply)

    async def test_wme_in_private_chat_limits_concurrent_membership_checks(self):
        for chat_id in range(-120, -100):
            self.add_task(1, ["@alice"], chat_id=chat_id)
        active = peak = 0

        async def chat_member(chat_id, user_id):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return SimpleNamespace(status="member")

        telegram_bot = self.make_bot()
        telegram_bot.get_chat_member = AsyncMock(side_effect=chat_member)
        update = self.make_update("!wme", chat_type="private", chat_id=7)

        await self.send(update, telegram_bot)

        self.assertEqual(telegram_bot.get_chat_member.await_count, 20)
        self.assertLessEqual(peak, bot.MEMBERSHIP_CHECK_CONCURRENCY)
        self.assertIn("Assigned to you (20)", update.message.reply_text.await_args.args[0])

    async def test_wme_reports_a_partially_delivered_reply(self):
        for number in range(1, 301):
            self.add_task(number, ["@alice", "@" + "b" * 30])
        send_message = AsyncMock(side_effect=[SimpleNamespace(message_id=0), TimedOut()])
        update = self.make_update("!wme")

        await self.send(update, self.make_bot(send_message))

        reply = update.message.reply_text.await_args.args[0]
        self.assertTrue(reply.startswith("Only part of my reply reached you."))

    async def test_wme_asks_to_delete_a_public_reply_it_could_not_delete(self):
        self.add_task(1, ["@alice"])
        telegram_bot = self.make_bot(AsyncMock(return_value=SimpleNamespace(message_id=321)))
        telegram_bot.delete_message = AsyncMock(side_effect=BadRequest("Message can't be deleted"))
        update = self.make_update("!wme")

        await self.send(update, telegram_bot)

        self.assertIn("couldn't delete it, so please delete it", update.message.reply_text.await_args.args[0])

    def test_bounce_lookup_has_an_identity_index(self):
        with sqlite3.connect(self.db.db_path) as conn:
            plan = " ".join(row[3] for row in conn.execute("""
                EXPLAIN QUERY PLAN SELECT 1 FROM review_activity
                WHERE chat_id = 1 AND review_host = 'h' AND review_project = 'p' AND review_number = '1'
                  AND completed_at > ''
            """))
        self.assertIn("review_activity_identity", plan)

    def test_visible_length_ignores_markup_and_counts_like_telegram(self):
        self.assertEqual(visible_length('<a href="https://example.com/very/long">repo/1</a> &lt;b&gt;'), 10)
        self.assertEqual(visible_length("👍"), 2)


if __name__ == "__main__":
    unittest.main()
