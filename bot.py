import asyncio
import os
import re
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Callable, Optional
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError, TimedOut
from html import escape as html_escape

from database import Database, FinishedReview, Task
from gamification import CheerWriter, approval_cheer, bounce_cheer, queue_wait
from leaderboard import Identities, build_leaderboard, format_leaderboard, reporting_week
from presentation import FINISHED_VISIBLE_DAYS, finished_listing, grouped_by_chat, my_reviews, review_link, split_messages, task_listing
from review_urls import normalize_review_number, parse_review_url
from scheduler import add_reminder_job, remove_reminder_job, setup_scheduler
from apscheduler.triggers.cron import CronTrigger

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize database
db = Database()
# Writes the private messages reviewers get after !wdone and !wbounce
cheer_writer = CheerWriter.from_environment()
# Cheers are sent in the background; keep references so the tasks aren't garbage-collected.
cheer_tasks: set[asyncio.Task] = set()
# Set once a Bot API server posts an ephemeral message publicly. Cheers are unrequested, so they stop
# until restart instead of flashing a public message (and a notification) after every review command.
ephemeral_unsupported = False

# Regex patterns for commands
WADD_PREFIX = re.compile(r"^!wadd\b", re.IGNORECASE)
W_PATTERN = re.compile(r"^!w$", re.IGNORECASE)
WME_PATTERN = re.compile(r"^!wme$", re.IGNORECASE)
WMERGED_PATTERN = re.compile(r"^!wmerged\s+(.+)$", re.IGNORECASE | re.DOTALL)
WMERGED_PREFIX = re.compile(r"^!wmerged\b", re.IGNORECASE)
WCLEAR_PATTERN = re.compile(r"^!wclear\s+(.+)$", re.IGNORECASE | re.DOTALL)
WCLEAR_PREFIX = re.compile(r"^!wclear\b", re.IGNORECASE)
WDONE_PATTERN = re.compile(r"^!wdone\s+(.+)$", re.IGNORECASE | re.DOTALL)
WDONE_PREFIX = re.compile(r"^!wdone\b", re.IGNORECASE)
WBOUNCE_PATTERN = re.compile(r"^!wbounce\s+(.+)$", re.IGNORECASE | re.DOTALL)
WBOUNCE_PREFIX = re.compile(r"^!wbounce\b", re.IGNORECASE)
WHELP_PATTERN = re.compile(r"^!whelp$", re.IGNORECASE)
WREMINDER_STATUS_PATTERN = re.compile(r"^!wreminder$", re.IGNORECASE)
WREMINDER_SET_PATTERN = re.compile(r"^!wreminder-set\s+(.+)$", re.IGNORECASE)
WREMINDER_OFF_PATTERN = re.compile(r"^!wreminder-off$", re.IGNORECASE)
WREMINDER_REMOVE_PATTERN = re.compile(r"^!wreminder-remove$", re.IGNORECASE)
WASSIGN_PREFIX = re.compile(r"^!wassign\b", re.IGNORECASE)
WLEADERBOARD_PATTERN = re.compile(r"^!wleaderboard(?:\s+(last))?$", re.IGNORECASE)
WLEADERBOARD_OFF_PATTERN = re.compile(r"^!wleaderboard-off$", re.IGNORECASE)
WLEADERBOARD_ON_PATTERN = re.compile(r"^!wleaderboard-on$", re.IGNORECASE)
WLEADERBOARD_PREFIX = re.compile(r"^!wleaderboard\b", re.IGNORECASE)

QUALIFIED_REFERENCE_PATTERN = re.compile(r"[^/\s]+/[0-9]+")
NUMBER_PATTERN = re.compile(r"[0-9]+")
LEGACY_REFERENCE_PATTERN = re.compile(r"#[0-9]+")
MENTION_PATTERN = re.compile(r"@\w+")
MENTIONS_PATTERN = re.compile(r"(?:@\w+)+")
# Commas separate links and usernames, but a comma inside a URL's query string stays part of it.
WADD_SEPARATOR_PATTERN = re.compile(r"\s+|,+(?=\s|$|@|https?://)", re.IGNORECASE)
# PR/MR number ranges such as 55-100, 55..100, or 55...100. Nothing may separate the numbers from the
# dash, so a bulleted list ("12" then "- 15" on the next line) or a stray "5 -8" is never a range.
RANGE_PATTERN = re.compile(r"([0-9]+)(?:-|\.{2,}|…)([0-9]+)")
# Several references or ranges separated by spaces, commas, or new lines. PR/MR paths never contain
# commas, so a comma after a URL's path starts the next reference (…/pull/5,6), but a comma inside its
# query string or fragment stays part of it, as with !wadd.
REFERENCE_LIST_PATTERN = re.compile(
    rf"{RANGE_PATTERN.pattern}|https?://[^\s,?#]+(?:[?#]\S*?(?=,*(?:\s|$)|,+https?://))?|[^\s,]+",
    re.IGNORECASE)

MEMBER_STATUSES = (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER)
# Telegram rate-limits bursts of requests, so check a user's groups a few at a time.
MEMBERSHIP_CHECK_CONCURRENCY = 5

WMERGED_USAGE = (
    "Usage: <code>!wmerged &lt;PR/MR references&gt;</code>\n"
    "Mark your approved PRs/MRs as merged, so they leave <code>!wme</code>.\n"
    "Examples: <code>!wmerged repo/123</code>, <code>!wmerged 123</code>, <code>!wmerged 55 67 88</code>, "
    "<code>!wmerged 55-60</code>, or <code>!wmerged https://github.com/owner/repo/pull/123</code>"
)
WDONE_USAGE = (
    "Usage: <code>!wdone &lt;PR/MR references&gt;</code>\n"
    "Examples: <code>!wdone repo/123</code>, <code>!wdone 123</code>, <code>!wdone 1 2 3 4</code>, "
    "<code>!wdone 1...4</code>, or <code>!wdone https://github.com/owner/repo/pull/123</code>"
)
WBOUNCE_USAGE = (
    "Usage: <code>!wbounce &lt;PR/MR references&gt;</code>\n"
    "Remove tasks with a Changes required comment.\n"
    "Examples: <code>!wbounce repo/123</code>, <code>!wbounce 123</code>, <code>!wbounce 1 2 3</code>, "
    "<code>!wbounce 1-3</code>, or <code>!wbounce https://github.com/owner/repo/pull/123</code>"
)
WASSIGN_USAGE = (
    "Usage: <code>!wassign &lt;PR/MR references&gt; @username [...]</code>\n"
    "Examples:\n"
    "• <code>!wassign repo/123 @alice</code>\n"
    "• <code>!wassign 123 @alice @bob</code>\n"
    "• <code>!wassign 55 67 88 @alice</code> or <code>!wassign 55-60 @alice</code>\n"
    "• <code>!wassign 55 67 @alice</code>, then <code>88 @bob</code> on the next line\n"
    "• <code>!wassign https://github.com/owner/repo/pull/123 @alice @bob @charlie</code>"
)
# Ranges in one command on the shared queue (!wdone, !wbounce, !wassign) can cover at most this many
# reviews in total, so a typo such as 12-120 for "12 120" can't empty the queue.
MAX_QUEUE_RANGE = 20
REFERENCE_USAGE = "Use a PR/MR reference such as <code>repo/120</code>, its URL, or an unambiguous PR/MR number. Use <code>!w</code> to see current references."


