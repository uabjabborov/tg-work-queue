import asyncio
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from telegram.ext import ApplicationBuilder

from database import Database, utc_timestamp
from leaderboard import (
    TASHKENT, RankingEntry, build_leaderboard, format_leaderboard,
    most_recent_recap, reporting_week,
)
from review_urls import parse_review_url
from scheduler import send_weekly_leaderboards, setup_scheduler

with patch.object(Database, "_init_db"):
    import bot


WEEK_START = datetime(2026, 9, 28, tzinfo=TASHKENT)
ACTIVITY_TIME = WEEK_START + timedelta(days=2, hours=13)
DUE = WEEK_START + timedelta(weeks=1, hours=9)
ENROLLED = WEEK_START - timedelta(days=1)


class DatabaseFixture:
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.path = str(Path(temp_dir.name) / "workqueue.db")
        self.db = Database(self.path, now=ENROLLED)

    def add(self, number=120, chat_id=1, author="@alice", author_id=1, url=None, now=ENROLLED):
        url = url or f"https://github.com/owner/repo/pull/{number}"
        task_id = parse_review_url(url)[0]
        self.assertIsNotNone(self.db.add_task(
            chat_id, task_id, url, ["@assigned", "@other"], author, author_id, now=now
        ))
        return self.db.get_tasks(chat_id)[-1]

    def complete(self, task, outcome="done", reviewer="@bob", reviewer_id=2, when=ACTIVITY_TIME):
        return self.db.complete_task(task.chat_id, task.id, outcome, reviewer, reviewer_id, when)

    def finish(self, number=120, chat_id=1, author="@alice", author_id=1,
               reviewer="@bob", reviewer_id=2, outcome="done", when=ACTIVITY_TIME, url=None):
        task = self.add(number, chat_id, author, author_id, url)
        self.assertIsNotNone(self.complete(task, outcome, reviewer, reviewer_id, when))
        return task

    def board(self, chat_id=1, now=ACTIVITY_TIME):
        return build_leaderboard(self.db, chat_id, reporting_week(now))

    def activity(self, chat_id=1):
        week = reporting_week(ACTIVITY_TIME)
        return self.db.get_review_activity(chat_id, week.start_utc, week.end_utc)


