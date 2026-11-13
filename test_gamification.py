import asyncio
import random
import re
import tempfile
import unittest
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anthropic
import httpx2
from telegram.error import BadRequest

from database import Database, utc_timestamp
from cheer_lines import (
    BODIES, CALL_TO_ACTION, CLOSERS, COMBOS, EMPTY_QUEUE, NOTHING_TO_REVIEW, OPENERS, SLOTS, TIER_OPENERS,
)
from gamification import (
    EMPTY_QUEUE_COOLDOWN, TIER_EMOJI, Cheer, CheerWriter, approval_cheer, bounce_cheer, empty_queue_quip, format_wait,
    header, queue_wait, speed_tier, template_quip,
)

with patch.object(Database, "_init_db"):
    import bot


NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
PLACEHOLDER = re.compile(r"\{\w+\}")


def cheer(tier="flash", outcome="done", count=1, wait=timedelta(minutes=7), bounce_round=0):
    return Cheer(outcome, tier, count, "repo/55", "https://github.com/owner/repo/pull/55", wait, bounce_round)


class TierTests(unittest.TestCase):
    def test_speed_tiers_follow_the_queue_wait(self):
        cases = [
            (timedelta(minutes=9, seconds=59), "flash"), (timedelta(minutes=10), "rocket"),
            (timedelta(minutes=59), "rocket"), (timedelta(hours=1), "steady"),
            (timedelta(hours=3, minutes=59), "steady"), (timedelta(hours=4), "turtle"),
            (timedelta(hours=23), "turtle"), (timedelta(days=1), "fossil"), (None, "steady"),
        ]
        for wait, tier in cases:
            with self.subTest(wait=wait):
                self.assertEqual(speed_tier(wait), tier)

    def test_waits_read_like_people_write_them(self):
        self.assertEqual(format_wait(timedelta(seconds=42)), "42 sec")
        self.assertEqual(format_wait(timedelta(minutes=7, seconds=30)), "7 min")
        self.assertEqual(format_wait(timedelta(hours=2)), "2 h")
        self.assertEqual(format_wait(timedelta(hours=2, minutes=5)), "2 h 5 min")
        self.assertEqual(format_wait(timedelta(days=3)), "3 d")
        self.assertEqual(format_wait(timedelta(days=3, hours=4, minutes=59)), "3 d 4 h")

    def test_queue_wait_reads_new_and_legacy_timestamps(self):
        self.assertEqual(queue_wait(utc_timestamp(NOW - timedelta(minutes=5)), NOW), timedelta(minutes=5))
        # Rows saved by SQLite's CURRENT_TIMESTAMP are naive UTC.
        self.assertEqual(queue_wait("2026-11-13 11:00:00", NOW), timedelta(hours=1))
        self.assertIsNone(queue_wait("not a time", NOW))
        self.assertIsNone(queue_wait(utc_timestamp(NOW + timedelta(minutes=1)), NOW))

    def test_batches_highlight_the_fastest_approval_and_the_most_bounced_pr(self):
        approval = approval_cheer([("a/1", "u1", timedelta(hours=5)), ("a/2", "u2", None),
                                   ("a/3", "u3", timedelta(minutes=3))])
        self.assertEqual((approval.task_id, approval.tier, approval.count), ("a/3", "flash", 3))
        self.assertEqual(approval_cheer([("a/1", "u1", None)]).tier, "steady")

        bounce = bounce_cheer([("a/1", "u1", 1), ("a/2", "u2", 4), ("a/3", "u3", 2)])
        self.assertEqual((bounce.task_id, bounce.tier, bounce.bounce_round, bounce.count), ("a/2", "marathon", 4, 3))
        self.assertEqual(bounce_cheer([("a/1", "u1", 1)]).tier, "guard")
        self.assertEqual(bounce_cheer([("a/1", "u1", 2)]).tier, "round2")
        self.assertIsNone(approval_cheer([]))
        self.assertIsNone(bounce_cheer([]))

    def test_header_states_the_facts_and_escapes_the_reference(self):
        self.assertEqual(header(cheer()), '⚡ <a href="https://github.com/owner/repo/pull/55">repo/55</a> · 7 min')
        self.assertEqual(
            header(Cheer("bounce", "marathon", 2, "<x>/1", "https://h/p?a=1&b=2", None, 3)),
            '🔁 ×2 · <a href="https://h/p?a=1&amp;b=2">&lt;x&gt;/1</a> · round 3',
        )
        self.assertNotIn("round", header(cheer("guard", "bounce", bounce_round=1, wait=None)))