WADD_USAGE = (
    "Usage: <code>!wadd &lt;URL&gt; [@username ...]</code>\n"
    "Examples:\n"
    "• <code>!wadd http://gitlab.example.com/group/repo/-/merge_requests/123</code>\n"
    "• <code>!wadd http://gitlab.example.com/group/repo/-/merge_requests/123 @alice @bob</code>\n"
    "• <code>!wadd &lt;URL 1&gt; &lt;URL 2&gt; @alice</code> (both assigned to @alice)\n"
    "• One link per line, each with its own <code>@username</code>s"
)
UNSUPPORTED_URL = (
    "Must be a GitLab merge request or GitHub pull request.\n"
    "Supported formats:\n"
    "• <code>http://host/group/project/-/merge_requests/N</code>\n"
    "• <code>https://github.com/owner/repo/pull/N</code>"
)


def parse_wadd(text: str) -> tuple[list[tuple[str, str, list[str]]], str | None]:
    """Split !wadd arguments into (task_id, url, assignees), or explain what is wrong.

    @mentions assign the links before them, back to the previous @mentions or the start of the
    line, so `!wadd url1 url2 @alice` assigns both links and each line of a message stands alone.
    A line of only @mentions assigns the line before it.
    """
    reviews: list[tuple[str, str, list[str]]] = []
    pending: list[list[str]] = []
    for line in WADD_PREFIX.sub("", text, count=1).splitlines():
        line_start = True
        for token in WADD_SEPARATOR_PATTERN.split(line.strip()):
            if not token:
                continue
            if token.startswith("@"):
                if not MENTION_PATTERN.fullmatch(token):
                    return [], f"Invalid username format. Use <code>@username</code> for each assignee (got: {html_escape(token)})"
                if not pending:
                    return [], "Missing URL. Put a GitLab MR or GitHub PR link before the username(s) it is assigned to."
                for assignees in pending:
                    if token not in assignees:
                        assignees.append(token)
            elif token.lower().startswith(("http://", "https://")):
                task_id = extract_task_id(token)
                if task_id is None:
                    return [], f"Unsupported URL <code>{html_escape(token)}</code>. {UNSUPPORTED_URL}"
                # Links after @mentions or on a new line start a new group.
                if line_start or (pending and pending[-1]):
                    pending = []
                reviews.append((task_id, token, []))
                pending.append(reviews[-1][2])
            else:
                return [], f"Invalid URL <code>{html_escape(token)}</code>. Links must start with http:// or https://"
            line_start = False
    if not reviews:
        return [], f"Missing URL.\n{WADD_USAGE}"
    return reviews, None


def extract_task_id(url: str) -> str | None:
    parsed = parse_review_url(url)
    return parsed[0] if parsed else None


def review_number(review) -> str | None:
    """The normalized PR/MR number of anything with a task_id, or None if it has none."""
    number = review.task_id.rsplit("/", 1)[-1]
    return normalize_review_number(number) if NUMBER_PATTERN.fullmatch(number) else None


def matching_reviews(reviews: list, task_ref: str) -> list | None:
    """Reviews (anything with task_id and url) matching a PR/MR reference, or None if it isn't one."""
    if NUMBER_PATTERN.fullmatch(task_ref):
        number = normalize_review_number(task_ref)
        return [review for review in reviews if review_number(review) == number]
    if QUALIFIED_REFERENCE_PATTERN.fullmatch(task_ref):
        return [review for review in reviews if review.task_id == task_ref]
    parsed = parse_review_url(task_ref)
    if parsed is None:
        return None
    return [review for review in reviews if (saved := parse_review_url(review.url)) and saved[1] == parsed[1]]


def number_key(number: str) -> tuple[int, str]:
    """Order normalized PR/MR numbers numerically without converting arbitrarily long digit strings."""
    return len(number), number


def reviews_in_range(reviews: list, low: str, high: str) -> list:
    low, high = sorted((number_key(normalize_review_number(low)), number_key(normalize_review_number(high))))
    return [review for review in reviews
            if (number := review_number(review)) is not None and low <= number_key(number) <= high]


def review_identity(review) -> tuple:
    """The PR/MR a review points at. Links saved before URL parsing was tightened may not parse; each
    such link only stands for itself."""
    parsed = parse_review_url(review.url)
    return parsed[1] if parsed else ("", review.url, "")


def split_references(args: str) -> list[str]:
    """Split command arguments into PR/MR references and number ranges."""
    return [match.group(0) for match in REFERENCE_LIST_PATTERN.finditer(args)]


def invalid_reference_error(references: list[str], usage: str) -> str | None:
    """Explain the first reference that isn't a PR/MR reference or number range, if any."""
    for reference in references:
        if LEGACY_REFERENCE_PATTERN.fullmatch(reference):
            return f"Queue numbers such as <code>{html_escape(reference)}</code> are no longer supported.{usage}"
        if not RANGE_PATTERN.fullmatch(reference) and matching_reviews([], reference) is None:
            return f"Invalid PR/MR reference <code>{html_escape(reference)}</code>.{usage}"
    return None


@dataclass
class Resolution:
    matched: dict = field(default_factory=dict)  # Matched reviews by key, each once, in order.
    missing: list[str] = field(default_factory=list)
    ambiguous: list[tuple[str, list]] = field(default_factory=list)
    ranges: list[tuple[str, list]] = field(default_factory=list)  # Each range with the reviews it matched.


def number_clashes(reviews: list) -> list:
    """Reviews sharing a PR/MR number with a different PR/MR, such as api/5 and web/5."""
    by_number = {}
    for review in reviews:
        by_number.setdefault(review_number(review), []).append(review)
    return [review for same_number in by_number.values()
            if len({review_identity(other) for other in same_number}) > 1 for review in same_number]


