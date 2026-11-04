import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from telegram import Message
from telegram.error import BadRequest, Forbidden, TimedOut

from database import Database
from presentation import visible_length
from review_metadata import ReviewMetadataClient

with patch.object(Database, "_init_db"):
    import bot


class PrivateQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Database(str(Path(directory.name) / "test.db"))
        db_patch = patch.object(bot, "db", self.db)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        self.client = ReviewMetadataClient()
        self.client.fetch = AsyncMock(return_value=("Title <private>", "Description & details"))
        client_patch = patch("review_metadata.ReviewMetadataClient", return_value=self.client)
        client_patch.start()
        self.addCleanup(client_patch.stop)

    def add(self, number=12, chat_id=1, assignees=None):
        self.db.add_task(chat_id, f"repo/{number}", f"https://github.com/team/repo/pull/{number}",
                         assignees or [], "@author", 5)

    def update(self, text="!wd", chat_type="supergroup", anonymous=False):
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=1, type=chat_type),
            effective_user=SimpleNamespace(id=7, username=None, first_name="Alice", is_bot=False),
            message=SimpleNamespace(text=text, message_id=100, reply_text=AsyncMock(),
                                    is_topic_message=True, message_thread_id=55,
                                    sender_chat=object() if anonymous else None),
        )

    def telegram_bot(self):
        # Exercise the pinned Telegram library's decoding of the newer API fields.
        sent = Message.de_json({
            "message_id": 0, "date": 1, "chat": {"id": 1, "type": "supergroup"},
            "ephemeral_message_id": 321,
            "receiver_user": {"id": 7, "is_bot": False, "first_name": "Alice"},
        }, None)
        return SimpleNamespace(
            id=99, send_message=AsyncMock(return_value=sent), _post=AsyncMock(return_value=True),
            delete_message=AsyncMock(return_value=True),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="administrator")),
        )

    async def send(self, update, telegram_bot=None):
        telegram_bot = telegram_bot or self.telegram_bot()
        await bot.handle_message(update, SimpleNamespace(bot=telegram_bot))
        return telegram_bot

    async def test_full_current_chat_queue_is_private_and_command_deleted(self):
        self.add(12, assignees=["@bob"])
        self.add(13)
        self.add(99, chat_id=2, assignees=["@alice"])
        update = self.update(text="  !WD  ")
        telegram_bot = await self.send(update)

        update.message.reply_text.assert_not_awaited()
        args, kwargs = telegram_bot.send_message.await_args
        self.assertNotIn("repo/", args[1])
        self.assertNotIn("Description", args[1])
        self.assertEqual(kwargs["message_thread_id"], 55)
        self.assertEqual(kwargs["api_kwargs"], {"ephemeral_message_parameters": {"receiver_user_id": 7}})
        telegram_bot._post.assert_awaited_once()
        self.assertEqual(telegram_bot._post.await_args.args[0], "editEphemeralMessageText")
        data = telegram_bot._post.await_args.kwargs["data"]
        self.assertEqual((data["chat_id"], data["receiver_user_id"], data["ephemeral_message_id"]), (1, 7, 321))
        self.assertIn("repo/12", data["text"])
        self.assertIn("repo/13", data["text"])
        self.assertNotIn("repo/99", data["text"])
        self.assertIn("Title &lt;private&gt;", data["text"])
        self.assertIn("Description &amp; details", data["text"])
        self.assertIn("→ @bob (by @author)", data["text"])
        telegram_bot.delete_message.assert_awaited_once_with(1, 100)
        self.assertEqual(len(self.db.get_tasks(1)), 2)

    async def test_empty_queue_is_private_too(self):
        update = self.update()
        telegram_bot = await self.send(update)
        self.assertEqual(telegram_bot._post.await_args.kwargs["data"]["text"], "No tasks in the queue.")
        update.message.reply_text.assert_not_awaited()
        telegram_bot.delete_message.assert_awaited_once_with(1, 100)

    async def test_one_entry_with_many_assignees_splits_into_valid_private_messages(self):
        assignees = [f"@user{i:03d}" + "a" * 24 for i in range(100)]
        self.add(assignees=assignees)
        self.client.fetch.return_value = ("Fix login", "d" * 1200)
        update = self.update()
        telegram_bot = await self.send(update)
        texts = [call.kwargs["data"]["text"] for call in telegram_bot._post.await_args_list]
        self.assertGreater(len(texts), 1)
        self.assertTrue(all(visible_length(text) <= 4096 for text in texts))
        self.assertIn("d" * 1200, "".join(texts))
        for assignee in assignees:
            self.assertIn(assignee, "".join(texts))
        update.message.reply_text.assert_not_awaited()

    async def test_unconfirmed_private_edit_is_not_reported_as_delivered(self):
        update = self.update()
        telegram_bot = self.telegram_bot()
        telegram_bot._post.return_value = False
        delivered = await bot.reply_privately(update, SimpleNamespace(bot=telegram_bot), ["Sensitive"], verify_private=True)
        self.assertFalse(delivered)
        self.assertNotIn("Sensitive", update.message.reply_text.await_args.args[0])

    async def test_deletion_failure_preserves_private_response(self):
        self.add()
        update = self.update()
        telegram_bot = self.telegram_bot()
        telegram_bot.delete_message.side_effect = Forbidden("cannot delete messages")
        await self.send(update, telegram_bot)
        self.assertIn("Description", telegram_bot._post.await_args.kwargs["data"]["text"])
        update.message.reply_text.assert_not_awaited()

    async def test_send_and_edit_failures_never_publish_details_and_still_delete_command(self):
        self.add()
        for method in ("send_message", "_post"):
            for error in (Forbidden("not permitted"), BadRequest("unsupported"), TimedOut()):
                with self.subTest(method=method, error=error):
                    update = self.update()
                    telegram_bot = self.telegram_bot()
                    getattr(telegram_bot, method).side_effect = error
                    await self.send(update, telegram_bot)
                    telegram_bot.delete_message.assert_awaited_once_with(1, 100)
                    for sent in update.message.reply_text.await_args_list + telegram_bot.send_message.await_args_list:
                        self.assertNotIn("repo/12", str(sent))
                        self.assertNotIn("Description", str(sent))
                    if method == "send_message":
                        telegram_bot._post.assert_not_awaited()

    async def test_unsupported_server_gets_only_placeholder_then_command_is_deleted(self):
        self.add()
        update = self.update()
        telegram_bot = self.telegram_bot()
        telegram_bot.send_message.return_value = SimpleNamespace(message_id=987)
        await self.send(update, telegram_bot)
        telegram_bot._post.assert_not_awaited()
        self.assertEqual(telegram_bot.delete_message.await_args_list, [call(1, 987), call(1, 100)])
        self.assertNotIn("Description", telegram_bot.send_message.await_args.args[1])
        self.assertNotIn("repo/12", update.message.reply_text.await_args.args[0])

    async def test_missing_or_wrong_private_identifiers_prevent_sending_details(self):
        self.add()
        for fields in ({}, {"ephemeral_message_id": 1, "receiver_user": {"id": 8}},
                       {"ephemeral_message_id": "bad", "receiver_user": {"id": 7}}):
            with self.subTest(fields=fields):
                update = self.update()
                telegram_bot = self.telegram_bot()
                telegram_bot.send_message.return_value = SimpleNamespace(message_id=0, api_kwargs=fields)
                await self.send(update, telegram_bot)
                telegram_bot._post.assert_not_awaited()
                self.assertIn("haven't sent the details", update.message.reply_text.await_args.args[0])
                telegram_bot.delete_message.assert_awaited_once_with(1, 100)

    async def test_anonymous_sender_gets_no_details_and_command_is_deleted(self):
        self.add()
        update = self.update(anonymous=True)
        telegram_bot = await self.send(update)
        telegram_bot.send_message.assert_not_awaited()
        telegram_bot._post.assert_not_awaited()
        self.client.fetch.assert_not_awaited()
        self.assertIn("anonymously", update.message.reply_text.await_args.args[0])
        telegram_bot.delete_message.assert_awaited_once_with(1, 100)

    async def test_direct_message_uses_its_own_queue_and_keeps_command(self):
        self.add()
        self.add(99, chat_id=2)
        update = self.update(chat_type="private")
        telegram_bot = await self.send(update)
        response = update.message.reply_text.await_args.args[0]
        self.assertIn("Description", response)
        self.assertNotIn("repo/99", response)
        telegram_bot.send_message.assert_not_awaited()
        telegram_bot._post.assert_not_awaited()
        telegram_bot.delete_message.assert_not_awaited()

    async def test_long_queue_splits_every_chunk_privately(self):
        for number in range(8):
            self.add(number)
        self.client.fetch.return_value = ("Title", "😀<&>" * 1000)
        update = self.update()
        telegram_bot = await self.send(update)
        self.assertGreater(telegram_bot._post.await_count, 1)
        self.assertEqual(telegram_bot._post.await_count, telegram_bot.send_message.await_count)
        for edited in telegram_bot._post.await_args_list:
            data = edited.kwargs["data"]
            self.assertLessEqual(visible_length(data["text"]), 4096)
            self.assertEqual(data["receiver_user_id"], 7)
        update.message.reply_text.assert_not_awaited()
        telegram_bot.delete_message.assert_awaited_once_with(1, 100)

    async def test_help_documents_wd(self):
        update = self.update("!whelp")
        await self.send(update)
        self.assertIn("<code>!wd</code>", update.message.reply_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
