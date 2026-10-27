"""MCP server that lets AI coding agents add reviews to a chat's work queue.

Runs beside the bot over streamable HTTP and shares its SQLite database. Telegram does not
deliver one bot's messages to another, so tools write to the queue directly and announce
the result in the group through the Bot API instead of sending `!wadd` text.
"""

import hmac
import logging
import os
import re
from dataclasses import dataclass
from html import escape as html_escape

import anyio
import uvicorn
from dotenv import load_dotenv
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from telegram import Bot
from telegram.constants import ParseMode

from database import Database
from presentation import review_listing
from review_urls import parse_review_url

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

NOTE_LIMIT = 1000
USERNAME_PATTERN = re.compile(r"@?(\w+)")


@dataclass(frozen=True)
class Submitter:
    name: str
    user_id: int | None = None


def parse_tokens(spec: str) -> dict[str, Submitter]:
    """Parse `token=@name[:telegram_user_id]` entries separated by commas."""
    tokens = {}
    entries = filter(None, (part.strip() for part in spec.split(",")))
    for position, entry in enumerate(entries, start=1):
        # Split on the last "=" so tokens may contain "=" (names cannot).
        token, separator, identity = entry.rpartition("=")
        name, _, user_id = identity.partition(":")
        if not separator or not token or not name or not (user_id or "0").lstrip("-").isdigit():
            # Never echo the entry: it contains a secret.
            raise ValueError(f"Invalid MCP_TOKENS entry #{position}; expected token=@name[:telegram_user_id]")
        tokens[token] = Submitter(name, int(user_id) if user_id else None)
    return tokens


def parse_reviewers(reviewers: list[str]) -> list[str]:
    """Accept `alice` or `@alice`, matching the usernames `!wadd` accepts."""
    parsed = []
    for reviewer in reviewers:
        match = USERNAME_PATTERN.fullmatch(reviewer.strip())
        if match is None:
            raise ToolError(f"Invalid reviewer {reviewer!r}; use a Telegram username such as @alice")
        if f"@{match.group(1)}" not in parsed:
            parsed.append(f"@{match.group(1)}")
    return parsed


class BearerAuth:
    """Reject requests without a known token and expose its submitter to tools."""

    def __init__(self, app: ASGIApp, tokens: dict[str, Submitter]):
        self.app = app
        self.tokens = tokens

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        scheme, _, token = headers.get(b"authorization", b"").decode("latin-1").partition(" ")
        submitter = next(
            (submitter for known, submitter in self.tokens.items()
             if scheme.lower() == "bearer" and hmac.compare_digest(known.encode(), token.strip().encode())),
            None
        )
        if submitter is None:
            await JSONResponse({"error": "invalid or missing bearer token"}, status_code=401)(scope, receive, send)
            return
        scope.setdefault("state", {})["submitter"] = submitter
        await self.app(scope, receive, send)


def create_server(db: Database, bot_token: str, chat_id: int) -> MCPServer:
    server = MCPServer(
        "work-queue",
        instructions=(
            "Manages the team's Telegram code review queue. Use add_review when the user asks to "
            "queue a PR/MR for review, request a review, or run !wadd. Use list_reviews to see the queue."
        ),
    )

    bot = Bot(bot_token)

    async def announce(text: str) -> None:
        await bot.initialize()  # No-op after the first call.
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    @server.tool()
    async def add_review(url: str, ctx: Context, reviewers: list[str] | None = None, note: str | None = None) -> str:
        """Add a GitHub pull request or GitLab merge request to the Telegram review queue, like `!wadd`.

        Args:
            url: Full PR/MR URL, e.g. https://github.com/owner/repo/pull/45
            reviewers: Telegram usernames to assign and mention, e.g. ["@alice", "@bob"]
            note: Optional message for reviewers, such as what changed or what to focus on
        """
        submitter: Submitter = ctx.request_context.request.state.submitter
        parsed = parse_review_url(url)
        if parsed is None:
            raise ToolError(
                "Unsupported URL. Use a GitLab merge request (http://host/group/project/-/merge_requests/N) "
                "or GitHub pull request (https://github.com/owner/repo/pull/N)."
            )
        task_id = parsed[0]
        assignees = parse_reviewers(reviewers or [])
        note = (note or "").strip()
        if len(note) > NOTE_LIMIT:
            raise ToolError(f"Note is too long ({len(note)} characters); the limit is {NOTE_LIMIT}.")

        # SQLite may wait on the bot's write lock; keep the event loop free meanwhile.
        seq_num = await anyio.to_thread.run_sync(
            db.add_task, chat_id, task_id, url, assignees, submitter.name, submitter.user_id
        )
        if seq_num is None:
            raise ToolError(f"PR/MR reference {task_id} already exists in the queue.")
        logger.info(f"Added task {task_id} in chat {chat_id} via MCP for {submitter.name}: {url} -> "
                    f"{', '.join(assignees) or 'unassigned'}")

        message = review_listing(task_id, url, assignees, submitter.name)
        if note:
            message += f"\n<blockquote>{html_escape(note)}</blockquote>"
        result = f"Added {task_id} to the review queue"
        result += f", assigned to {', '.join(assignees)}." if assignees else " (unassigned)."
        try:
            await announce(message)
        except Exception:
            logger.exception("Could not announce %s in chat %s", task_id, chat_id)
            return result + " The review is queued, but the Telegram announcement failed; reviewers were not notified."
        return result

    @server.tool()
    def list_reviews() -> str:
        """List the reviews currently waiting in the Telegram review queue, like `!w`."""
        tasks = db.get_tasks(chat_id)
        if not tasks:
            return "No reviews in the queue."
        return "\n".join(
            f"{task.task_id} {task.url}"
            + (f" → {', '.join(task.assignees)}" if task.assignees else "")
            + f" (by {task.created_by})"
            for task in tasks
        )

    return server


def build_app(server: MCPServer, tokens: dict[str, Submitter]) -> ASGIApp:
    # Stateless JSON responses keep each tool call a single authenticated HTTP request. Requiring
    # a bearer token already defeats DNS rebinding, and Host checks would reject reverse proxies.
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return BearerAuth(app, tokens)


def main() -> None:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("MCP_CHAT_ID")
    tokens = parse_tokens(os.getenv("MCP_TOKENS", ""))
    if not bot_token or not chat_id or not tokens:
        raise ValueError("TELEGRAM_BOT_TOKEN, MCP_CHAT_ID, and MCP_TOKENS are required for the MCP server")

    host = os.getenv("MCP_HOST", "127.0.0.1")
    port = int(os.getenv("MCP_PORT", "8090"))
    server = create_server(Database(), bot_token, int(chat_id))
    app = build_app(server, tokens)
    logger.info(f"Starting MCP server on {host}:{port} for chat {chat_id} with {len(tokens)} token(s)")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