def resolve_references(reviews: list, references: list[str], key: Callable, shared_queue: bool = False) -> Resolution:
    """Match references and number ranges against reviews.

    A reference matching several different PRs/MRs is ambiguous; the same PR/MR can appear more than
    once (e.g. finished in several chats), but repos on different hosts or paths can share a number.
    A range takes every PR/MR numbered within it. On the shared queue, where a typo would affect
    everyone, a range is held to the same rule as a single number (see also range_limit_error).
    """
    resolution = Resolution()
    for reference in references:
        if range_match := RANGE_PATTERN.fullmatch(reference):
            matches = reviews_in_range(reviews, range_match.group(1), range_match.group(2))
            resolution.ranges.append((reference, matches))
            if shared_queue and (clashes := number_clashes(matches)):
                resolution.ambiguous.append((reference, clashes))
                continue
        else:
            matches = matching_reviews(reviews, reference)
            if len({review_identity(review) for review in matches}) > 1:
                resolution.ambiguous.append((reference, matches))
                continue
        if not matches:
            resolution.missing.append(reference)
        resolution.matched.update((key(review), review) for review in matches)
    return resolution


def references_list(references: list[str]) -> str:
    return ", ".join(f"<code>{html_escape(reference)}</code>" for reference in references)


def links(reviews: list) -> str:
    return ", ".join(review_link(review.task_id, review.url) for review in reviews)


def ambiguity_blocks(ambiguous: list[tuple[str, list]], what: str) -> list[str]:
    return [f"<code>{html_escape(reference)}</code> includes the same number in several repos: {links(matches)}. "
            "List them with qualified PR/MR references instead."
            if RANGE_PATTERN.fullmatch(reference) else
            f"<code>{html_escape(reference)}</code> matches several {what}: {links(matches)}. Use a qualified PR/MR reference."
            for reference, matches in ambiguous]


def range_limit_error(resolutions: list[Resolution]) -> str | None:
    """Explain why a queue command's ranges cover too many reviews in total, if they do."""
    ranges = [item for resolution in resolutions for item in resolution.ranges]
    covered = len({task.id for _, matches in ranges for task in matches})
    if covered <= MAX_QUEUE_RANGE:
        return None
    subject = (f"<code>{html_escape(ranges[0][0])}</code> covers" if len(ranges) == 1
               else f"{references_list([reference for reference, _ in ranges])} cover")
    return (f"{subject} {covered} reviews. Ranges can cover at most {MAX_QUEUE_RANGE} reviews per command, "
            "so nothing was changed.")


def queue_problem_blocks(resolution: Resolution, gone: list[Task]) -> list[str]:
    """Report references that matched nothing or several reviews, and tasks removed in the meantime."""
    blocks = []
    if resolution.missing:
        noun = "PR/MR reference" if len(resolution.missing) == 1 else "PR/MR references"
        blocks.append(f"{noun} {references_list(resolution.missing)} not found in this chat. "
                      "Use <code>!w</code> to see current references.")
    blocks += ambiguity_blocks(resolution.ambiguous, "reviews")
    if gone:
        blocks.append(f"Already removed from the queue: {links(gone)}.")
    return blocks


async def resolve_queue_references(update: Update, chat_id: int, groups: list[list[str]]) -> list[Resolution] | None:
    """Match each group of references against this chat's queue, or reply with why the command can't run."""
    if error := invalid_reference_error([reference for group in groups for reference in group], f" {REFERENCE_USAGE}"):
        await update.message.reply_text(error, parse_mode=ParseMode.HTML)
        return None
    tasks = db.get_tasks(chat_id)
    resolutions = [resolve_references(tasks, group, key=lambda task: task.id, shared_queue=True) for group in groups]
    if error := range_limit_error(resolutions):
        await update.message.reply_text(error, parse_mode=ParseMode.HTML)
        return None
    return resolutions


async def reply_blocks(update: Update, blocks: list[str]) -> None:
    for text in split_messages(blocks):
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


