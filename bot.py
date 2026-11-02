import asyncio
import os
import re
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError, TimedOut
from html import escape as html_escape

from database import Database, FinishedReview, Task
from leaderboard import build_leaderboard, format_leaderboard, reporting_week
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

# Regex patterns for commands
WADD_PREFIX = re.compile(r"^!wadd\b", re.IGNORECASE)
W_PATTERN = re.compile(r"^!w$", re.IGNORECASE)
WME_PATTERN = re.compile(r"^!wme$", re.IGNORECASE)
WMERGED_PATTERN = re.compile(r"^!wmerged\s+(.+)$", re.IGNORECASE)
WMERGED_PREFIX = re.compile(r"^!wmerged\b", re.IGNORECASE)
WCLEAR_PATTERN = re.compile(r"^!wclear\s+(.+)$", re.IGNORECASE | re.DOTALL)
WCLEAR_PREFIX = re.compile(r"^!wclear\b", re.IGNORECASE)
WDONE_PATTERN = re.compile(r"^!wdone\s+(.+)$", re.IGNORECASE)
WDONE_PREFIX = re.compile(r"^!wdone\b", re.IGNORECASE)
WBOUNCE_PATTERN = re.compile(r"^!wbounce\s+(.+)$", re.IGNORECASE)
WBOUNCE_PREFIX = re.compile(r"^!wbounce\b", re.IGNORECASE)
WHELP_PATTERN = re.compile(r"^!whelp$", re.IGNORECASE)
WREMINDER_STATUS_PATTERN = re.compile(r"^!wreminder$", re.IGNORECASE)
WREMINDER_SET_PATTERN = re.compile(r"^!wreminder-set\s+(.+)$", re.IGNORECASE)
WREMINDER_OFF_PATTERN = re.compile(r"^!wreminder-off$", re.IGNORECASE)
WREMINDER_REMOVE_PATTERN = re.compile(r"^!wreminder-remove$", re.IGNORECASE)
WASSIGN_PATTERN = re.compile(r"^!wassign\s+(.+?)\s+((?:@\w+\s*)+)$", re.IGNORECASE)
WASSIGN_PREFIX = re.compile(r"^!wassign\b", re.IGNORECASE)
WLEADERBOARD_PATTERN = re.compile(r"^!wleaderboard(?:\s+(last))?$", re.IGNORECASE)
WLEADERBOARD_OFF_PATTERN = re.compile(r"^!wleaderboard-off$", re.IGNORECASE)
WLEADERBOARD_ON_PATTERN = re.compile(r"^!wleaderboard-on$", re.IGNORECASE)
WLEADERBOARD_PREFIX = re.compile(r"^!wleaderboard\b", re.IGNORECASE)

QUALIFIED_REFERENCE_PATTERN = re.compile(r"[^/\s]+/[0-9]+")
NUMBER_PATTERN = re.compile(r"[0-9]+")
LEGACY_REFERENCE_PATTERN = re.compile(r"#[0-9]+")
MENTION_PATTERN = re.compile(r"@\w+")
# Commas separate links and usernames, but a comma inside a URL's query string stays part of it.
WADD_SEPARATOR_PATTERN = re.compile(r"\s+|,+(?=\s|$|@|https?://)", re.IGNORECASE)
# PR/MR number ranges such as 55-100 or 55..100.
RANGE_PATTERN = re.compile(r"([0-9]+)\s*(?:-|\.{2,}|…)\s*([0-9]+)")
CLEAR_REFERENCE_PATTERN = re.compile(rf"{RANGE_PATTERN.pattern}|[^\s,]+")

MEMBER_STATUSES = (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER)
# Telegram rate-limits bursts of requests, so check a user's groups a few at a time.
MEMBERSHIP_CHECK_CONCURRENCY = 5

REFERENCE_USAGE = "Use a PR/MR reference such as <code>repo/120</code>, its URL, or an unambiguous PR/MR number. Use <code>!w</code> to see current references."


