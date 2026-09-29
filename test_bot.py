import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from database import Database

# Import the handlers without initializing the bot's default database.
with patch.object(Database, "_init_db"):
    import bot


class QueueRemovalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db = Database(str(Path(temp_dir.name) / "workqueue.db"))
        db_patch = patch.object(bot, "db", self.db)
        db_patch.start()
        self.addCleanup(db_patch.stop)

    def add_task(self, chat_id=1, task_id="repo/123", created_by="@author"):
        return self.db.add_task(
            chat_id, task_id, f"https://github.com/owner/repo/pull/{task_id.split('/')[-1]}",
            ["@reviewer"], created_by
        )

    async def send_command(self, text, chat_id=1):
        reply = AsyncMock()
        update = SimpleNamespace(
            message=SimpleNamespace(text=text, reply_text=reply),
            effective_chat=SimpleNamespace(id=chat_id),
            effective_user=None,
        )
        await bot.handle_message(update, None)
        reply.assert_awaited_once()
        return reply.await_args

    async def test_bounce_removes_only_the_selected_task(self):
        commands = ("!wbounce 1", "!wbounce #1", "!wbounce repo/123", "  !WBOUNCE #1  ")
        self.add_task(chat_id=99)
        for chat_id, command in enumerate(commands, start=1):
            with self.subTest(command=command):
                self.add_task(chat_id)
                self.add_task(chat_id, task_id="repo/456")

                reply = await self.send_command(command, chat_id)

                self.assertEqual([task.task_id for task in self.db.get_tasks(chat_id)], ["repo/456"])
                self.assertEqual(
                    reply.args[0],
                    'Removed [#1] <a href="https://github.com/owner/repo/pull/123">repo/123</a> '
                    '(added by @author)\nChanges required.'
                )
                self.assertEqual(reply.kwargs["parse_mode"], "HTML")
                self.assertTrue(reply.kwargs["disable_web_page_preview"])

        self.assertEqual([task.task_id for task in self.db.get_tasks(99)], ["repo/123"])

    async def test_bounce_without_reference_shows_usage_and_keeps_queue(self):
        self.add_task()
        for command in ("!wbounce", "!wbounce   "):
            with self.subTest(command=command):
                reply = await self.send_command(command)
                self.assertIn("Usage: <code>!wbounce &lt;N or task_id&gt;</code>", reply.args[0])
                self.assertEqual(len(self.db.get_tasks(1)), 1)

    async def test_bounce_missing_task_keeps_queue(self):
        self.add_task()
        for reference in ("2", "#2", "repo/456"):
            with self.subTest(reference=reference):
                reply = await self.send_command(f"!wbounce {reference}")
                self.assertEqual(reply.args[0], f"Task {reference} not found.")
                self.assertEqual(len(self.db.get_tasks(1)), 1)

    async def test_bounce_cannot_remove_a_task_from_another_chat(self):
        self.add_task(chat_id=2)
        for reference in ("1", "#1", "repo/123"):
            with self.subTest(reference=reference):
                reply = await self.send_command(f"!wbounce {reference}")
                self.assertEqual(reply.args[0], f"Task {reference} not found.")
                self.assertEqual(len(self.db.get_tasks(2)), 1)

    async def test_bounce_escapes_author_name(self):
        self.add_task(created_by="<Alice> & Bob")
        reply = await self.send_command("!wbounce 1")
        self.assertIn("(added by &lt;Alice&gt; &amp; Bob)", reply.args[0])

    async def test_done_still_removes_without_changes_required_comment(self):
        for chat_id, reference in enumerate(("1", "#1", "repo/123"), start=1):
            with self.subTest(reference=reference):
                self.add_task(chat_id)
                reply = await self.send_command(f"!wdone {reference}", chat_id)
                self.assertEqual(self.db.get_tasks(chat_id), [])
                self.assertEqual(
                    reply.args[0],
                    'Removed [#1] <a href="https://github.com/owner/repo/pull/123">repo/123</a> '
                    '(added by @author)'
                )


if __name__ == "__main__":
    unittest.main()