def sender_identity(update: Update) -> tuple[int | None, str]:
    """Anonymous chat senders and Telegram's placeholder bot are not individuals."""
    user = update.effective_user
    if not user or getattr(update.message, "sender_chat", None) or getattr(user, "is_bot", False):
        return None, "Unknown"
    name = f"@{user.username}" if user.username else user.first_name
    return getattr(user, "id", None), name or "Unknown"


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming messages and route to appropriate command handlers."""
    if not update.message or not update.message.text:
        return
    
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    
    created_by_id, created_by = sender_identity(update)
    
    # Check for !wadd command
    if WADD_PREFIX.match(text):
        reviews, error = parse_wadd(text)
        if error:
            await update.message.reply_text(error, parse_mode=ParseMode.HTML)
            return
        await handle_wadd(update, chat_id, reviews, created_by, created_by_id)
        return
    
    # Check for !w command
    if W_PATTERN.match(text):
        await handle_w(update, chat_id)
        return

    if WME_PATTERN.match(text):
        await handle_wme(update, context, chat_id)
        return

    wmerged_match = WMERGED_PATTERN.match(text)
    if wmerged_match:
        await handle_wmerged(update, context, chat_id, wmerged_match.group(1).strip())
        return
    elif WMERGED_PREFIX.match(text):
        await update.message.reply_text(WMERGED_USAGE, parse_mode=ParseMode.HTML)
        return

    wclear_match = WCLEAR_PATTERN.match(text)
    if wclear_match:
        await handle_wclear(update, context, chat_id, wclear_match.group(1).strip())
        return
    elif WCLEAR_PREFIX.match(text):
        await update.message.reply_text(WCLEAR_USAGE, parse_mode=ParseMode.HTML)
        return
    
    # Check for !wdone command
    wdone_match = WDONE_PATTERN.match(text)
    if wdone_match:
        await handle_wdone(update, context, chat_id, wdone_match.group(1).strip())
        return
    elif WDONE_PREFIX.match(text):
        await update.message.reply_text(WDONE_USAGE, parse_mode=ParseMode.HTML)
        return
    
    # Check for !wbounce command
    wbounce_match = WBOUNCE_PATTERN.match(text)
    if wbounce_match:
        await handle_wbounce(update, context, chat_id, wbounce_match.group(1).strip())
        return
    elif WBOUNCE_PREFIX.match(text):
        await update.message.reply_text(WBOUNCE_USAGE, parse_mode=ParseMode.HTML)
        return

    # Check for !whelp command
    if WHELP_PATTERN.match(text):
        await handle_whelp(update)
        return

    if WLEADERBOARD_OFF_PATTERN.match(text):
        await handle_wleaderboard_toggle(update, chat_id, enabled=False)
        return
    if WLEADERBOARD_ON_PATTERN.match(text):
        await handle_wleaderboard_toggle(update, chat_id, enabled=True)
        return
    leaderboard_match = WLEADERBOARD_PATTERN.match(text)
    if leaderboard_match:
        await handle_wleaderboard(update, chat_id, previous=bool(leaderboard_match.group(1)))
        return
    if WLEADERBOARD_PREFIX.match(text):
        await update.message.reply_text(
            "Usage: <code>!wleaderboard</code> for this week or <code>!wleaderboard last</code> for last week.\n"
            "Use <code>!wleaderboard-off</code> / <code>!wleaderboard-on</code> to toggle automatic recaps.",
            parse_mode=ParseMode.HTML
        )
        return
    
    # Check for !wreminder command
    if WREMINDER_STATUS_PATTERN.match(text):
        await handle_wreminder_status(update, chat_id)
        return
    
    # Check for !wreminder-set command
    wreminder_set_match = WREMINDER_SET_PATTERN.match(text)
    if wreminder_set_match:
        cron_expression = wreminder_set_match.group(1).strip()
        await handle_wreminder_set(update, context, chat_id, cron_expression)
        return
    
    # Check for !wreminder-off command
    if WREMINDER_OFF_PATTERN.match(text):
        await handle_wreminder_off(update, chat_id)
        return
    
    # Check for !wreminder-remove command
    if WREMINDER_REMOVE_PATTERN.match(text):
        await handle_wreminder_remove(update, chat_id)
        return
    
    # Check for !wassign command
    if WASSIGN_PREFIX.match(text):
        groups = parse_wassign(text[len("!wassign"):])
        if groups is not None:
            await handle_wassign(update, chat_id, groups)
            return
        parts = text.split()
        if len(parts) > 1 and LEGACY_REFERENCE_PATTERN.fullmatch(parts[1]):
            await update.message.reply_text(invalid_reference_error(parts[1:2], f" {REFERENCE_USAGE}"), parse_mode=ParseMode.HTML)
            return
        await update.message.reply_text(WASSIGN_USAGE, parse_mode=ParseMode.HTML)
        return


async def handle_wadd(update: Update, chat_id: int, reviews: list[tuple[str, str, list[str]]], created_by: str,
                      created_by_id: int | None = None) -> None:
    """Handle !wadd command - add tasks from one or more MR/PR links."""
    added, existing = [], []
    for task_id, url, assignees in reviews:
        if db.add_task(chat_id, task_id, url, assignees, created_by, created_by_id) is None:
            existing.append(task_id)
            continue
        entry = review_link(task_id, url)
        if assignees:
            entry += " → " + ", ".join(html_escape(a) for a in assignees)
        added.append(entry)
        logger.info(f"Added task {task_id} in chat {chat_id}: {url} -> {', '.join(assignees) or 'unassigned'}")

    if len(reviews) == 1:
        response = added or [f"PR/MR reference {html_escape(existing[0])} already exists in the queue."]
    else:
        total = f" of {len(reviews)}" if existing else ""
        response = [f"<b>Added {len(added)}{total}</b>", *added] if added else []
        if existing:
            response.append("Already in the queue: " + ", ".join(html_escape(task_id) for task_id in existing))
    for message in split_messages(response):
        await update.message.reply_text(message, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def handle_w(update: Update, chat_id: int) -> None:
    """Handle !w command - list all tasks."""
    tasks = db.get_tasks(chat_id)
    
    if not tasks:
        await update.message.reply_text("No tasks in the queue.")
        return
    
    response = "\n".join(task_listing(task) for task in tasks)
    await update.message.reply_text(response, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def visible_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int,
                          with_assigned: bool = True) -> tuple[list[Task], list[FinishedReview], dict[int, str] | None, int]:
    """The sender's assigned reviews and approved or bounced PRs/MRs as !wme shows them, with the
    titles of the chats they come from (None in a group) and how many chats couldn't be checked."""
    username = update.effective_user.username
    name = f"@{username}" if username else None
    since = datetime.now(timezone.utc) - timedelta(days=FINISHED_VISIBLE_DAYS)
    if update.effective_chat.type != ChatType.PRIVATE:
        assigned = db.get_tasks_assigned_to(name, chat_id) if name and with_assigned else []
        return assigned, db.get_finished_reviews(user_id, name, since, chat_id), None, 0

    # A direct message has no queue of its own, so look across every chat, but only show groups
    # the sender is still in: a matching @username alone could be a typo or a reclaimed name.
    assigned = db.get_tasks_assigned_to(name) if name and with_assigned else []
    finished = db.get_finished_reviews(user_id, name, since)
    other_chats = {item.chat_id for item in [*assigned, *finished]} - {chat_id}
    chat_titles, unchecked = await member_chat_titles(context, other_chats, user_id)
    # Reviews queued in this direct message belong to the sender already.
    chat_titles[chat_id] = "This chat"
    assigned = [task for task in assigned if task.chat_id in chat_titles]
    finished = [review for review in finished if review.chat_id in chat_titles]
    return assigned, finished, chat_titles, unchecked