class LineLibraryTests(unittest.TestCase):
    """Checks every line in cheer_lines.py, so a typo in a new line fails here and not in a chat."""

    def all_pools(self):
        for name, group in (("openers", OPENERS), ("bodies", BODIES), ("combos", COMBOS), ("closers", CLOSERS),
                            ("empty", {"queue": EMPTY_QUEUE}), ("nothing", {"to_review": NOTHING_TO_REVIEW}),
                            ("cta", {"add": CALL_TO_ACTION})):
            for key, pool in group.items():
                for language, lines in pool.items():
                    yield f"{name}/{key}/{language}", language, lines

    def test_every_line_uses_known_placeholders_and_slots_in_its_own_language(self):
        allowed = {"time", "count", "round"}
        for where, language, lines in self.all_pools():
            for line in lines:
                with self.subTest(where=where, line=line):
                    for placeholder in re.findall(r"\{(\w+)\}", line):
                        if placeholder not in allowed:
                            self.assertIn(placeholder, SLOTS)
                            self.assertTrue(placeholder.endswith(f"_{language}"))
                    self.assertLessEqual(len(line), 200)
                    self.assertEqual(line, line.strip())

    def test_russian_lines_never_put_a_number_before_a_noun(self):
        # Russian nouns change form with the number (2 возврата, 5 возвратов), so write ×{count} or №{round}.
        for where, language, lines in self.all_pools():
            if language == "ru":
                for line in lines:
                    with self.subTest(where=where, line=line):
                        self.assertNotRegex(line, r"\{(count|round)\} (?!ревью|подряд)\w")

    def test_each_language_has_its_own_slot_options(self):
        for name, options in SLOTS.items():
            with self.subTest(slot=name):
                self.assertRegex(name, r"_(en|uz|ru)$")
                self.assertGreaterEqual(len(options), 4)
                self.assertEqual(len(options), len(set(options)))

    def test_every_tier_has_plenty_of_lines_in_every_language(self):
        self.assertEqual(set(BODIES), set(TIER_OPENERS))
        self.assertEqual(set(BODIES), set(TIER_EMOJI))
        for tier, pool in BODIES.items():
            self.assertEqual(set(pool), {"en", "uz", "ru"})
            for language, lines in pool.items():
                with self.subTest(tier=tier, language=language):
                    self.assertGreaterEqual(len(lines), 10)
        for where, _, lines in self.all_pools():
            with self.subTest(where=where):
                self.assertEqual(len(lines), len(set(lines)))

    def test_public_lines_have_plenty_per_language_and_no_review_facts(self):
        for name, pool in (("empty", EMPTY_QUEUE), ("nothing", NOTHING_TO_REVIEW), ("cta", CALL_TO_ACTION)):
            self.assertEqual(set(pool), {"en", "uz", "ru"})
            for language, lines in pool.items():
                with self.subTest(pool=name, language=language):
                    self.assertGreaterEqual(len(lines), 10)
                    for line in lines:
                        # Public lines don't describe a particular review.
                        self.assertNotRegex(line, r"\{(time|count|round)\}")

    def test_openers_used_in_public_lines_have_no_review_facts(self):
        # Public lines are filled with slots only, so {time}, {count} or {round} there would break !w.
        for mood in ("hype", "tease"):
            for line in (line for lines in OPENERS[mood].values() for line in lines):
                with self.subTest(mood=mood, line=line):
                    self.assertNotRegex(line, r"\{(time|count|round)\}")

    def test_calls_to_action_lead_into_the_add_command(self):
        for line in (line for lines in CALL_TO_ACTION.values() for line in lines):
            with self.subTest(line=line):
                self.assertTrue(line.endswith(":"))

    def test_empty_queue_quips_fill_slots_and_rotate(self):
        rng, recent, quips, lines = random.Random(6), deque(maxlen=12), [], []
        for _ in range(30):
            quips.append(empty_queue_quip(rng, recent))
            lines.append(recent[-1])
        self.assertFalse(any(PLACEHOLDER.search(quip) for quip in quips))
        for start in range(len(lines) - 12):
            self.assertEqual(len(set(lines[start:start + 12])), 12)
        self.assertGreater(len(set(quips)), 25)

    def test_one_tier_already_yields_thousands_of_different_messages(self):
        rng = random.Random(4)
        self.assertGreater(len({template_quip(cheer(), rng) for _ in range(3000)}), 2000)