class AttributionTests(DatabaseFixture, unittest.TestCase):
    def test_done_credits_submitter_and_command_sender_and_cleans_assignees(self):
        task = self.finish()
        board = self.board()
        self.assertEqual(board.contributors, [RankingEntry("@alice", 1)])
        self.assertEqual(board.reviewers, [RankingEntry("@bob", 1)])
        self.assertEqual(self.db.get_tasks(1), [])
        event, = self.activity()
        self.assertEqual((event.created_by_id, event.completed_by_id, event.outcome), (1, 2, "done"))
        self.assertEqual(event.review_identity, ("github.com", "/owner/repo", "120"))
        self.assertEqual(event.completed_at, ACTIVITY_TIME.astimezone(timezone.utc))
        with self.db._get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM task_assignees").fetchone()[0], 0)
            row = conn.execute("SELECT * FROM review_activity").fetchone()
            self.assertEqual((row["queue_task_id"], row["url"]), (task.id, task.url))

    def test_bounce_credits_only_command_sender(self):
        self.finish(outcome="bounce")
        self.assertEqual(self.board().contributors, [])
        self.assertEqual(self.board().reviewers, [RankingEntry("@bob", 1)])

    def test_self_reviews_use_ids_across_renames_for_both_outcomes(self):
        self.finish(reviewer="@renamed", reviewer_id=1)
        self.finish(number=121, outcome="bounce", reviewer="@renamed", reviewer_id=1)
        self.assertEqual(self.board().contributors, [RankingEntry("@renamed", 1)])
        self.assertEqual(self.board().reviewers, [])

    def test_different_ids_with_the_same_name_are_not_self_reviews(self):
        self.finish(author="Same Name", reviewer="Same Name")
        self.assertEqual(self.board().contributors, [RankingEntry("Same Name", 1)])
        self.assertEqual(self.board().reviewers, [RankingEntry("Same Name", 1)])

    def test_legacy_self_review_falls_back_to_saved_name(self):
        self.finish(author_id=None, reviewer="@ALICE", reviewer_id=None)
        self.assertEqual(self.board().contributors, [RankingEntry("@alice", 1)])
        self.assertEqual(self.board().reviewers, [])

    def test_legacy_alias_resolves_renamed_self_reviewer(self):
        self.add(number=1)
        self.add(number=2, author="@renamed", now=ACTIVITY_TIME - timedelta(hours=1))
        self.finish(author_id=None, reviewer="@renamed", reviewer_id=1)
        self.assertEqual(self.board().contributors, [RankingEntry("@renamed", 1)])
        self.assertEqual(self.board().reviewers, [])

    def test_readded_reviews_dedupe_per_person_ranking_and_week(self):
        self.finish(outcome="bounce", url="http://GITHUB.COM/owner/repo/pull/00120///?x=1#note")
        self.finish(url="https://github.com/owner/repo/pull/120?tab=files")
        self.finish()
        self.finish(reviewer="@charlie", reviewer_id=3)
        board = self.board()
        self.assertEqual(board.contributors, [RankingEntry("@alice", 1)])
        self.assertEqual(board.reviewers, [RankingEntry("@bob", 1), RankingEntry("@charlie", 1)])
        self.assertEqual(len(self.activity()), 4)
        next_week = ACTIVITY_TIME + timedelta(weeks=1)
        self.finish(when=next_week)
        self.assertEqual(self.board(now=next_week).contributors, [RankingEntry("@alice", 1)])
        self.assertEqual(self.board(now=next_week).reviewers, [RankingEntry("@bob", 1)])

    def test_distinct_hosts_full_projects_and_numbers_each_count(self):
        urls = (
            "https://gitlab.example.com/teams/one/repo/-/merge_requests/120",
            "https://gitlab.example.com/teams/two/repo/-/merge_requests/120",
            "https://other.example.com/teams/one/repo/-/merge_requests/120",
            "https://gitlab.example.com/teams/one/repo/-/merge_requests/121",
        )
        for url in urls:
            self.finish(url=url)
        self.assertEqual(self.board().contributors, [RankingEntry("@alice", 4)])
        self.assertEqual(self.board().reviewers, [RankingEntry("@bob", 4)])

    def test_chat_activity_and_identity_matching_are_isolated(self):
        self.finish(author_id=None)
        self.add(number=121)
        self.finish(chat_id=2, author="@alice", author_id=3, reviewer="@dave", reviewer_id=4)
        self.assertEqual(self.board().contributors, [RankingEntry("@alice", 1)])
        self.assertEqual(self.board().reviewers, [RankingEntry("@bob", 1)])
        self.assertEqual(self.board(2).reviewers, [RankingEntry("@dave", 1)])
        self.assertTrue(self.board(3).empty)

    def test_ids_keep_credit_together_and_old_tasks_do_not_revert_names(self):
        old_task = self.add()
        new_task = self.add(number=121, author="@alicia", now=ACTIVITY_TIME - timedelta(hours=1))
        self.complete(old_task, reviewer="@bob", when=ACTIVITY_TIME)
        self.complete(new_task, reviewer="@bobby", when=ACTIVITY_TIME + timedelta(minutes=1))
        self.assertEqual(self.board().contributors, [RankingEntry("@alicia", 2)])
        self.assertEqual(self.board().reviewers, [RankingEntry("@bobby", 2)])

    def test_unambiguous_legacy_names_merge_with_known_ids(self):
        self.finish(author_id=None)
        self.finish(number=121)
        self.assertEqual(self.board().contributors, [RankingEntry("@alice", 2)])

    def test_ambiguous_legacy_alias_keeps_separate_attribution(self):
        self.finish(author="@common", reviewer="Unknown", reviewer_id=None)
        self.finish(number=121, author="@common", author_id=3, reviewer="Unknown", reviewer_id=None)
        self.add(number=122, author="@alice", now=ACTIVITY_TIME + timedelta(minutes=1))
        self.add(number=123, author="@charlie", author_id=3, now=ACTIVITY_TIME + timedelta(minutes=1))
        self.finish(number=124, author="@common", author_id=None, reviewer="Unknown", reviewer_id=None)
        self.assertEqual(self.board().contributors, [
            RankingEntry("@alice", 1), RankingEntry("@charlie", 1), RankingEntry("@common", 1)
        ])

    def test_unknown_identities_receive_no_individual_credit(self):
        self.finish(author="Unknown", author_id=None, reviewer="Anonymous", reviewer_id=None)
        self.assertTrue(self.board().empty)
        self.finish(number=121, author="Unknown", author_id=None)
        self.finish(number=122, reviewer="Unknown", reviewer_id=None)
        self.assertEqual(self.board().contributors, [RankingEntry("@alice", 1)])
        self.assertEqual(self.board().reviewers, [RankingEntry("@bob", 1)])

    def test_known_ids_take_precedence_over_literal_unknown_display_names(self):
        self.finish(author="Unknown", reviewer="Anonymous")
        self.assertEqual(self.board().contributors, [RankingEntry("Unknown", 1)])
        self.assertEqual(self.board().reviewers, [RankingEntry("Anonymous", 1)])

    def test_top_five_counts_ties_and_html_escaping(self):
        names = ["Zulu", "charlie", "Bob <&> \"Team\"", "echo", "delta", "alice", "foxtrot"]
        for i, name in enumerate(names, start=1):
            self.finish(number=i, author=name, author_id=100 + i, reviewer="Unknown", reviewer_id=None)
        self.finish(number=8, author="Zulu", author_id=101, reviewer="Unknown", reviewer_id=None)
        board = self.board()
        self.assertEqual(board.contributors, [
            RankingEntry("Zulu", 2), RankingEntry("alice", 1), RankingEntry('Bob <&> "Team"', 1),
            RankingEntry("charlie", 1), RankingEntry("delta", 1)
        ])
        text = format_leaderboard(board)
        self.assertIn("Bob &lt;&amp;&gt; &quot;Team&quot;", text)
        self.assertIn("1. Zulu — 2", text)
        self.assertIn("2026-09-28 – 2026-10-04 (Asia/Tashkent)", text)
        self.assertIn("No reviewer points for this period.", text)
        self.assertNotIn("foxtrot", text)

    def test_empty_standings_show_both_sections(self):
        text = format_leaderboard(self.board())
        self.assertIn("<b>Contributors</b>", text)
        self.assertIn("No contributor points for this period.", text)
        self.assertIn("<b>Reviewers</b>", text)
        self.assertIn("No reviewer points for this period.", text)