async def handle_wme(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Handle !wme command - privately show reviews assigned to the sender and their approved or bounced PRs/MRs."""
    user_id, _ = sender_identity(update)
    if user_id is None:
        # Private replies go to one user, so they need an identified sender.
        await update.message.reply_text("<code>!wme</code> needs to know who you are. Send it from your own account, not anonymously.",
                                        parse_mode=ParseMode.HTML)
        return

    assigned, finished, chat_titles, unchecked = await visible_reviews(update, context, chat_id, user_id)
    await reply_privately(update, context, my_reviews(assigned, finished, has_username=bool(update.effective_user.username),
                                                      chat_titles=chat_titles, unchecked_chats=unchecked))


async def handle_wmerged(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, args: str) -> None:
    """Handle !wmerged command - take the sender's approved PRs/MRs off their !wme list."""
    user_id, name = sender_identity(update)
    if user_id is None:
        await update.message.reply_text("<code>!wmerged</code> needs to know who you are. Send it from your own account, not anonymously.",
                                        parse_mode=ParseMode.HTML)
        return
    references = split_references(args)
    if not references:
        await update.message.reply_text(WMERGED_USAGE, parse_mode=ParseMode.HTML)
        return
    if error := invalid_reference_error(references, " Use a reference such as <code>repo/120</code>, its URL, or its number."):
        await update.message.reply_text(error, parse_mode=ParseMode.HTML)
        return

    username = update.effective_user.username
    since = datetime.now(timezone.utc) - timedelta(days=FINISHED_VISIBLE_DAYS)
    approved = [review for review in db.get_finished_reviews(user_id, f"@{username}" if username else None, since, chat_id)
                if review.outcome == "done"]
    resolution = resolve_references(approved, references, key=lambda review: review.activity_id)
    merged = list(resolution.matched.values())
    db.mark_merged([review.activity_id for review in merged], name, user_id)

    blocks = []
    if len(merged) == 1:
        blocks.append(f"✅ Marked {review_link(merged[0].task_id, merged[0].url)} as merged.")
    elif merged:
        blocks.append(f"✅ Marked {len(merged)} as merged:")
        blocks += [f"• {review_link(review.task_id, review.url)}" for review in merged]
    if resolution.missing:
        verb = "isn't one of" if len(resolution.missing) == 1 else "aren't among"
        blocks.append(f"{references_list(resolution.missing)} {verb} your approved PRs/MRs in this chat. "
                      "Use <code>!wme</code> to see them.")
    if blocks:
        await reply_blocks(update, blocks)
    if resolution.ambiguous:
        # The alternatives list the sender's approved PRs/MRs, which only they should see.
        # Once some were marked merged, sending the command again wouldn't repeat this reply.
        await reply_privately(update, context, split_messages(ambiguity_blocks(resolution.ambiguous, "of your approved reviews")),
                              public_outcome="Some references match several of your approved reviews." if merged else None)
    logger.info(f"Marked {len(merged)} reviews as merged in chat {chat_id}")


WCLEAR_USAGE = (
    "Usage: <code>!wclear &lt;PR/MR references&gt;</code> or <code>!wclear all</code>\n"
    "Clear your approved or bounced PRs/MRs from <code>!wme</code>.\n"
    "Examples: <code>!wclear 55 67 88</code>, <code>!wclear 55-100</code>, <code>!wclear repo/55, repo/67</code>, or <code>!wclear all</code>"
)


async def handle_wclear(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, args: str) -> None:
    """Handle !wclear command - take approved or bounced PRs/MRs off the sender's !wme list and privately list them."""
    user_id, name = sender_identity(update)
    if user_id is None:
        await update.message.reply_text("<code>!wclear</code> needs to know who you are. Send it from your own account, not anonymously.",
                                        parse_mode=ParseMode.HTML)
        return

    references = split_references(args)
    if not references:
        await update.message.reply_text(WCLEAR_USAGE, parse_mode=ParseMode.HTML)
        return
    clear_all = [reference.lower() for reference in references] == ["all"]
    if not clear_all and (error := invalid_reference_error(references, f"\n{WCLEAR_USAGE}")):
        await update.message.reply_text(error, parse_mode=ParseMode.HTML)
        return

    _, finished, chat_titles, unchecked = await visible_reviews(update, context, chat_id, user_id, with_assigned=False)
    if clear_all:
        resolution = Resolution(matched={review.activity_id: review for review in finished})
    else:
        resolution = resolve_references(finished, references, key=lambda review: review.activity_id)
    cleared, missing = resolution.matched, resolution.missing

    db.clear_finished_reviews(list(cleared), name, user_id)
    blocks = []
    if cleared:
        blocks.append(f"🧹 <b>Cleared {len(cleared)} from your <code>!wme</code> list</b>")
        blocks += grouped_by_chat(list(cleared.values()), finished_listing, chat_titles)
    elif clear_all or not (missing or resolution.ambiguous):
        blocks.append("Nothing to clear: you have no approved or bounced PRs/MRs here.")
    if missing:
        blocks.append(
            "Not among your approved or bounced PRs/MRs: "
            + references_list(missing)
            + ". Finish reviews assigned to you with <code>!wdone</code> or <code>!wbounce</code>."
        )
    blocks += ambiguity_blocks(resolution.ambiguous, "of your PRs/MRs")
    if unchecked:
        groups = "1 of your groups" if unchecked == 1 else f"{unchecked} of your groups"
        blocks.append(f"⚠️ I couldn't check {groups} right now, so nothing there was cleared.")
    # The clear has already happened, so a failed private reply still says how many were cleared.
    outcome = (f"Cleared {len(cleared)} from your <code>!wme</code> list, but I couldn't show which ones privately."
               if cleared else "Nothing was cleared.")
    delivered = await reply_privately(update, context, split_messages(blocks), public_outcome=outcome)
    logger.info(f"Cleared {len(cleared)} finished reviews for user {user_id} in chat {chat_id}"
                + ("" if delivered else "; the private reply was not delivered"))


async def member_chat_titles(context: ContextTypes.DEFAULT_TYPE, chat_ids: set[int],
                             user_id: int) -> tuple[dict[int, str], int]:
    """Return the titles of the chats the user currently belongs to, and how many couldn't be checked."""
    limit = asyncio.Semaphore(MEMBERSHIP_CHECK_CONCURRENCY)

    async def check(chat_id: int) -> bool | None:
        async with limit:
            try:
                member = await context.bot.get_chat_member(chat_id, user_id)
            except (BadRequest, Forbidden):
                # The user or the bot is no longer in the chat, or the chat is gone.
                return False
            except TelegramError as error:
                logger.warning("Could not check membership of chat %s: %r", chat_id, error)
                return None
            return member.status in MEMBER_STATUSES or (
                member.status == ChatMemberStatus.RESTRICTED and getattr(member, "is_member", False)
            )

    async def title(chat_id: int) -> str:
        async with limit:
            try:
                return (await context.bot.get_chat(chat_id)).title or f"Chat {chat_id}"
            except TelegramError:
                return f"Chat {chat_id}"

    chat_ids = list(chat_ids)
    results = dict(zip(chat_ids, await asyncio.gather(*(check(chat_id) for chat_id in chat_ids))))
    member_chats = [chat_id for chat_id in chat_ids if results[chat_id]]
    titles = dict(zip(member_chats, await asyncio.gather(*(title(chat_id) for chat_id in member_chats))))
    return titles, sum(1 for result in results.values() if result is None)


async def reply_privately(update: Update, context: ContextTypes.DEFAULT_TYPE, texts: list[str],
                          public_outcome: str | None = None, quiet: bool = False) -> bool:
    """Reply so only the sender sees it; in private chats a normal reply is already private.

    Commands that change data pass public_outcome, a summary without any PR/MR links that is posted
    publicly if the private reply fails, since sending such a command again can't recover the reply.
    Optional extras pass quiet, so a failed private reply is only logged instead of explained in the chat,
    unless it was posted publicly and couldn't be deleted.
    Returns whether the whole reply was delivered privately.
    """
    global ephemeral_unsupported
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        for text in texts:
            await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return True

    # Ephemeral messages (Bot API 10.2+) are not wrapped by python-telegram-bot yet. Admin bots
    # may send them at any time; Telegram does not guarantee delivery to offline users.
    for index, text in enumerate(texts):
        prefix = ("Only part of my reply reached you. " if index else "") + (f"{public_outcome} " if public_outcome else "")
        try:
            sent = await context.bot.send_message(
                chat.id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                message_thread_id=update.message.message_thread_id if update.message.is_topic_message else None,
                api_kwargs={"ephemeral_message_parameters": {"receiver_user_id": update.effective_user.id}},
            )
        except TelegramError as error:
            logger.warning("Could not send ephemeral reply in chat %s: %r", chat.id, error)
            if quiet:
                return False
            failure = await private_reply_failure(context, chat.id, error, can_resend=public_outcome is None)
            await update.message.reply_text(prefix + failure, parse_mode=ParseMode.HTML)
            return False

        # The Bot API ignores parameters it doesn't know, so a server without ephemeral support
        # would post publicly. Ephemeral messages are the only ones returned with message ID 0.
        if sent.message_id != 0:
            ephemeral_unsupported = True
            logger.error("Ephemeral reply in chat %s was posted publicly; deleting it", chat.id)
            notice = "This Telegram server doesn't support private replies, so I can't show that here."
            try:
                await context.bot.delete_message(chat.id, sent.message_id)
            except TelegramError:
                logger.exception("Could not delete public reply %s in chat %s", sent.message_id, chat.id)
                # Even an optional extra must not stay public without anyone being told.
                if quiet:
                    notice = "My private note was posted publicly and I couldn't delete it, so please delete it."
                else:
                    notice += " My reply was posted publicly and I couldn't delete it, so please delete it."
            else:
                if quiet:
                    return False
            await update.message.reply_text(prefix + notice, parse_mode=ParseMode.HTML)
            return False
    return True


async def private_reply_failure(context: ContextTypes.DEFAULT_TYPE, chat_id: int, error: TelegramError,
                                can_resend: bool = True) -> str:
    """Explain a failed ephemeral reply without blaming admin rights unless they are the cause.

    Only suggest sending the command again when doing so would repeat the same reply.
    """
    if isinstance(error, TimedOut):
        if can_resend:
            return "Telegram didn't confirm my private reply. If you didn't get it, send the command again."
        return "Telegram didn't confirm my private reply, so you may not have gotten the details."
    if isinstance(error, (BadRequest, Forbidden)):
        try:
            member = await context.bot.get_chat_member(chat_id, context.bot.id)
        except TelegramError:
            member = None
        if member is not None and member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return "I couldn't reply privately. Make me a group admin so I can send replies only you can see."
    if can_resend:
        return "I couldn't reply privately right now. Please try again later."
    return "I couldn't reply privately right now."


async def handle_wdone(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, args: str) -> None:
    """Handle !wdone command - remove tasks by PR/MR reference, number, or number range."""
    await handle_remove_tasks(update, context, chat_id, args, outcome="done", usage=WDONE_USAGE)


async def handle_wbounce(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, args: str) -> None:
    """Handle !wbounce command - remove tasks that require changes."""
    await handle_remove_tasks(update, context, chat_id, args, outcome="bounce", usage=WBOUNCE_USAGE,
                              comment="Changes required.")


async def handle_remove_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, args: str,
                              outcome: str, usage: str, comment: Optional[str] = None) -> None:
    """Remove the referenced tasks in one transaction and report what was removed and what wasn't."""
    references = split_references(args)
    if not references:
        await update.message.reply_text(usage, parse_mode=ParseMode.HTML)
        return
    resolutions = await resolve_queue_references(update, chat_id, [references])
    if resolutions is None:
        return

    resolution = resolutions[0]
    tasks = list(resolution.matched.values())
    completed_by_id, completed_by = sender_identity(update)
    try:
        removed = db.complete_tasks(chat_id, [task.id for task in tasks], outcome, completed_by, completed_by_id)
    except Exception:
        logger.exception("Could not complete reviews %s in chat %s", [task.task_id for task in tasks], chat_id)
        await update.message.reply_text(
            "Could not remove the review. Please try again later." if len(tasks) == 1
            else "Could not remove the reviews, so none were removed. Please try again later.")
        return

    removed_ids = {task.id for task in removed}
    blocks = []
    if len(removed) == 1:
        blocks.append(f"Removed {review_link(removed[0].task_id, removed[0].url)} (added by {html_escape(removed[0].created_by)})")
    elif removed:
        blocks.append(f"Removed {len(removed)}:")
        blocks += [f"• {review_link(task.task_id, task.url)} (added by {html_escape(task.created_by)})" for task in removed]
    if removed and comment:
        blocks.append(html_escape(comment))
    blocks += queue_problem_blocks(resolution, [task for task in tasks if task.id not in removed_ids])
    await reply_blocks(update, blocks)
    logger.info(f"Removed {len(removed)} tasks ({outcome}) from chat {chat_id}")
    if removed:
        schedule_cheer(update, context, chat_id, removed, outcome)


