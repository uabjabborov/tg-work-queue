import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from database import Database
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


if __name__ == "__main__":
    unittest.main()
