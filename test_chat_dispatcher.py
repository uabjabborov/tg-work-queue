import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from chat_dispatcher import ChatDispatcher
from database import Database

with patch.object(Database, "_init_db"):
    import bot


class ChatDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_bot_commands_enter_dispatcher(self):
        dispatcher = SimpleNamespace(run=AsyncMock())
        context = SimpleNamespace(bot_data={"chat_dispatcher": dispatcher})
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=1),
                                 message=SimpleNamespace(text="normal conversation", reply_text=AsyncMock()))
        await bot.dispatch_message(update, context)
        dispatcher.run.assert_not_awaited()
        update.message.text = "  !WD  "
        await bot.dispatch_message(update, context)
        dispatcher.run.assert_awaited_once()
        with patch.object(bot, "handle_message", new_callable=AsyncMock) as handler:
            await dispatcher.run.await_args.args[1]()
            handler.assert_awaited_once_with(update, context)

    async def test_chat_order_does_not_block_other_chats(self):
        dispatcher = ChatDispatcher(concurrency=2)
        entered, release, other_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        async def first():
            calls.append("first")
            entered.set()
            await release.wait()
            calls.append("finished")

        async def second():
            calls.append("second")

        async def other():
            calls.append("other")
            other_done.set()

        busy = AsyncMock()
        first_task = asyncio.create_task(dispatcher.run(1, first, busy))
        await entered.wait()
        second_task = asyncio.create_task(dispatcher.run(1, second, busy))
        other_task = asyncio.create_task(dispatcher.run(2, other, busy))
        try:
            await asyncio.wait_for(other_done.wait(), 1)
            self.assertEqual(calls, ["first", "other"])
        finally:
            release.set()
            await asyncio.gather(first_task, second_task, other_task)
        self.assertEqual(calls, ["first", "other", "finished", "second"])
        busy.assert_not_awaited()
        self.assertEqual(dispatcher._chats, {})

    async def test_overflow_is_bounded_and_notice_is_coalesced(self):
        dispatcher = ChatDispatcher(pending_per_chat=1)
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow():
            entered.set()
            await release.wait()

        busy, extra = AsyncMock(), AsyncMock()
        task = asyncio.create_task(dispatcher.run(1, slow, busy))
        await entered.wait()
        try:
            for _ in range(10):
                await dispatcher.run(1, extra, busy)
            busy.assert_awaited_once()
            extra.assert_not_awaited()
        finally:
            release.set()
            await task
        await dispatcher.run(1, extra, busy)
        extra.assert_awaited_once()

    async def test_cancellation_and_failure_release_chat_and_global_slot(self):
        dispatcher = ChatDispatcher(concurrency=1)
        entered = asyncio.Event()

        async def slow():
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(dispatcher.run(1, slow, AsyncMock()))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(ValueError):
            await dispatcher.run(1, AsyncMock(side_effect=ValueError), AsyncMock())
        callback = AsyncMock()
        await asyncio.wait_for(dispatcher.run(2, callback, AsyncMock()), 1)
        callback.assert_awaited_once()
        self.assertEqual(dispatcher._chats, {})