def schedule_cheer(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, removed: list[Task],
                   outcome: str) -> None:
    """Privately cheer the reviewer in the background, so writing the message never delays the bot."""
    task = asyncio.create_task(send_cheer(update, context, chat_id, removed, outcome, datetime.now(timezone.utc)))
    cheer_tasks.add(task)
    task.add_done_callback(cheer_tasks.discard)


async def send_cheer(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, removed: list[Task],
                     outcome: str, now: datetime) -> None:
    """Send the reviewer a playful message only they can see. Reviews of their own PRs/MRs don't count,
    and a cheer that can't be delivered is dropped quietly instead of bothering the chat."""
    try:
        sender_id, sender = sender_identity(update)
        if sender_id is None or (ephemeral_unsupported and update.effective_chat.type != ChatType.PRIVATE):
            return
        # Same self-review rules as the leaderboard, including legacy names of renamed users.
        people = Identities(db.get_known_users(chat_id))
        reviews = [task for task in removed
                   if not people.is_self_review(task.created_by_id, task.created_by, sender_id, sender)]
        if outcome == "done":
            cheer = approval_cheer([(task.task_id, task.url, queue_wait(task.created_at, now)) for task in reviews])
        else:
            # Rounds count reviewers sending the PR/MR back, not its author bouncing it themselves.
            identities = {task.id: parsed[1] for task in reviews if (parsed := parse_review_url(task.url))}
            rounds = Counter(
                bounce.review_identity for bounce in db.get_bounces(chat_id, list(identities.values()))
                if not people.is_self_review(bounce.created_by_id, bounce.created_by,
                                             bounce.completed_by_id, bounce.completed_by)
            )
            cheer = bounce_cheer([(task.task_id, task.url, max(1, rounds[identities.get(task.id)]))
                                  for task in reviews])
        if cheer is None:
            return
        text = await cheer_writer.message(cheer, sender_id)
        await reply_privately(update, context, [text], quiet=True)
    except Exception:
        logger.exception("Could not cheer the reviewer in chat %s", chat_id)