class TemplateTests(unittest.TestCase):
    def test_every_tier_fills_all_placeholders_in_every_combination(self):
        for tier in BODIES:
            outcome = "bounce" if tier in ("guard", "round2", "marathon") else "done"
            for wait in (timedelta(minutes=7), None):
                for seed in range(200):
                    with self.subTest(tier=tier, wait=wait, seed=seed):
                        text = template_quip(cheer(tier, outcome, count=4, wait=wait, bounce_round=5),
                                             random.Random(seed))
                        self.assertIsNone(PLACEHOLDER.search(text))
                        self.assertNotIn("  ", text)

    def test_quips_mix_languages_and_rarely_repeat(self):
        rng = random.Random(1)
        quips = {template_quip(cheer(), rng) for _ in range(100)}
        self.assertGreater(len(quips), 80)
        joined = " ".join(quips)
        self.assertRegex(joined, "[А-Яа-я]")
        self.assertIn("Barry Allen", joined)
        self.assertRegex(joined, r"\b(Zo'r|Qoyil|Barakalla|emasmi)\b")

    def test_a_person_does_not_get_the_same_line_twice_in_a_row(self):
        rng, recent, bodies = random.Random(2), deque(maxlen=12), []
        for _ in range(30):
            template_quip(cheer(), rng, recent)
            bodies.append(recent[-1])
        for start in range(len(bodies) - 12):
            self.assertEqual(len(set(bodies[start:start + 12])), 12)

    def test_combo_line_needs_a_batch(self):
        rng = random.Random(3)
        single = " ".join(template_quip(cheer(count=2), rng) for _ in range(50))
        batch = " ".join(template_quip(cheer(count=5), rng) for _ in range(50))
        self.assertNotRegex(single, "×2|2 reviews|2 ta|2 ревью")
        self.assertRegex(batch, "×5|5 reviews|5 ta|5 ревью")


class FakeClaude:
    def __init__(self, response=None, error=None):
        self.create = AsyncMock(return_value=response, side_effect=error)
        self.messages = SimpleNamespace(create=self.create)


def claude_response(text, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[
        SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text),
    ])


class WriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_writes_the_quip_without_seeing_links_or_names(self):
        claude = FakeClaude(claude_response('"Ismingiz Barry Allen emasmi? <b>Flash</b> bo\'lsangiz kerak ⚡"'))
        writer = CheerWriter(claude, rng=random.Random(0))

        text = await writer.message(cheer(), person=7)

        self.assertEqual(text.splitlines()[1], "Ismingiz Barry Allen emasmi? &lt;b&gt;Flash&lt;/b&gt; bo'lsangiz kerak ⚡")
        kwargs = claude.create.await_args.kwargs
        self.assertEqual(kwargs["model"], "claude-haiku-5-5")
        self.assertEqual(kwargs["output_config"], {"effort": "low"})
        self.assertLessEqual(kwargs["max_tokens"], 1024)
        request = kwargs["messages"][0]["content"]
        self.assertIn("7 min", request)
        for private in ("repo", "55", "github", "owner"):
            self.assertNotIn(private, request)

    async def test_recent_quips_are_passed_back_so_claude_avoids_them(self):
        claude = FakeClaude(claude_response("First one"))
        writer = CheerWriter(claude, rng=random.Random(0))
        await writer.message(cheer(), person=7)
        claude.create.return_value = claude_response("Second one")
        await writer.message(cheer(), person=7)
        self.assertIn("- First one", claude.create.await_args.kwargs["messages"][0]["content"])

    async def test_claude_sees_the_quips_people_saw_not_template_markup(self):
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        claude = FakeClaude(error=anthropic.APIConnectionError(request=request))
        writer = CheerWriter(claude, rng=random.Random(5))
        for _ in range(5):
            await writer.message(cheer(), person=7)
        claude.create.side_effect, claude.create.return_value = None, claude_response("Fresh one")

        await writer.message(cheer(), person=7)

        request_text = claude.create.await_args.kwargs["messages"][0]["content"]
        recent = request_text.split("Don't repeat these recent messages or their jokes:\n")[1]
        self.assertEqual(recent.count("\n- ") + 1, 5)
        self.assertNotRegex(recent, r"\{\w+\}")

    async def test_templates_take_over_when_claude_fails_refuses_or_rambles(self):
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx2.Response(200, request=request)
        failures = [
            FakeClaude(error=anthropic.APIConnectionError(request=request)),
            FakeClaude(error=anthropic.APITimeoutError(request=request)),
            FakeClaude(error=anthropic.RateLimitError("slow down", response=httpx2.Response(429, request=request),
                                                      body=None)),
            FakeClaude(error=anthropic.APIResponseValidationError(response=response, body=None)),
            FakeClaude(SimpleNamespace(stop_reason="end_turn", content=None)),
            FakeClaude(claude_response("nope", stop_reason="refusal")),
            FakeClaude(claude_response("")),
            FakeClaude(claude_response("x" * 1000)),
        ]
        for claude in failures:
            with self.subTest(claude=claude.create):
                writer = CheerWriter(claude, rng=random.Random(0))
                text = await writer.message(cheer(), person=7)
                self.assertEqual(len(text.splitlines()), 2)
                self.assertNotIn("nope", text)
                self.assertNotIn("xxx", text)
                # The template line the person got is remembered, Claude's rejected text isn't.
                self.assertIn(writer.recent_lines[7][-1], [line for lines in BODIES["flash"].values() for line in lines])

    async def test_history_forgets_the_least_recently_active_people(self):
        writer = CheerWriter(rng=random.Random(0))
        with patch("gamification.MEMORY_LIMIT", 3):
            for person in range(5):
                await writer.message(cheer(), person=person)
            await writer.message(cheer(), person=2)
            writer.public_line(1, "empty", EMPTY_QUEUE)
        self.assertEqual(list(writer.recent_lines), [3, 4, 2])
        self.assertEqual(list(writer.recent_quips), [3, 4, 2])

    def test_celebration_hour_starts_when_it_was_sent(self):
        writer, now = CheerWriter(rng=random.Random(0)), datetime(2026, 11, 13, tzinfo=timezone.utc)
        self.assertIsNotNone(writer.empty_queue_celebration(1, "@alice", now))
        self.assertIsNotNone(writer.empty_queue_celebration(1, "@alice", now))
        writer.celebrated(1, now)
        self.assertIsNone(writer.empty_queue_celebration(1, "@alice", now + EMPTY_QUEUE_COOLDOWN / 2))
        self.assertIsNotNone(writer.empty_queue_celebration(1, "@alice", now + EMPTY_QUEUE_COOLDOWN))

    def test_claude_is_used_only_with_an_api_key_and_not_switched_off(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(CheerWriter.from_environment().client)
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test", "CHEERS_AI": "off"}, clear=True):
            self.assertIsNone(CheerWriter.from_environment().client)
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}, clear=True):
            writer = CheerWriter.from_environment()
            self.assertEqual(writer.model, "claude-haiku-5-5")
            # A slow Claude falls back to the library quickly instead of retrying.
            self.assertEqual(writer.client.timeout, 8.0)
            self.assertEqual(writer.client.max_retries, 0)
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test", "CHEERS_MODEL": "claude-opus-5-5"}, clear=True):
            self.assertEqual(CheerWriter.from_environment().model, "claude-opus-5-5")


class BotCheerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db = Database(str(Path(temp_dir.name) / "workqueue.db"))
        for name, value in (("db", self.db), ("cheer_writer", CheerWriter(rng=random.Random(0))),
                            ("ephemeral_paused_until", None)):
            patcher = patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def add_task(self, number, queued_minutes_ago=7, author="@author", author_id=1):
        self.db.add_task(1, f"repo/{number}", f"https://github.com/owner/repo/pull/{number}", [], author, author_id,
                         now=datetime.now(timezone.utc) - timedelta(minutes=queued_minutes_ago))

    async def command(self, text, user_id=7, username="alice", send_message=None, delete_message=None,
                      anonymous=False):
        update = SimpleNamespace(
            message=SimpleNamespace(text=text, reply_text=AsyncMock(), sender_chat=object() if anonymous else None,
                                    is_topic_message=False, message_thread_id=None),
            effective_chat=SimpleNamespace(id=1, type="supergroup"),
            effective_user=SimpleNamespace(id=user_id, username=username, first_name="Alice", is_bot=False),
        )
        telegram_bot = SimpleNamespace(
            id=99, send_message=send_message or AsyncMock(return_value=SimpleNamespace(message_id=0)),
            delete_message=delete_message or AsyncMock(), get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        )
        tasks = []
        application = SimpleNamespace(
            create_task=lambda coroutine, name=None: tasks.append(asyncio.create_task(coroutine)))
        await bot.handle_message(update, SimpleNamespace(bot=telegram_bot, application=application))
        await asyncio.gather(*tasks)
        return update, telegram_bot

    async def test_fast_approval_gets_a_private_flash_cheer(self):
        self.add_task(55)

        update, telegram_bot = await self.command("!wdone 55")

        update.message.reply_text.assert_awaited_once()
        self.assertTrue(update.message.reply_text.await_args.args[0].startswith("Removed"))
        telegram_bot.send_message.assert_awaited_once()
        args, kwargs = telegram_bot.send_message.await_args
        self.assertEqual(kwargs["api_kwargs"], {"ephemeral_message_parameters": {"receiver_user_id": 7}})
        self.assertTrue(args[1].startswith('⚡ <a href="https://github.com/owner/repo/pull/55">repo/55</a> · 7 min\n'))

    async def test_slow_and_batched_approvals_still_get_a_cheer(self):
        self.add_task(1, queued_minutes_ago=3 * 24 * 60)
        self.add_task(2, queued_minutes_ago=2 * 24 * 60)
        self.add_task(3, queued_minutes_ago=5 * 60)

        _, telegram_bot = await self.command("!wdone 1-3")

        self.assertTrue(telegram_bot.send_message.await_args.args[1].startswith("🐢 ×3 · "))
        self.assertIn(">repo/3</a> · 5 h", telegram_bot.send_message.await_args.args[1])

    async def test_own_prs_earn_no_cheer(self):
        self.add_task(55, author="@alice", author_id=7)
        self.add_task(56, author="@alice", author_id=None)

        _, telegram_bot = await self.command("!wdone 55 56")

        telegram_bot.send_message.assert_not_awaited()

    async def test_own_pr_under_an_old_username_earns_no_cheer(self):
        # Alice (ID 7) used to be @bob; an entry saved before IDs were recorded only has the old name.
        self.add_task(1, author="@bob", author_id=7)
        self.add_task(55, author="@bob", author_id=None)

        _, telegram_bot = await self.command("!wdone 55", username="alice")

        telegram_bot.send_message.assert_not_awaited()

    async def test_bounce_rounds_skip_authors_bouncing_their_own_pr(self):
        for _ in range(2):
            self.add_task(55)
            _, telegram_bot = await self.command("!wbounce 55", user_id=1, username="author")
            telegram_bot.send_message.assert_not_awaited()
        self.add_task(55)

        _, telegram_bot = await self.command("!wbounce 55")

        self.assertEqual(telegram_bot.send_message.await_args.args[1].splitlines()[0],
                         '🛡️ <a href="https://github.com/owner/repo/pull/55">repo/55</a>')

    def test_has_tasks_checks_one_chat(self):
        self.assertFalse(self.db.has_tasks(1))
        self.add_task(55)
        self.assertTrue(self.db.has_tasks(1))
        self.assertFalse(self.db.has_tasks(2))

    def test_bounces_of_several_prs_are_fetched_together(self):
        for chat_id, number in ((1, 55), (1, 56), (1, 57), (2, 55)):
            self.db.add_task(chat_id, f"repo/{number}", f"https://github.com/owner/repo/pull/{number}", [], "@a", 1)
            task = next(task for task in self.db.get_tasks(chat_id) if task.task_id == f"repo/{number}")
            self.db.complete_tasks(chat_id, [task.id], "bounce", "@b", 2)
        identity = lambda number: ("github.com", "/owner/repo", str(number))

        bounces = self.db.get_bounces(1, [identity(55), identity(56), identity(55)])

        self.assertEqual(sorted(bounce.review_identity for bounce in bounces), [identity(55), identity(56)])
        self.assertEqual(self.db.get_bounces(1, []), [])

    async def test_repeated_bounces_count_rounds(self):
        for expected in ("🛡️ <a", "🥊 <a", "🔁 <a"):
            self.add_task(55)
            _, telegram_bot = await self.command("!wbounce 55")
            self.assertTrue(telegram_bot.send_message.await_args.args[1].startswith(expected))
        self.assertIn("</a> · round 3\n", telegram_bot.send_message.await_args.args[1])

    async def test_bounce_rounds_are_counted_per_chat(self):
        self.db.add_task(2, "repo/55", "https://github.com/owner/repo/pull/55", [], "@author", 1)
        self.db.complete_tasks(2, [self.db.get_tasks(2)[0].id], "bounce", "@bob", 8)
        self.add_task(55)

        _, telegram_bot = await self.command("!wbounce 55")

        self.assertTrue(telegram_bot.send_message.await_args.args[1].startswith("🛡️ "))

    async def test_undeliverable_cheer_stays_out_of_the_chat(self):
        self.add_task(55)

        update, telegram_bot = await self.command(
            "!wdone 55", send_message=AsyncMock(side_effect=BadRequest("ephemeral messages are not allowed")))

        telegram_bot.send_message.assert_awaited_once()
        update.message.reply_text.assert_awaited_once()

    async def test_cheers_resume_a_day_after_a_public_post(self):
        self.add_task(55)
        bot.ephemeral_paused_until = datetime.now(timezone.utc) - timedelta(seconds=1)

        _, telegram_bot = await self.command("!wdone 55")

        telegram_bot.send_message.assert_awaited_once()

    async def test_a_cheer_written_while_another_found_public_posts_is_not_sent(self):
        self.add_task(55)

        async def slow_writer(cheer, person):
            # Meanwhile another cheer finds that this server posts private messages publicly.
            bot.ephemeral_paused_until = datetime.now(timezone.utc) + timedelta(hours=1)
            return "text"

        with patch.object(bot.cheer_writer, "message", side_effect=slow_writer):
            _, telegram_bot = await self.command("!wdone 55")

        telegram_bot.send_message.assert_not_awaited()

    async def test_cheer_posted_publicly_is_deleted_and_cheers_stop(self):
        self.add_task(55)
        self.add_task(56)
        public = AsyncMock(return_value=SimpleNamespace(message_id=42))

        update, telegram_bot = await self.command("!wdone 55", send_message=public)

        telegram_bot.delete_message.assert_awaited_once_with(1, 42)
        update.message.reply_text.assert_awaited_once()
        # The server can't send private messages, so later commands don't flash public ones for a day.
        self.assertGreater(bot.ephemeral_paused_until, datetime.now(timezone.utc) + timedelta(hours=23))
        update, telegram_bot = await self.command("!wdone 56", send_message=public)
        public.assert_awaited_once()
        update.message.reply_text.assert_awaited_once()

    async def test_public_cheer_that_cannot_be_deleted_is_reported(self):
        self.add_task(55)

        update, _ = await self.command(
            "!wdone 55", send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)),
            delete_message=AsyncMock(side_effect=BadRequest("message can't be deleted")))

        self.assertEqual(update.message.reply_text.await_count, 2)
        self.assertIn("please delete it", update.message.reply_text.await_args.args[0])

    async def test_emptying_the_queue_celebrates_publicly_at_most_hourly(self):
        self.add_task(55)
        self.add_task(56)

        update, _ = await self.command("!wdone 55")
        self.assertNotIn("Queue is empty", update.message.reply_text.await_args.args[0])

        update, _ = await self.command("!wdone 56")
        reply = update.message.reply_text.await_args.args[0]
        self.assertIn("\n\n🎉 <b>Queue is empty!</b> @alice closed the last one.\n", reply)
        self.assertTrue(reply.endswith("<code>!wadd &lt;PR/MR link&gt;</code>"))
        self.assertTrue(reply.startswith("Removed"))
        self.assertEqual(update.message.reply_text.await_count, 1)

        self.add_task(57)
        update, _ = await self.command("!wbounce 57")
        self.assertNotIn("Queue is empty", update.message.reply_text.await_args.args[0])

        bot.cheer_writer.last_empty_celebration[1] -= EMPTY_QUEUE_COOLDOWN
        self.add_task(58)
        update, _ = await self.command("!wdone 58")
        self.assertIn("🎉 <b>Queue is empty!</b>", update.message.reply_text.await_args.args[0])

    async def test_someone_who_just_missed_a_removal_learns_the_pr_is_gone(self):
        self.add_task(55)
        await self.command("!wdone repo/55")

        update, _ = await self.command("!wdone repo/55", user_id=8, username="bob")

        self.assertTrue(update.message.reply_text.await_args.args[0].startswith(
            "📭 <code>repo/55</code> isn't in the queue."))

    async def test_closing_your_own_pr_empties_the_queue_without_credit(self):
        self.add_task(55, author="@alice", author_id=7)

        update, _ = await self.command("!wdone 55")

        self.assertIn("🎉 <b>Queue is empty!</b>\n", update.message.reply_text.await_args.args[0])
        self.assertNotIn("closed the last one", update.message.reply_text.await_args.args[0])

    async def test_a_celebration_that_failed_to_send_does_not_use_up_the_hour(self):
        self.add_task(55)
        self.add_task(56)
        await self.command("!wdone 55")
        with patch.object(bot, "reply_blocks", AsyncMock(side_effect=RuntimeError("Telegram is down"))):
            with self.assertRaises(RuntimeError):
                await self.command("!wdone 56")
        self.assertNotIn(1, bot.cheer_writer.last_empty_celebration)

        self.add_task(57)
        update, _ = await self.command("!wdone 57")

        self.assertIn("🎉 <b>Queue is empty!</b>", update.message.reply_text.await_args.args[0])

    async def test_broken_fun_lines_fall_back_to_the_plain_replies(self):
        with patch.object(bot.cheer_writer, "public_line", side_effect=KeyError("count")):
            update, _ = await self.command("!w")
            self.assertEqual(update.message.reply_text.await_args.args[0], "No tasks in the queue.")
            update, _ = await self.command("!wdone 55")
            self.assertEqual(update.message.reply_text.await_args.args[0],
                             "<code>55</code> not found: the queue is empty.")

    async def test_anonymous_closer_gets_no_name_in_the_celebration(self):
        self.add_task(55)

        update, _ = await self.command("!wdone 55", anonymous=True)

        self.assertIn("🎉 <b>Queue is empty!</b>\n", update.message.reply_text.await_args.args[0])

    async def test_w_on_an_empty_queue_adds_a_fun_line_and_a_call_to_action(self):
        update, _ = await self.command("!w")

        text = update.message.reply_text.await_args.args[0]
        first, second, third = text.split("\n")
        self.assertEqual(first, "📭 No tasks in the queue.")
        self.assertTrue(second)
        self.assertTrue(third.startswith("👉 ") and third.endswith(": <code>!wadd &lt;PR/MR link&gt;</code>"))
        self.assertEqual(update.message.reply_text.await_args.kwargs["parse_mode"], "HTML")

    async def test_finishing_reviews_on_an_empty_queue_gets_a_public_nudge(self):
        cases = (
            ("!wdone 55", "<code>55</code> isn't in the queue. It's empty, so there's nothing to mark done."),
            ("!wbounce 1-3", "<code>1-3</code> aren't in the queue. It's empty, so there's nothing to send back."),
            ("!wdone 7, 9", "<code>7</code>, <code>9</code> aren't in the queue. It's empty, so there's nothing to mark done."),
        )
        for command, headline in cases:
            with self.subTest(command=command):
                update, telegram_bot = await self.command(command)

                update.message.reply_text.assert_awaited_once()
                lines = update.message.reply_text.await_args.args[0].split("\n")
                self.assertEqual(lines[0], f"📭 {headline}")
                self.assertEqual(len(lines), 3)
                self.assertNotRegex(lines[1], r"\{\w+\}")
                self.assertTrue(lines[2].endswith("<code>!wadd &lt;PR/MR link&gt;</code>"))
                telegram_bot.send_message.assert_not_awaited()

    async def test_usage_and_invalid_references_still_explain_on_an_empty_queue(self):
        update, _ = await self.command("!wdone")
        self.assertTrue(update.message.reply_text.await_args.args[0].startswith("Usage:"))
        update, _ = await self.command("!wdone #1")
        self.assertIn("no longer supported", update.message.reply_text.await_args.args[0])

    async def test_a_failing_celebration_never_breaks_the_removal(self):
        self.add_task(55)
        with patch.object(bot.cheer_writer, "empty_queue_celebration", side_effect=RuntimeError("boom")):
            update, _ = await self.command("!wdone 55")

        self.assertEqual(self.db.get_tasks(1), [])
        self.assertEqual(update.message.reply_text.await_args.args[0],
                         'Removed <a href="https://github.com/owner/repo/pull/55">repo/55</a> (added by @author)')

    async def test_a_failing_cheer_never_breaks_the_command(self):
        self.add_task(55)
        with patch.object(bot.cheer_writer, "message", AsyncMock(side_effect=RuntimeError("boom"))):
            update, telegram_bot = await self.command("!wdone 55")

        telegram_bot.send_message.assert_not_awaited()
        self.assertEqual(self.db.get_tasks(1), [])
        self.assertTrue(update.message.reply_text.await_args.args[0].startswith("Removed"))


if __name__ == "__main__":
    unittest.main()