class PersistenceTests(DatabaseFixture, unittest.TestCase):
    def test_repeated_or_stale_completions_cannot_remove_readded_queue_entry(self):
        task = self.finish()
        self.assertIsNone(self.complete(task))
        new_task = self.add()
        self.assertIsNone(self.complete(task))
        self.assertEqual(self.db.get_tasks(1), [new_task])
        self.assertEqual(len(self.activity()), 1)
        self.complete(new_task)
        self.assertEqual(len(self.activity()), 2)
        self.assertEqual(self.board().contributors, [RankingEntry("@alice", 1)])

    def test_concurrent_commands_archive_only_one_event(self):
        task = self.add()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.complete(task), range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.activity()), 1)

    def test_failures_at_each_transaction_stage_leave_queue_and_history_intact(self):
        for i, (table, action) in enumerate((
            ("review_activity", "INSERT"), ("task_assignees", "DELETE"), ("tasks", "DELETE")
        )):
            with self.subTest(table=table):
                task = self.add(number=120 + i)
                before = self.db.get_tasks(1)
                event_count = len(self.activity())
                with self.db._get_connection() as conn:
                    conn.execute(f"""
                        CREATE TRIGGER fail_completion BEFORE {action} ON {table}
                        BEGIN SELECT RAISE(ABORT, 'simulated transaction failure'); END
                    """)
                with self.assertRaises(sqlite3.IntegrityError):
                    self.complete(task, reviewer_id=20 + i)
                self.assertEqual(self.db.get_tasks(1), before)
                self.assertEqual(len(self.activity()), event_count)
                self.assertNotIn((20 + i, "@bob"), self.db.get_known_users(1))
                with self.db._get_connection() as conn:
                    conn.execute("DROP TRIGGER fail_completion")
                self.complete(task)

    def test_history_and_deduplication_survive_deletion_and_restart(self):
        self.finish()
        self.db = Database(self.path, now=DUE)
        self.assertEqual(self.db.get_tasks(1), [])
        self.assertEqual(self.board().contributors, [RankingEntry("@alice", 1)])
        self.finish()
        self.assertEqual(self.board().reviewers, [RankingEntry("@bob", 1)])
        self.assertEqual(len(self.activity()), 2)

    def test_successful_submission_enrolls_and_preserves_opt_out_and_counter(self):
        self.assertIsNone(self.db.get_leaderboard_settings(1))
        self.add()
        original = self.db.get_leaderboard_settings(1)
        self.assertTrue(original.enabled)
        self.assertEqual(original.enabled_since, ENROLLED)
        self.db.set_leaderboard_enabled(1, False, ACTIVITY_TIME)
        self.assertIsNone(self.db.add_task(1, "repo/120", "https://github.com/owner/repo/pull/120",
                                         [], "@duplicate", 99, now=ACTIVITY_TIME))
        self.assertNotIn((99, "@duplicate"), self.db.get_known_users(1))
        self.db = Database(self.path, now=DUE)
        task = self.add(number=121, now=ACTIVITY_TIME)
        self.assertEqual(task.seq_num, 2)
        self.assertFalse(self.db.get_leaderboard_settings(1).enabled)
        self.db.set_leaderboard_enabled(2, False, ENROLLED)
        self.add(chat_id=2)
        self.assertFalse(self.db.get_leaderboard_settings(2).enabled)

    def test_failed_enrollment_rolls_back_the_submission(self):
        with self.db._get_connection() as conn:
            conn.execute("""
                CREATE TRIGGER fail_enrollment BEFORE INSERT ON leaderboard_settings
                BEGIN SELECT RAISE(ABORT, 'enrollment failure'); END
            """)
        self.assertIsNone(self.db.add_task(1, "repo/120", "https://github.com/owner/repo/pull/120",
                                         ["@bob"], "@alice", 1, now=ENROLLED))
        self.assertEqual(self.db.get_tasks(1), [])
        self.assertEqual(self.db.get_known_users(1), [])
        self.assertIsNone(self.db.get_leaderboard_settings(1))
        with self.db._get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM seq_counters").fetchone()[0], 0)