def parse_assignees(assignees_str: str) -> list[str]:
    """Parse multiple @mentions from a string into a list of formatted usernames.
    
    Args:
        assignees_str: String containing one or more @username mentions
        
    Returns:
        List of usernames with @ prefix (e.g., ['@alice', '@bob'])
    """
    # Find all @username patterns
    mentions = re.findall(r'@(\w+)', assignees_str)
    # Return with @ prefix
    return [f"@{mention}" for mention in mentions]


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


def resolve_task_reference(chat_id: int, task_ref: str) -> tuple[Task | None, str | None]:
    """Find one task in this chat, or explain why the reference cannot be used."""
    if LEGACY_REFERENCE_PATTERN.fullmatch(task_ref):
        return None, f"Queue numbers such as <code>{html_escape(task_ref)}</code> are no longer supported. {REFERENCE_USAGE}"

    matches = matching_reviews(db.get_tasks(chat_id), task_ref)
    if matches is None:
        return None, f"Invalid PR/MR reference <code>{html_escape(task_ref)}</code>. {REFERENCE_USAGE}"
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        alternatives = ", ".join(review_link(task.task_id, task.url) for task in matches)
        return None, f"PR/MR number <code>{html_escape(task_ref)}</code> matches multiple reviews: {alternatives}. Use a qualified PR/MR reference."
    return None, f"PR/MR reference <code>{html_escape(task_ref)}</code> not found in this chat. Use <code>!w</code> to see current references."


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
        await handle_wmerged(update, chat_id, wmerged_match.group(1).strip())
        return
    elif WMERGED_PREFIX.match(text):
        await update.message.reply_text(
            "Usage: <code>!wmerged &lt;PR/MR reference&gt;</code>\n"
            "Mark one of your approved PRs/MRs as merged, so it leaves <code>!wme</code>.\n"
            "Examples: <code>!wmerged repo/123</code>, <code>!wmerged 123</code>, or <code>!wmerged https://github.com/owner/repo/pull/123</code>",
            parse_mode=ParseMode.HTML
        )
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
        task_ref = wdone_match.group(1).strip()
        await handle_wdone(update, chat_id, task_ref)
        return
    elif WDONE_PREFIX.match(text):
        await update.message.reply_text(
            "Usage: <code>!wdone &lt;PR/MR reference&gt;</code>\n"
            "Examples: <code>!wdone repo/123</code>, <code>!wdone 123</code>, or <code>!wdone https://github.com/owner/repo/pull/123</code>",
            parse_mode=ParseMode.HTML
        )
        return
    
    # Check for !wbounce command
    wbounce_match = WBOUNCE_PATTERN.match(text)
    if wbounce_match:
        task_ref = wbounce_match.group(1).strip()
        await handle_wbounce(update, chat_id, task_ref)
        return
    elif WBOUNCE_PREFIX.match(text):
        await update.message.reply_text(
            "Usage: <code>!wbounce &lt;PR/MR reference&gt;</code>\n"
            "Remove a task with a Changes required comment.\n"
            "Examples: <code>!wbounce repo/123</code>, <code>!wbounce 123</code>, or <code>!wbounce https://github.com/owner/repo/pull/123</code>",
            parse_mode=ParseMode.HTML
        )
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
    wassign_match = WASSIGN_PATTERN.match(text)
    if wassign_match:
        task_ref = wassign_match.group(1).strip()
        assignees_str = wassign_match.group(2)
        assignees = parse_assignees(assignees_str)
        await handle_wassign(update, chat_id, task_ref, assignees)
        return
    elif WASSIGN_PREFIX.match(text):
        parts = text.split()
        if len(parts) > 1 and LEGACY_REFERENCE_PATTERN.fullmatch(parts[1]):
            _, error = resolve_task_reference(chat_id, parts[1])
            await update.message.reply_text(error, parse_mode=ParseMode.HTML)
            return
        await update.message.reply_text(
            "Usage: <code>!wassign &lt;PR/MR reference&gt; @username [...]</code>\n"
            "Examples:\n"
            "• <code>!wassign repo/123 @alice</code>\n"
            "• <code>!wassign 123 @alice @bob</code>\n"
            "• <code>!wassign https://github.com/owner/repo/pull/123 @alice @bob @charlie</code>",
            parse_mode=ParseMode.HTML
        )
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


