import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient

import mcp_server
from database import Database
from review_details import ReviewDetails

CHAT_ID = -100
HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "Authorization": "Bearer secret",
}


class McpServerTests(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db = Database(str(Path(temp_dir.name) / "workqueue.db"))
        self.bot = MagicMock()
        self.bot.initialize = AsyncMock()
        self.bot.send_message = AsyncMock()
        bot_patch = patch.object(mcp_server, "Bot", return_value=self.bot)
        bot_patch.start()
        self.addCleanup(bot_patch.stop)
        self.fetch_details = AsyncMock(return_value=None)
        fetch_patch = patch.object(mcp_server, "fetch_review_details", self.fetch_details)
        fetch_patch.start()
        self.addCleanup(fetch_patch.stop)

        server = mcp_server.create_server(self.db, "bot-token", CHAT_ID)
        app = mcp_server.build_app(server, {"secret": mcp_server.Submitter("@dave", 7)})
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def call_tool(self, name, arguments, headers=HEADERS):
        response = self.client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        return response

    def tool_result(self, name, arguments):
        result = self.call_tool(name, arguments).json()["result"]
        return result["isError"], result["content"][0]["text"]

    def test_add_review_queues_task_for_token_owner_and_announces_note(self):
        is_error, text = self.tool_result("add_review", {
            "url": "https://github.com/owner/repo/pull/45",
            "reviewers": ["alice", "@bob", "@alice"],
            "note": "Check <migrations> & tests",
        })

        self.assertFalse(is_error)
        self.assertEqual(text, "Added repo/45 to the review queue, assigned to @alice, @bob.")
        [task] = self.db.get_tasks(CHAT_ID)
        self.assertEqual((task.task_id, task.assignees, task.created_by, task.created_by_id),
                         ("repo/45", ["@alice", "@bob"], "@dave", 7))
        self.bot.send_message.assert_awaited_once()
        chat_id, message = self.bot.send_message.await_args.args
        self.assertEqual(chat_id, CHAT_ID)
        self.assertEqual(message, (
            '<a href="https://github.com/owner/repo/pull/45">repo/45</a> → @alice, @bob (by @dave)\n'
            "<blockquote>Check &lt;migrations&gt; &amp; tests</blockquote>"
        ))

    def test_add_review_saves_title_and_description_from_the_agent(self):
        is_error, _ = self.tool_result("add_review", {
            "url": "https://gitlab.internal/team/repo/-/merge_requests/7",
            "reviewers": ["@alice"],
            "title": "  Fix <login>\n redirect ",
            "description": "<!-- template -->\nRedirect after login.",
        })

        self.assertFalse(is_error)
        self.fetch_details.assert_not_awaited()
        [task] = self.db.get_tasks(CHAT_ID)
        self.assertEqual((task.title, task.description), ("Fix <login> redirect", "Redirect after login."))
        self.assertEqual(self.bot.send_message.await_args.args[1], (
            '<a href="https://gitlab.internal/team/repo/-/merge_requests/7">repo/7</a>'
            " — Fix &lt;login&gt; redirect → @alice (by @dave)"
        ))
        self.assertEqual(self.tool_result("list_reviews", {}), (False, (
            "repo/7 https://gitlab.internal/team/repo/-/merge_requests/7 — Fix <login> redirect → @alice (by @dave)"
        )))

    def test_add_review_looks_up_missing_title(self):
        self.fetch_details.return_value = ReviewDetails("Fetched title", "Fetched body")

        self.tool_result("add_review", {"url": "https://github.com/owner/repo/pull/45"})
        self.tool_result("add_review", {"url": "https://github.com/owner/repo/pull/46", "description": "Agent body"})

        first, second = self.db.get_tasks(CHAT_ID)
        self.assertEqual((first.title, first.description), ("Fetched title", "Fetched body"))
        self.assertEqual((second.title, second.description), ("Fetched title", "Agent body"))
        self.assertIn("</a> — Fetched title (by @dave)", self.bot.send_message.await_args.args[1])

    def test_add_review_reports_errors_the_agent_can_fix(self):
        self.tool_result("add_review", {"url": "https://github.com/owner/repo/pull/45"})
        cases = (
            ({"url": "https://github.com/owner/repo/pull/45"}, "already exists in the queue"),
            ({"url": "https://github.com/owner/repo/issues/1"}, "Unsupported URL"),
            ({"url": "https://github.com/owner/repo/pull/46", "reviewers": ["not a name"]}, "Invalid reviewer"),
            ({"url": "https://github.com/owner/repo/pull/46", "note": "x" * 1001}, "Note is too long"),
            ({"url": "https://github.com/owner/repo/pull/46", "title": "x" * 256}, "Title is too long"),
        )
        for arguments, error in cases:
            with self.subTest(error=error):
                is_error, text = self.tool_result("add_review", arguments)
                self.assertTrue(is_error)
                self.assertIn(error, text)
        self.assertEqual(len(self.db.get_tasks(CHAT_ID)), 1)
        self.bot.send_message.assert_awaited_once()

    def test_add_review_keeps_task_when_announcement_fails(self):
        self.bot.send_message.side_effect = RuntimeError("Telegram is down")

        is_error, text = self.tool_result("add_review", {"url": "https://github.com/owner/repo/pull/45"})

        self.assertFalse(is_error)
        self.assertIn("announcement failed", text)
        self.assertEqual(len(self.db.get_tasks(CHAT_ID)), 1)

    def test_list_reviews(self):
        self.assertEqual(self.tool_result("list_reviews", {}), (False, "No reviews in the queue."))
        self.db.add_task(CHAT_ID, "repo/45", "https://github.com/owner/repo/pull/45", ["@alice"], "@dave")
        self.db.add_task(CHAT_ID + 1, "other/1", "https://github.com/owner/other/pull/1", [], "@erin")

        self.assertEqual(self.tool_result("list_reviews", {}),
                         (False, "repo/45 https://github.com/owner/repo/pull/45 → @alice (by @dave)"))

    def test_rejects_missing_or_unknown_tokens(self):
        for authorization in (None, "Bearer wrong", "Basic secret"):
            with self.subTest(authorization=authorization):
                headers = {key: value for key, value in HEADERS.items() if key != "Authorization"}
                if authorization:
                    headers["Authorization"] = authorization
                response = self.call_tool("add_review", {"url": "https://github.com/owner/repo/pull/45"}, headers)
                self.assertEqual(response.status_code, 401)
        self.assertEqual(self.db.get_tasks(CHAT_ID), [])


class ParseTokensTests(unittest.TestCase):
    def test_parses_names_and_optional_user_ids(self):
        self.assertEqual(mcp_server.parse_tokens(" a=@alice:42 , b=@bob, c==@carol"), {
            "a": mcp_server.Submitter("@alice", 42),
            "b": mcp_server.Submitter("@bob"),
            "c=": mcp_server.Submitter("@carol"),
        })

    def test_invalid_entries_do_not_echo_secrets(self):
        for spec in ("secret", "secret=", "secret=@alice:abc"):
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError) as error:
                    mcp_server.parse_tokens(spec)
                self.assertNotIn("secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