class WeekTests(DatabaseFixture, unittest.TestCase):
    def test_tashkent_midnight_uses_correct_utc_boundaries(self):
        before = datetime(2026, 9, 27, 18, 59, 59, tzinfo=timezone.utc)
        at = before + timedelta(seconds=1)
        self.assertEqual(reporting_week(before).start.date(), date(2026, 9, 21))
        week = reporting_week(at)
        self.assertEqual(week.start, WEEK_START)
        self.assertEqual(week.start_utc, datetime(2026, 9, 27, 19, tzinfo=timezone.utc))
        self.assertEqual(week.end_utc, datetime(2026, 10, 4, 19, tzinfo=timezone.utc))

    def test_year_transition(self):
        now = datetime(2027, 1, 1, 12, tzinfo=TASHKENT)
        week = reporting_week(now)
        self.assertEqual((week.start.date(), week.end.date()), (date(2026, 12, 28), date(2027, 1, 4)))
        self.assertEqual(reporting_week(now, previous=True).start.date(), date(2026, 12, 21))

    def test_completion_week_is_exclusive_at_end_and_independent_of_submission(self):
        week = reporting_week(ACTIVITY_TIME)
        times = [week.start_utc - timedelta(microseconds=1), week.start_utc,
                 week.end_utc - timedelta(microseconds=1), week.end_utc]
        for i, when in enumerate(times):
            self.finish(number=120 + i, when=when)
        self.assertEqual(self.board().contributors, [RankingEntry("@alice", 2)])
        self.assertEqual(self.board(now=week.start - timedelta(seconds=1)).contributors, [RankingEntry("@alice", 1)])
        self.assertEqual(self.board(now=week.end).contributors, [RankingEntry("@alice", 1)])

    def test_most_recent_due_changes_only_at_monday_nine(self):
        prior, prior_due = most_recent_recap(DUE - timedelta(microseconds=1))
        self.assertEqual(prior.start, WEEK_START - timedelta(weeks=1))
        self.assertEqual(prior_due, DUE - timedelta(weeks=1))
        week, due = most_recent_recap(DUE)
        self.assertEqual((week.start, due), (WEEK_START, DUE))
        self.assertEqual(most_recent_recap(DUE + timedelta(days=6)), (week, due))

    def test_naive_times_are_rejected_without_removing_the_task(self):
        with self.assertRaises(ValueError):
            reporting_week(datetime(2026, 10, 1))
        task = self.add()
        with self.assertRaises(ValueError):
            self.complete(task, when=datetime(2026, 10, 1))
        self.assertEqual(self.db.get_tasks(1), [task])
        self.assertEqual(self.activity(), [])