async def handle_wleaderboard(update: Update, chat_id: int, previous: bool = False) -> None:
    board = build_leaderboard(db, chat_id, reporting_week(previous=previous))
    await update.message.reply_text(format_leaderboard(board), parse_mode=ParseMode.HTML)


async def handle_wleaderboard_toggle(update: Update, chat_id: int, enabled: bool) -> None:
    db.set_leaderboard_enabled(chat_id, enabled)
    response = (
        "Weekly recaps enabled for the next scheduled recap: Monday at 09:00 Asia/Tashkent."
        if enabled else "Weekly recaps disabled. Activity tracking continues."
    )
    await update.message.reply_text(response)


async def handle_whelp(update: Update) -> None:
    """Handle !whelp command - display help instructions."""
    help_text = f"""<b>Work Queue Commands</b>

<code>!wadd &lt;URL&gt; [@username ...]</code>
Add one or more merge requests (optionally assign to one or more users)
Examples:
• <code>!wadd http://gitlab.example.com/group/repo/-/merge_requests/123</code>
• <code>!wadd http://gitlab.example.com/group/repo/-/merge_requests/123 @alice @bob</code>
• <code>!wadd &lt;URL 1&gt; &lt;URL 2&gt; @alice</code> (both assigned to @alice)
• One link per line, each followed by its own <code>@username</code>s

<code>!w</code>
List all tasks in the queue

<code>!wme</code>
Privately show reviews assigned to you, and your PRs/MRs that were approved (ready to merge) or bounced (only you see the reply; the bot must be a group admin)

<code>!wmerged &lt;PR/MR references&gt;</code>
Mark your approved PRs/MRs as merged, so they leave <code>!wme</code>
Examples: <code>!wmerged 123</code>, <code>!wmerged 55 67</code>, or <code>!wmerged 55-60</code>

<code>!wclear &lt;PR/MR references&gt;</code> / <code>!wclear all</code>
Clear your approved or bounced PRs/MRs from <code>!wme</code> (only you see the reply; the bot must be a group admin)
Examples: <code>!wclear 55 67 88</code>, <code>!wclear 55-100</code>, or <code>!wclear all</code>

<code>!wdone &lt;PR/MR references&gt;</code>
Remove completed tasks by PR/MR reference, URL, unambiguous PR/MR number, or number range
Examples: <code>!wdone repo/123</code>, <code>!wdone 123</code>, <code>!wdone 1 2 3 4</code>, or <code>!wdone 1...4</code>

<code>!wbounce &lt;PR/MR references&gt;</code>
Remove tasks with a Changes required comment
Examples: <code>!wbounce repo/123</code>, <code>!wbounce 123</code>, <code>!wbounce 1 2 3</code>, or <code>!wbounce 1-3</code>
After <code>!wdone</code> or <code>!wbounce</code>, the reviewer privately gets a playful note about their speed or bounce round

<code>!wassign &lt;PR/MR references&gt; @username [...]</code>
Assign or reassign tasks (replaces all existing assignees)
Examples: <code>!wassign repo/45 @alice</code>, <code>!wassign 45 @bob @charlie</code>, or <code>!wassign 45 46 50-52 @alice</code>
One line per group gives each its own users. Ranges in one queue command cover at most {MAX_QUEUE_RANGE} reviews.

<code>!wreminder-set &lt;cron_expression&gt;</code>
Set automatic reminder (5-part cron format, UTC time)
Examples:
• <code>!wreminder-set 0 9 * * *</code> (daily at 9 AM UTC)
• <code>!wreminder-set 0 9,17 * * 0-4</code> (weekdays at 9 AM & 5 PM)

<code>!wreminder</code>
Show current reminder configuration

<code>!wreminder-off</code>
Disable reminder (keeps configuration)

<code>!wreminder-remove</code>
Delete reminder configuration

<code>!wleaderboard</code> / <code>!wleaderboard last</code>
Show this week's or last week's top five contributors and reviewers (Asia/Tashkent)

<code>!wleaderboard-off</code> / <code>!wleaderboard-on</code>
Disable or enable automatic recaps; any chat member can toggle them. Tracking continues.
Recaps: Monday at 09:00 Asia/Tashkent, covering the previous Monday–Sunday.

<b>Leaderboard scoring:</b>
• !wdone: contributor point for the queue submitter; reviewer point for the command sender
• !wbounce: reviewer point for the command sender; no contributor point
• Self-reviews earn no reviewer points. Assignees do not receive points automatically.
• Each person earns at most one point per PR/MR per ranking per completion week, even if re-added.

<code>!whelp</code>
Show this help message

<b>Supported URLs:</b>
• GitLab: <code>http://host/group/project/-/merge_requests/N</code>
• GitHub: <code>https://github.com/owner/repo/pull/N</code>

<b>Cron Format:</b> <code>* * * * *</code> = minute hour day month day_of_week
<b>Day of week:</b> 0=Monday, 1=Tuesday, ..., 6=Sunday"""
    
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)


async def handle_wreminder_status(update: Update, chat_id: int) -> None:
    """Handle !wreminder command - show current reminder configuration."""
    reminder = db.get_reminder(chat_id)
    
    if reminder is None:
        await update.message.reply_text(
            "No reminder configured for this chat.\n\n"
            "Use <code>!wreminder-set &lt;cron_expression&gt;</code> to set one.\n"
            "Example: <code>!wreminder-set 0 9 * * *</code> (daily at 9 AM UTC)",
            parse_mode=ParseMode.HTML
        )
        return
    
    status = "✅ Enabled" if reminder.enabled else "⏸ Disabled"
    response = f"""<b>Reminder Configuration</b>

Status: {status}
Schedule: <code>{html_escape(reminder.cron_expression)}</code>
Timezone: UTC
Created: {reminder.created_at}
Updated: {reminder.updated_at}

Use <code>!wreminder-off</code> to disable or <code>!wreminder-remove</code> to delete."""
    
    await update.message.reply_text(response, parse_mode=ParseMode.HTML)


