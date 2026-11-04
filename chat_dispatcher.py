"""Run different chats concurrently while preserving command order within each chat."""

import asyncio
from dataclasses import dataclass, field


@dataclass
class _Chat:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: int = 0
    notified: bool = False


class ChatDispatcher:
    def __init__(self, concurrency: int = 8, pending_per_chat: int = 4):
        self._slots = asyncio.Semaphore(concurrency)
        self._pending_per_chat = pending_per_chat
        self._chats: dict[int, _Chat] = {}

    async def run(self, chat_id, callback, on_busy):
        chat = self._chats.setdefault(chat_id, _Chat())
        if chat.pending >= self._pending_per_chat:
            # Coalesce overload notices until this chat's accepted commands finish.
            if not chat.notified:
                chat.notified = True
                await on_busy()
            return
        chat.pending += 1
        try:
            async with chat.lock:
                # Waiters from one chat must not occupy all global slots.
                async with self._slots:
                    await callback()
        finally:
            chat.pending -= 1
            if not chat.pending:
                del self._chats[chat_id]