class MigrationTests(unittest.TestCase):
    def test_old_queue_reminders_counters_and_legacy_credit_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "workqueue.db")
            with sqlite3.connect(path) as conn:
                conn.executescript("""
                    CREATE TABLE tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                        seq_num INTEGER NOT NULL, task_id TEXT NOT NULL, url TEXT NOT NULL,
                        assigned_to TEXT NOT NULL, created_by TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(chat_id, task_id), UNIQUE(chat_id, seq_num)
                    );
                    CREATE TABLE seq_counters (chat_id INTEGER PRIMARY KEY, next_num INTEGER DEFAULT 1);
                    CREATE TABLE reminders (
                        chat_id INTEGER PRIMARY KEY, cron_expression TEXT NOT NULL,
                        enabled BOOLEAN NOT NULL DEFAULT 1,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    INSERT INTO tasks (chat_id, seq_num, task_id, url, assigned_to, created_by, created_at)
                    VALUES (1, 10, 'repo/120', 'https://github.com/owner/repo/pull/120', '@bob', '@alice', '2026-09-20 10:00:00'),
                           (4, 1, 'repo/121', 'https://github.com/owner/repo/pull/121', 'unassigned', '@dave', '2026-09-20 10:00:00');
                    INSERT INTO seq_counters VALUES (1, 11), (2, 8);
                    INSERT INTO reminders (chat_id, cron_expression, enabled) VALUES (3, '0 9 * * *', 0);
                """)
            db = Database(path, now=ENROLLED)
            self.assertEqual([s.chat_id for s in db.get_active_leaderboard_settings()], [1, 2, 3, 4])
            task, = db.get_tasks(1)
            self.assertEqual((task.seq_num, task.assignees, task.created_by, task.created_by_id),
                             (10, ["@bob"], "@alice", None))
            self.assertEqual(task.created_at, "2026-09-20 10:00:00")
            self.assertFalse(db.get_reminder(3).enabled)
            self.assertTrue(build_leaderboard(db, 1, reporting_week(ACTIVITY_TIME)).empty)
            self.assertEqual(db.add_task(1, "repo/122", "https://github.com/owner/repo/pull/122",
                                         ["@bob", "@charlie"], "@alice", 1, now=ENROLLED), 11)
            db.complete_task(1, task.id, "done", "@bob", 2, ACTIVITY_TIME)
            db.set_leaderboard_enabled(2, False, ACTIVITY_TIME)
            db = Database(path, now=DUE)
            self.assertFalse(db.get_leaderboard_settings(2).enabled)
            self.assertEqual(db.get_leaderboard_settings(1).enabled_since, ENROLLED)
            self.assertEqual(db.get_tasks(1)[0].assignees, ["@bob", "@charlie"])
            self.assertEqual(build_leaderboard(db, 1, reporting_week(ACTIVITY_TIME)).contributors,
                             [RankingEntry("@alice", 1)])


class RecapTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.send_message = AsyncMock()
        self.application = SimpleNamespace(bot=SimpleNamespace(send_message=self.send_message), bot_data={})

    async def test_no_early_recap_then_monday_nine_sends_shared_standings(self):
        self.finish()
        await send_weekly_leaderboards(self.application, self.db, DUE - timedelta(microseconds=1))
        self.send_message.assert_not_awaited()
        await send_weekly_leaderboards(self.application, self.db, DUE)
        self.send_message.assert_awaited_once_with(
            chat_id=1, text=format_leaderboard(self.board()), parse_mode="HTML", disable_web_page_preview=True
        )
        self.assertTrue(self.db.has_leaderboard_delivery(1, WEEK_START.date()))

    async def test_restart_catches_up_overdue_recap_and_suppresses_duplicates(self):
        self.finish()
        self.db = Database(self.path, now=DUE + timedelta(days=2))
        await send_weekly_leaderboards(self.application, self.db, DUE + timedelta(days=2))
        self.send_message.assert_awaited_once()
        self.db = Database(self.path, now=DUE + timedelta(days=3))
        await send_weekly_leaderboards(self.application, self.db, DUE + timedelta(days=3))
        self.send_message.assert_awaited_once()

    async def test_only_most_recent_overdue_report_is_sent_after_long_downtime(self):
        self.finish()
        latest_time = ACTIVITY_TIME + timedelta(weeks=2)
        self.finish(number=121, author="@charlie", author_id=3, when=latest_time)
        now = DUE + timedelta(weeks=2, days=1)
        await send_weekly_leaderboards(self.application, self.db, now)
        self.send_message.assert_awaited_once()
        text = self.send_message.await_args.kwargs["text"]
        self.assertIn("2026-10-12 – 2026-10-18", text)
        self.assertIn("@charlie", text)
        self.assertNotIn("@alice", text)
        self.assertFalse(self.db.has_leaderboard_delivery(1, WEEK_START.date()))

    async def test_old_activity_is_not_sent_when_latest_due_week_is_empty(self):
        self.finish()
        await send_weekly_leaderboards(self.application, self.db, DUE + timedelta(weeks=2))
        self.send_message.assert_not_awaited()

    async def test_empty_chats_are_skipped_and_reviewer_only_chats_are_sent(self):
        self.add()
        self.finish(chat_id=2, outcome="bounce")
        await send_weekly_leaderboards(self.application, self.db, DUE)
        self.send_message.assert_awaited_once()
        self.assertEqual(self.send_message.await_args.kwargs["chat_id"], 2)
        self.assertIn("No contributor points for this period.", self.send_message.await_args.kwargs["text"])
        self.assertFalse(self.db.has_leaderboard_delivery(1, WEEK_START.date()))

    async def test_opt_out_persists_and_tracking_continues(self):
        self.finish()
        self.db.set_leaderboard_enabled(1, False, ACTIVITY_TIME)
        self.finish(number=121)
        self.db = Database(self.path, now=DUE)
        await send_weekly_leaderboards(self.application, self.db, DUE)
        self.send_message.assert_not_awaited()
        self.assertEqual(self.board().contributors, [RankingEntry("@alice", 2)])

    async def test_reenable_after_due_waits_for_next_monday_even_after_restart(self):
        self.finish()
        self.db.set_leaderboard_enabled(1, False, ACTIVITY_TIME)
        self.db.set_leaderboard_enabled(1, True, DUE + timedelta(hours=1))
        self.db = Database(self.path, now=DUE + timedelta(days=1))
        await send_weekly_leaderboards(self.application, self.db, DUE + timedelta(days=1))
        self.send_message.assert_not_awaited()
        self.finish(number=121, when=ACTIVITY_TIME + timedelta(weeks=1))
        await send_weekly_leaderboards(self.application, self.db, DUE + timedelta(weeks=1))
        self.send_message.assert_awaited_once()
        self.assertIn("2026-10-05 – 2026-10-11", self.send_message.await_args.kwargs["text"])

    async def test_enable_before_due_is_eligible_and_repeated_on_does_not_postpone(self):
        self.finish()
        self.db.set_leaderboard_enabled(1, False, ACTIVITY_TIME)
        self.db.set_leaderboard_enabled(1, True, DUE - timedelta(minutes=1))
        self.db.set_leaderboard_enabled(1, True, DUE + timedelta(hours=1))
        await send_weekly_leaderboards(self.application, self.db, DUE + timedelta(hours=1))
        self.send_message.assert_awaited_once()

    async def test_reenable_exactly_at_deadline_waits_for_next_delivery(self):
        self.finish()
        self.db.set_leaderboard_enabled(1, False, ACTIVITY_TIME)
        self.db.set_leaderboard_enabled(1, True, DUE)
        await send_weekly_leaderboards(self.application, self.db, DUE)
        self.send_message.assert_not_awaited()

    async def test_chat_first_enrolled_after_due_does_not_receive_older_report(self):
        task = self.add(now=DUE + timedelta(minutes=1))
        self.complete(task)  # A backdated event cannot opt the chat into an earlier deadline.
        await send_weekly_leaderboards(self.application, self.db, DUE + timedelta(days=1))
        self.send_message.assert_not_awaited()

    async def test_delivery_failure_is_logged_isolated_and_retried_without_duplicates(self):
        self.finish()
        self.finish(chat_id=2)

        async def fail_first_chat(**kwargs):
            if kwargs["chat_id"] == 1:
                raise RuntimeError("Telegram unavailable for this chat")

        self.send_message.side_effect = fail_first_chat
        with self.assertLogs("scheduler", level="ERROR") as logs:
            await send_weekly_leaderboards(self.application, self.db, DUE)
        self.assertIn("chat 1", logs.output[0])
        self.assertEqual(self.send_message.await_count, 2)
        self.assertFalse(self.db.has_leaderboard_delivery(1, WEEK_START.date()))
        self.assertTrue(self.db.has_leaderboard_delivery(2, WEEK_START.date()))
        self.send_message.reset_mock(side_effect=True)
        self.db = Database(self.path, now=DUE + timedelta(hours=1))
        await send_weekly_leaderboards(self.application, self.db, DUE + timedelta(hours=1))
        self.send_message.assert_awaited_once()
        self.assertEqual(self.send_message.await_args.kwargs["chat_id"], 1)

    async def test_toggle_during_another_chats_delivery_is_respected(self):
        self.finish()
        self.finish(chat_id=2)

        async def turn_second_chat_off(**kwargs):
            self.db.set_leaderboard_enabled(2, False, DUE)

        self.send_message.side_effect = turn_second_chat_off
        await send_weekly_leaderboards(self.application, self.db, DUE)
        self.send_message.assert_awaited_once()
        self.assertEqual(self.send_message.await_args.kwargs["chat_id"], 1)

    async def test_scheduler_uses_separate_tashkent_trigger_and_pauses_during_catch_up(self):
        self.db.set_reminder(1, "0 9 * * *")
        scheduler = Mock(running=False)
        scheduler.get_job.return_value = None
        caught_up = AsyncMock()
        timeline = Mock()
        timeline.attach_mock(scheduler.start, "start")
        timeline.attach_mock(caught_up, "catch_up")
        timeline.attach_mock(scheduler.resume, "resume")
        with patch("scheduler.get_scheduler", return_value=scheduler), \
                patch("scheduler.send_weekly_leaderboards", caught_up):
            self.assertIs(await setup_scheduler(self.application, self.db), scheduler)
        scheduler.start.assert_called_once_with(paused=True)
        caught_up.assert_awaited_once_with(self.application, self.db)
        scheduler.resume.assert_called_once_with()
        self.assertEqual([call[0] for call in timeline.mock_calls], ["start", "catch_up", "resume"])
        reminder_job, weekly_job = scheduler.add_job.call_args_list
        self.assertEqual(str(reminder_job.kwargs["trigger"].timezone), "UTC")
        trigger = weekly_job.kwargs["trigger"]
        self.assertEqual(str(trigger.timezone), "Asia/Tashkent")
        self.assertEqual(trigger.get_next_fire_time(None, DUE - timedelta(seconds=1)), DUE)
        self.assertEqual(trigger.get_next_fire_time(DUE, DUE), DUE + timedelta(weeks=1))
        self.assertTrue(weekly_job.kwargs["coalesce"])
        self.assertEqual(weekly_job.kwargs["max_instances"], 1)


class LeaderboardCommandTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        db_patch = patch.object(bot, "db", self.db)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        clock_patch = patch("database.utc_timestamp", side_effect=lambda now=None: utc_timestamp(now or ACTIVITY_TIME))
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        week_patch = patch.object(bot, "reporting_week",
                                 side_effect=lambda previous=False: reporting_week(ACTIVITY_TIME, previous))
        week_patch.start()
        self.addCleanup(week_patch.stop)

    async def command(self, text, user_id=2, username="bob", first_name="Bob", chat_id=1,
                      anonymous=False, unknown=False, bot_sender=False):
        reply = AsyncMock()
        user = None if unknown else SimpleNamespace(id=user_id, username=username, first_name=first_name, is_bot=bot_sender)
        update = SimpleNamespace(
            message=SimpleNamespace(text=text, reply_text=reply, sender_chat=object() if anonymous else None),
            effective_chat=SimpleNamespace(id=chat_id), effective_user=user,
        )
        await bot.handle_message(update, None)
        reply.assert_awaited_once()
        return reply.await_args

    async def test_commands_save_ids_and_credit_sender_instead_of_assignees(self):
        await self.command("!wadd https://github.com/owner/repo/pull/120 @assigned @other",
                           user_id=1, username="alice")
        self.assertEqual(self.db.get_tasks(1)[0].created_by_id, 1)
        await self.command("!wdone 120", user_id=4, username="dave")
        current = await self.command("!wleaderboard")
        self.assertEqual(current.args[0], format_leaderboard(self.board()))
        self.assertIn("@alice", current.args[0])
        self.assertIn("@dave", current.args[0])
        self.assertNotIn("@assigned", current.args[0])
        self.assertEqual(current.kwargs["parse_mode"], "HTML")

    async def test_previous_week_and_case_insensitive_routing(self):
        self.finish(when=ACTIVITY_TIME - timedelta(weeks=1))
        previous = await self.command("!WLEADERBOARD LAST")
        self.assertIn("2026-09-21 – 2026-09-27", previous.args[0])
        self.assertIn("@alice", previous.args[0])
        current = await self.command("!wleaderboard")
        self.assertIn("No contributor points", current.args[0])
        self.assertIn("No reviewer points", current.args[0])

    async def test_off_on_commands_are_per_chat_and_do_not_stop_tracking(self):
        await self.command("!wleaderboard-off", user_id=8, username="any_member")
        self.finish()
        self.assertFalse(self.db.get_leaderboard_settings(1).enabled)
        self.assertIsNone(self.db.get_leaderboard_settings(2))
        self.assertIn("@alice", (await self.command("!wleaderboard")).args[0])
        enabled = await self.command("!wleaderboard-on", user_id=9, username="another_member")
        self.assertTrue(self.db.get_leaderboard_settings(1).enabled)
        self.assertIn("next scheduled recap", enabled.args[0])

    async def test_bounce_and_repeated_or_invalid_commands_do_not_add_extra_credit(self):
        self.add()
        await self.command("!wbounce repo/120")
        await self.command("!wbounce repo/120")
        await self.command("!wdone 999")
        await self.command("!wdone #1")
        self.assertEqual(len(self.activity()), 1)
        self.assertEqual(self.board().contributors, [])
        self.assertEqual(self.board().reviewers, [RankingEntry("@bob", 1)])

    async def test_anonymous_and_unknown_senders_receive_no_individual_entries(self):
        await self.command("!wadd https://github.com/owner/repo/pull/120", anonymous=True)
        task = self.db.get_tasks(1)[0]
        self.assertEqual((task.created_by, task.created_by_id), ("Unknown", None))
        await self.command("!wdone 120", anonymous=True)
        self.assertTrue(self.board().empty)
        await self.command("!wadd https://github.com/owner/repo/pull/121", unknown=True)
        await self.command("!wbounce 121", unknown=True)
        self.assertTrue(self.board().empty)
        await self.command("!wadd https://github.com/owner/repo/pull/122",
                           user_id=1087968824, username="GroupAnonymousBot", bot_sender=True)
        await self.command("!wdone 122", bot_sender=True)
        self.assertTrue(self.board().empty)

    async def test_html_names_without_usernames_are_escaped(self):
        await self.command("!wadd https://github.com/owner/repo/pull/120",
                           user_id=1, username=None, first_name="<Alice> & Team")
        await self.command("!wdone 120", username=None, first_name="<Bob>")
        reply = await self.command("!wleaderboard")
        self.assertIn("&lt;Alice&gt; &amp; Team", reply.args[0])
        self.assertIn("&lt;Bob&gt;", reply.args[0])

    async def test_failed_command_transaction_leaves_queue_without_credit(self):
        task = self.add()
        with self.db._get_connection() as conn:
            conn.execute("""
                CREATE TRIGGER fail_delete BEFORE DELETE ON tasks
                BEGIN SELECT RAISE(ABORT, 'delete failure'); END
            """)
        with self.assertLogs("bot", level="ERROR"):
            reply = await self.command("!wdone 120")
        self.assertIn("Please try again", reply.args[0])
        self.assertEqual(self.db.get_tasks(1), [task])
        self.assertTrue(self.board().empty)

    async def test_help_and_invalid_leaderboard_arguments_explain_commands_and_rules(self):
        help_reply = await self.command("!whelp")
        for command in ("!wleaderboard", "!wleaderboard last", "!wleaderboard-off", "!wleaderboard-on"):
            self.assertIn(command, help_reply.args[0])
        self.assertIn("Monday at 09:00 Asia/Tashkent", help_reply.args[0])
        self.assertIn("Self-reviews earn no reviewer points", help_reply.args[0])
        self.assertIn("one point per PR/MR per ranking", help_reply.args[0])
        self.assertLess(len(help_reply.args[0]), 4096)
        invalid = await self.command("!wleaderboard next")
        self.assertIn("Usage:", invalid.args[0])

    async def test_scheduler_starts_only_from_async_initialized_application_hook(self):
        application = SimpleNamespace(bot_data={})
        scheduler = Mock(running=True)
        setup = AsyncMock(return_value=scheduler)
        with patch.object(bot, "setup_scheduler", setup):
            await bot.post_init(application)
        setup.assert_awaited_once_with(application, self.db)
        self.assertIs(application.bot_data["scheduler"], scheduler)
        await bot.post_stop(application)
        scheduler.shutdown.assert_called_once_with(wait=False)
        original_build = ApplicationBuilder.build
        built_apps = []

        def capture_application(builder):
            app = original_build(builder)
            built_apps.append(app)
            return app

        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "123456:test-token"}), \
                patch.object(ApplicationBuilder, "build", autospec=True, side_effect=capture_application), \
                patch.object(bot.Application, "run_polling") as poll:
            bot.main()
        poll.assert_called_once_with(allowed_updates=bot.Update.ALL_TYPES)
        self.assertIs(built_apps[0].post_init, bot.post_init)
        self.assertIs(built_apps[0].post_stop, bot.post_stop)

    async def test_real_scheduler_starts_and_stops_on_the_application_event_loop(self):
        application = SimpleNamespace(bot_data={})
        with patch("scheduler._scheduler", None), \
                patch("scheduler.send_weekly_leaderboards", new_callable=AsyncMock) as catch_up:
            await bot.post_init(application)
            scheduler = application.bot_data["scheduler"]
            try:
                self.assertTrue(scheduler.running)
                self.assertIsNotNone(scheduler.get_job("weekly_leaderboards"))
                catch_up.assert_awaited_once_with(application, self.db)
            finally:
                await bot.post_stop(application)
                await asyncio.sleep(0)
            self.assertFalse(scheduler.running)


if __name__ == "__main__":
    unittest.main()