async def handle_wmerged(update: Update, chat_id: int, task_ref: str) -> None:
    """Handle !wmerged command - take one of the sender's approved PRs/MRs off their !wme list."""
    user_id, name = sender_identity(update)
    if user_id is None:
        await update.message.reply_text("<code>!wmerged</code> needs to know who you are. Send it from your own account, not anonymously.",
                                        parse_mode=ParseMode.HTML)
        return

    username = update.effective_user.username
    since = datetime.now(timezone.utc) - timedelta(days=FINISHED_VISIBLE_DAYS)
    approved = [review for review in db.get_finished_reviews(user_id, f"@{username}" if username else None, since, chat_id)
                if review.outcome == "done"]
    matches = matching_reviews(approved, task_ref)
    if matches is None:
        await update.message.reply_text(
            f"Invalid PR/MR reference <code>{html_escape(task_ref)}</code>. Use a reference such as <code>repo/120</code>, its URL, or its number.",
            parse_mode=ParseMode.HTML)
        return
    if len(matches) > 1:
        alternatives = ", ".join(review_link(review.task_id, review.url) for review in matches)
        await update.message.reply_text(
            f"PR/MR number <code>{html_escape(task_ref)}</code> matches several of your approved reviews: {alternatives}. Use a qualified PR/MR reference.",
            parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return
    if not matches:
        await update.message.reply_text(
            f"<code>{html_escape(task_ref)}</code> isn't one of your approved PRs/MRs in this chat. Use <code>!wme</code> to see them.",
            parse_mode=ParseMode.HTML)
        return

    review = matches[0]
    db.mark_merged(review.activity_id, name, user_id)
    await update.message.reply_text(f"✅ Marked {review_link(review.task_id, review.url)} as merged.",
                                    parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    logger.info(f"Marked review {review.task_id} as merged in chat {chat_id}")


WCLEAR_USAGE = (
    "Usage: <code>!wclear &lt;PR/MR references&gt;</code> or <code>!wclear all</code>\n"
    "Clear your approved or bounced PRs/MRs from <code>!wme</code>.\n"
    "Examples: <code>!wclear 55 67 88</code>, <code>!wclear 55-100</code>, <code>!wclear repo/55, repo/67</code>, or <code>!wclear all</code>"
)


def number_key(number: str) -> tuple[int, str]:
    """Order normalized PR/MR numbers numerically without converting arbitrarily long digit strings."""
    return len(number), number


def reviews_in_range(reviews: list[FinishedReview], low: str, high: str) -> list[FinishedReview]:
    low, high = sorted((number_key(normalize_review_number(low)), number_key(normalize_review_number(high))))
    return [review for review in reviews
            if (number := review_number(review)) is not None and low <= number_key(number) <= high]


async def handle_wclear(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, args: str) -> None:
    """Handle !wclear command - take approved or bounced PRs/MRs off the sender's !wme list."""
    user_id, name = sender_identity(update)
    if user_id is None:
        await update.message.reply_text("<code>!wclear</code> needs to know who you are. Send it from your own account, not anonymously.",
                                        parse_mode=ParseMode.HTML)
        return

    references = [match.group(0) for match in CLEAR_REFERENCE_PATTERN.finditer(args)]
    if not references:
        await update.message.reply_text(WCLEAR_USAGE, parse_mode=ParseMode.HTML)
        return
    clear_all = [reference.lower() for reference in references] == ["all"]
    if not clear_all:
        for reference in references:
            if not RANGE_PATTERN.fullmatch(reference) and matching_reviews([], reference) is None:
                await update.message.reply_text(f"Invalid PR/MR reference <code>{html_escape(reference)}</code>.\n{WCLEAR_USAGE}",
                                                parse_mode=ParseMode.HTML)
                return

    _, finished, chat_titles, unchecked = await visible_reviews(update, context, chat_id, user_id, with_assigned=False)
    cleared = {review.activity_id: review for review in finished} if clear_all else {}
    missing, ambiguous = [], []
    for reference in [] if clear_all else references:
        if range_match := RANGE_PATTERN.fullmatch(reference):
            matches = reviews_in_range(finished, range_match.group(1), range_match.group(2))
        else:
            matches = matching_reviews(finished, reference)
            # The same PR/MR can be finished in several chats when clearing from a direct message,
            # but repos on different hosts or paths can share a task_id.
            if len({parse_review_url(review.url)[1] for review in matches}) > 1:
                ambiguous.append((reference, matches))
                continue
        if not matches:
            missing.append(reference)
        cleared.update((review.activity_id, review) for review in matches)

    db.clear_finished_reviews(list(cleared), name, user_id)
    blocks = []
    if cleared:
        blocks.append(f"🧹 <b>Cleared {len(cleared)} from your <code>!wme</code> list</b>")
        blocks += grouped_by_chat(list(cleared.values()), finished_listing, chat_titles)
    elif clear_all or not (missing or ambiguous):
        blocks.append("Nothing to clear: you have no approved or bounced PRs/MRs here.")
    if missing:
        blocks.append(
            "Not among your approved or bounced PRs/MRs: "
            + ", ".join(f"<code>{html_escape(reference)}</code>" for reference in missing)
            + ". Finish reviews assigned to you with <code>!wdone</code> or <code>!wbounce</code>."
        )
    for reference, matches in ambiguous:
        alternatives = ", ".join(review_link(review.task_id, review.url) for review in matches)
        blocks.append(f"<code>{html_escape(reference)}</code> matches several of your PRs/MRs: {alternatives}. "
                      "Use a qualified PR/MR reference.")
    if unchecked:
        groups = "1 of your groups" if unchecked == 1 else f"{unchecked} of your groups"
        blocks.append(f"⚠️ I couldn't check {groups} right now, so nothing there was cleared.")
    for message in split_messages(blocks):
        await update.message.reply_text(message, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    logger.info(f"Cleared {len(cleared)} finished reviews for user {user_id} in chat {chat_id}")


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


async def reply_privately(update: Update, context: ContextTypes.DEFAULT_TYPE, texts: list[str]) -> None:
    """Reply so only the sender sees it; in private chats a normal reply is already private."""
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        for text in texts:
            await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return

    # Ephemeral messages (Bot API 10.2+) are not wrapped by python-telegram-bot yet. Admin bots
    # may send them at any time; Telegram does not guarantee delivery to offline users.
    for index, text in enumerate(texts):
        partial = "Only part of my reply reached you. " if index else ""
        try:
            sent = await context.bot.send_message(
                chat.id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                message_thread_id=update.message.message_thread_id if update.message.is_topic_message else None,
                api_kwargs={"ephemeral_message_parameters": {"receiver_user_id": update.effective_user.id}},
            )
        except TelegramError as error:
            logger.warning("Could not send ephemeral reply in chat %s: %r", chat.id, error)
            await update.message.reply_text(partial + await private_reply_failure(context, chat.id, error),
                                            parse_mode=ParseMode.HTML)
            return

        # The Bot API ignores parameters it doesn't know, so a server without ephemeral support
        # would post publicly. Ephemeral messages are the only ones returned with message ID 0.
        if sent.message_id != 0:
            logger.error("Ephemeral reply in chat %s was posted publicly; deleting it", chat.id)
            notice = "This Telegram server doesn't support private replies, so I can't show that here."
            try:
                await context.bot.delete_message(chat.id, sent.message_id)
            except TelegramError:
                logger.exception("Could not delete public reply %s in chat %s", sent.message_id, chat.id)
                notice += " My reply was posted publicly and I couldn't delete it, so please delete it."
            await update.message.reply_text(partial + notice)
            return


async def private_reply_failure(context: ContextTypes.DEFAULT_TYPE, chat_id: int, error: TelegramError) -> str:
    """Explain a failed ephemeral reply without blaming admin rights unless they are the cause."""
    if isinstance(error, TimedOut):
        return "Telegram didn't confirm my private reply. If you didn't get it, send the command again."
    if isinstance(error, (BadRequest, Forbidden)):
        try:
            member = await context.bot.get_chat_member(chat_id, context.bot.id)
        except TelegramError:
            member = None
        if member is not None and member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return "I couldn't reply privately. Make me a group admin so I can send replies only you can see."
    return "I couldn't reply privately right now. Please try again later."


async def handle_wdone(update: Update, chat_id: int, task_ref: str) -> None:
    """Handle !wdone command - remove a task by PR/MR reference."""
    await handle_remove_task(update, chat_id, task_ref, outcome="done")


async def handle_wbounce(update: Update, chat_id: int, task_ref: str) -> None:
    """Handle !wbounce command - remove a task that requires changes."""
    await handle_remove_task(update, chat_id, task_ref, outcome="bounce", comment="Changes required.")


async def handle_remove_task(update: Update, chat_id: int, task_ref: str, outcome: str,
                              comment: Optional[str] = None) -> None:
    """Remove a task and reply with an optional comment."""
    task, error = resolve_task_reference(chat_id, task_ref)
    if error:
        await update.message.reply_text(error, parse_mode=ParseMode.HTML)
        return

    completed_by_id, completed_by = sender_identity(update)
    try:
        removed_task = db.complete_task(chat_id, task.id, outcome, completed_by, completed_by_id)
    except Exception:
        logger.exception("Could not complete review %s in chat %s", task.task_id, chat_id)
        await update.message.reply_text("Could not remove the review. Please try again later.")
        return
    if removed_task is None:
        await update.message.reply_text(f"PR/MR reference <code>{html_escape(task_ref)}</code> not found in this chat.", parse_mode=ParseMode.HTML)
        return
    
    response = f'Removed {review_link(removed_task.task_id, removed_task.url)} (added by {html_escape(removed_task.created_by)})'
    if comment:
        response += f"\n{html_escape(comment)}"
    await update.message.reply_text(response, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    logger.info(f"Removed task {removed_task.task_id} from chat {chat_id}")


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
    help_text = """<b>Work Queue Commands</b>

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

<code>!wmerged &lt;PR/MR reference&gt;</code>
Mark one of your approved PRs/MRs as merged, so it leaves <code>!wme</code>

<code>!wclear &lt;PR/MR references&gt;</code> / <code>!wclear all</code>
Clear your approved or bounced PRs/MRs from <code>!wme</code>
Examples: <code>!wclear 55 67 88</code>, <code>!wclear 55-100</code>, or <code>!wclear all</code>

<code>!wdone &lt;PR/MR reference&gt;</code>
Remove a completed task by PR/MR reference, URL, or unambiguous PR/MR number
Examples: <code>!wdone repo/123</code>, <code>!wdone 123</code>, or <code>!wdone https://github.com/owner/repo/pull/123</code>

<code>!wbounce &lt;PR/MR reference&gt;</code>
Remove a task with a Changes required comment
Examples: <code>!wbounce repo/123</code>, <code>!wbounce 123</code>, or <code>!wbounce https://github.com/owner/repo/pull/123</code>

<code>!wassign &lt;PR/MR reference&gt; @username [...]</code>
Assign or reassign task (replaces all existing assignees)
Examples: <code>!wassign repo/45 @alice</code>, <code>!wassign 45 @bob @charlie</code>, or <code>!wassign https://github.com/owner/repo/pull/45 @alice @bob</code>

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


async def handle_wassign(update: Update, chat_id: int, task_ref: str, assignees: list[str]) -> None:
    """Handle !wassign command - assign or reassign a task to one or more users."""
    task, error = resolve_task_reference(chat_id, task_ref)
    if error:
        await update.message.reply_text(error, parse_mode=ParseMode.HTML)
        return

    updated_task = db.update_task_assignees_by_id(chat_id, task.task_id, assignees)
    if updated_task is None:
        await update.message.reply_text(f"PR/MR reference <code>{html_escape(task_ref)}</code> not found in this chat.", parse_mode=ParseMode.HTML)
        return
    
    response = review_link(updated_task.task_id, updated_task.url)
    if assignees:
        assignees_formatted = ", ".join(html_escape(a) for a in assignees)
        response += f" → {assignees_formatted}"
    else:
        response += " (unassigned)"
    
    await update.message.reply_text(response, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    
    assignees_log = ", ".join(assignees) if assignees else "unassigned"
    logger.info(f"Assigned task {updated_task.task_id} to {assignees_log} in chat {chat_id}")


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