async def handle_wreminder_set(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, cron_expression: str) -> None:
    """Handle !wreminder-set command - set or update reminder schedule."""
    # Validate cron expression
    parts = cron_expression.split()
    if len(parts) != 5:
        await update.message.reply_text(
            "❌ Invalid cron expression. Must have 5 parts: minute hour day month day_of_week\n\n"
            "<b>Format:</b> <code>* * * * *</code>\n"
            "         ↓ ↓ ↓ ↓ ↓\n"
            "         │ │ │ │ └─ Day of week (0-6, 0=Mon, 6=Sun)\n"
            "         │ │ │ └─── Month (1-12)\n"
            "         │ │ └───── Day (1-31)\n"
            "         │ └─────── Hour (0-23)\n"
            "         └───────── Minute (0-59)\n\n"
            "<b>Examples:</b>\n"
            "• <code>0 9 * * *</code> - Daily at 9 AM UTC\n"
            "• <code>0 9,17 * * *</code> - Daily at 9 AM & 5 PM UTC\n"
            "• <code>0 9 * * 0-4</code> - Weekdays at 9 AM UTC\n"
            "• <code>0 */4 * * *</code> - Every 4 hours",
            parse_mode=ParseMode.HTML
        )
        return
    
    # Try to validate with APScheduler
    try:
        minute, hour, day, month, day_of_week = parts
        CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            timezone='UTC'
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Invalid cron expression: {html_escape(str(e))}\n\n"
            "Please check your expression and try again.\n"
            "Example: <code>!wreminder-set 0 9 * * *</code>",
            parse_mode=ParseMode.HTML
        )
        return
    
    # Save to database
    db.set_reminder(chat_id, cron_expression, enabled=True)
    
    # Add/update scheduler job
    try:
        # Get the application from the context
        add_reminder_job(chat_id, cron_expression, context.application, db)
        
        await update.message.reply_text(
            f"✅ Reminder set successfully!\n\n"
            f"Schedule: <code>{html_escape(cron_expression)}</code>\n"
            f"Timezone: UTC\n\n"
            f"You'll receive reminders when there are pending tasks.\n"
            f"Use <code>!wreminder</code> to check status.",
            parse_mode=ParseMode.HTML
        )
        logger.info(f"Set reminder for chat {chat_id}: {cron_expression}")
        
    except Exception as e:
        logger.error(f"Error setting reminder for chat {chat_id}: {e}", exc_info=True)
        await update.message.reply_text(
            "❌ Error setting reminder. Please try again later.",
            parse_mode=ParseMode.HTML
        )


async def handle_wreminder_off(update: Update, chat_id: int) -> None:
    """Handle !wreminder-off command - disable reminder."""
    success = db.disable_reminder(chat_id)
    
    if not success:
        await update.message.reply_text(
            "No reminder configured for this chat.\n"
            "Use <code>!wreminder-set &lt;cron_expression&gt;</code> to set one.",
            parse_mode=ParseMode.HTML
        )
        return
    
    # Remove from scheduler
    remove_reminder_job(chat_id)
    
    await update.message.reply_text(
        "⏸ Reminder disabled.\n\n"
        "Your configuration is saved. Use <code>!wreminder-set &lt;cron_expression&gt;</code> to re-enable.",
        parse_mode=ParseMode.HTML
    )
    logger.info(f"Disabled reminder for chat {chat_id}")


async def handle_wreminder_remove(update: Update, chat_id: int) -> None:
    """Handle !wreminder-remove command - delete reminder configuration."""
    success = db.delete_reminder(chat_id)
    
    if not success:
        await update.message.reply_text(
            "No reminder configured for this chat.",
            parse_mode=ParseMode.HTML
        )
        return
    
    # Remove from scheduler
    remove_reminder_job(chat_id)
    
    await update.message.reply_text(
        "🗑 Reminder configuration deleted.\n\n"
        "Use <code>!wreminder-set &lt;cron_expression&gt;</code> to create a new one.",
        parse_mode=ParseMode.HTML
    )
    logger.info(f"Removed reminder for chat {chat_id}")


def parse_wassign(args: str) -> list[tuple[list[str], list[str]]] | None:
    """Split !wassign arguments into (references, assignees) groups, or None if they don't fit.

    Each run of references goes to the @usernames that follow it, so
    `!wassign 5 6 @alice` followed by a line `7 @bob` gives 5 and 6 to @alice and 7 to @bob.
    """
    groups = []
    for token in split_references(args):
        if token.startswith("@"):
            if not groups or not MENTIONS_PATTERN.fullmatch(token):
                return None
            groups[-1][1].extend(mention for mention in MENTION_PATTERN.findall(token) if mention not in groups[-1][1])
        elif not groups or groups[-1][1]:
            groups.append(([token], []))
        else:
            groups[-1][0].append(token)
    return groups if groups and groups[-1][1] else None


async def handle_wassign(update: Update, chat_id: int, groups: list[tuple[list[str], list[str]]]) -> None:
    """Handle !wassign command - assign or reassign tasks to one or more users, in one transaction."""
    resolutions = await resolve_queue_references(update, chat_id, [references for references, _ in groups])
    if resolutions is None:
        return

    # A task named in several groups goes to the last one.
    owner = {task_id: index for index, resolution in enumerate(resolutions) for task_id in resolution.matched}
    try:
        updated = {task.id: task for task in db.assign_tasks(chat_id, [(task_id, groups[index][1])
                                                                      for task_id, index in owner.items()])}
    except Exception:
        logger.exception("Could not assign reviews in chat %s", chat_id)
        await update.message.reply_text("Could not assign the reviews, so nothing was changed. Please try again later.")
        return

    blocks, gone = [], {}
    for index, resolution in enumerate(resolutions):
        tasks = [updated[task_id] for task_id in resolution.matched if task_id in updated and owner[task_id] == index]
        gone.update((task_id, task) for task_id, task in resolution.matched.items() if task_id not in updated)
        names = ", ".join(html_escape(assignee) for assignee in groups[index][1])
        if len(tasks) == 1:
            blocks.append(f"{review_link(tasks[0].task_id, tasks[0].url)} → {names}")
        elif tasks:
            blocks.append(f"Assigned {len(tasks)} → {names}:")
            blocks += [f"• {review_link(task.task_id, task.url)}" for task in tasks]
    combined = Resolution(missing=[reference for resolution in resolutions for reference in resolution.missing],
                          ambiguous=[item for resolution in resolutions for item in resolution.ambiguous])
    blocks += queue_problem_blocks(combined, list(gone.values()))
    await reply_blocks(update, blocks)
    logger.info(f"Assigned {len(updated)} tasks in chat {chat_id}")


async def post_init(application: Application) -> None:
    """Start jobs and catch up recaps after the Telegram client is initialized."""
    application.bot_data["scheduler"] = await setup_scheduler(application, db)


async def post_stop(application: Application) -> None:
    scheduler = application.bot_data.get("scheduler")
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)


def main() -> None:
    """Start the bot."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set in environment variables")
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")
    
    # Create application
    application = Application.builder().token(token).post_init(post_init).post_stop(post_stop).build()
    
    # Add message handler for all text messages
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    
    # Start polling
    logger.info("Starting bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
