import os
import re
import logging
from typing import Optional
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode
from html import escape as html_escape

from database import Database, Task
from leaderboard import build_leaderboard, format_leaderboard, reporting_week
from presentation import review_link, task_listing
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
WADD_PATTERN_WITH_USER = re.compile(r"^!wadd\s+(https?://\S+)\s+((?:@\w+\s*)+)$", re.IGNORECASE)
WADD_PATTERN_NO_USER = re.compile(r"^!wadd\s+(https?://\S+)$", re.IGNORECASE)
WADD_PREFIX = re.compile(r"^!wadd\b", re.IGNORECASE)
W_PATTERN = re.compile(r"^!w$", re.IGNORECASE)
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


def validate_wadd_args(text: str) -> str:
    """Validate !wadd arguments and return specific error message."""
    parts = text.split(None, 2)  # Split into max 3 parts: !wadd, url, @user(s) (optional)
    
    if len(parts) == 1:
        # Just "!wadd" with no arguments
        return (
            "Missing URL.\n"
            "Usage: <code>!wadd &lt;URL&gt; [@username ...]</code>\n"
            "Examples:\n"
            "• <code>!wadd http://gitlab.example.com/group/repo/-/merge_requests/123</code>\n"
            "• <code>!wadd http://gitlab.example.com/group/repo/-/merge_requests/123 @alice</code>\n"
            "• <code>!wadd http://gitlab.example.com/group/repo/-/merge_requests/123 @alice @bob</code>"
        )
    
    if len(parts) == 2:
        arg = parts[1]
        if arg.startswith("@"):
            return "Missing URL. Provide a GitLab MR or GitHub PR link before the username(s)."
        elif arg.startswith("http://") or arg.startswith("https://"):
            # URL only is valid, but check if it matches GitLab/GitHub pattern
            if extract_task_id(arg) is None:
                return (
                    "Unsupported URL format. Must be a GitLab merge request or GitHub pull request.\n"
                    "Supported formats:\n"
                    "• <code>http://host/group/project/-/merge_requests/N</code>\n"
                    "• <code>https://github.com/owner/repo/pull/N</code>"
                )
            # Valid URL, no assignee - this is fine, shouldn't reach here though
            return ""
        else:
            return (
                "Invalid URL. Must start with http:// or https://\n"
                "Example: <code>!wadd http://gitlab.example.com/group/repo/-/merge_requests/123</code>"
            )
    
    # len(parts) >= 3, but pattern didn't match
    url_part = parts[1]
    user_part = parts[2]
    
    if not (url_part.startswith("http://") or url_part.startswith("https://")):
        return "Invalid URL. Must start with http:// or https://"
    
    # Check if user_part contains at least one @username
    if not re.search(r'@\w+', user_part):
        return f"Invalid username format. Use <code>@username</code> for each assignee (got: {html_escape(user_part)})"
    
    # URL looks valid but doesn't match GitLab/GitHub pattern
    if extract_task_id(url_part) is None:
        return (
            "Unsupported URL format. Must be a GitLab merge request or GitHub pull request.\n"
            "Supported formats:\n"
            "• <code>http://host/group/project/-/merge_requests/N</code>\n"
            "• <code>https://github.com/owner/repo/pull/N</code>"
        )
    
    return (
        "Invalid command format.\n"
        "Usage: <code>!wadd &lt;URL&gt; [@username ...]</code>"
    )


def extract_task_id(url: str) -> str | None:
    parsed = parse_review_url(url)
    return parsed[0] if parsed else None


def resolve_task_reference(chat_id: int, task_ref: str) -> tuple[Task | None, str | None]:
    """Find one task in this chat, or explain why the reference cannot be used."""
    if LEGACY_REFERENCE_PATTERN.fullmatch(task_ref):
        return None, f"Queue numbers such as <code>{html_escape(task_ref)}</code> are no longer supported. {REFERENCE_USAGE}"

    tasks = db.get_tasks(chat_id)
    if NUMBER_PATTERN.fullmatch(task_ref):
        number = normalize_review_number(task_ref)
        matches = [
            task for task in tasks
            if NUMBER_PATTERN.fullmatch(task.task_id.rsplit("/", 1)[-1])
            and normalize_review_number(task.task_id.rsplit("/", 1)[-1]) == number
        ]
    elif QUALIFIED_REFERENCE_PATTERN.fullmatch(task_ref):
        matches = [task for task in tasks if task.task_id == task_ref]
    else:
        parsed = parse_review_url(task_ref)
        if parsed is None:
            return None, f"Invalid PR/MR reference <code>{html_escape(task_ref)}</code>. {REFERENCE_USAGE}"
        url_identity = parsed[1]
        matches = [task for task in tasks if (saved := parse_review_url(task.url)) and saved[1] == url_identity]

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
    wadd_match_with_user = WADD_PATTERN_WITH_USER.match(text)
    wadd_match_no_user = WADD_PATTERN_NO_USER.match(text)
    
    if wadd_match_with_user:
        url = wadd_match_with_user.group(1)
        assignees_str = wadd_match_with_user.group(2)
        assignees = parse_assignees(assignees_str)
        await handle_wadd(update, chat_id, url, assignees, created_by, created_by_id)
        return
    elif wadd_match_no_user:
        url = wadd_match_no_user.group(1)
        await handle_wadd(update, chat_id, url, [], created_by, created_by_id)
        return
    elif WADD_PREFIX.match(text):
        error_msg = validate_wadd_args(text)
        await update.message.reply_text(error_msg, parse_mode=ParseMode.HTML)
        return
    
    # Check for !w command
    if W_PATTERN.match(text):
        await handle_w(update, chat_id)
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


async def handle_wadd(update: Update, chat_id: int, url: str, assignees: list[str], created_by: str,
                      created_by_id: int | None = None) -> None:
    """Handle !wadd command - add a new task from MR/PR link."""
    task_id = extract_task_id(url)
    
    if task_id is None:
        await update.message.reply_text(
            "Invalid URL. Please provide a GitLab merge request or GitHub pull request link.\n"
            "Examples:\n"
            "• http://gitlab.example.com/group/repo/-/merge_requests/123\n"
            "• https://github.com/owner/repo/pull/123"
        )
        return
    
    if db.add_task(chat_id, task_id, url, assignees, created_by, created_by_id) is None:
        await update.message.reply_text(f"PR/MR reference {task_id} already exists in the queue.")
        return
    
    response = review_link(task_id, url)
    if assignees:
        assignees_formatted = ", ".join(html_escape(a) for a in assignees)
        response += f" → {assignees_formatted}"
    await update.message.reply_text(response, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    
    assignees_log = ", ".join(assignees) if assignees else "unassigned"
    logger.info(f"Added task {task_id} in chat {chat_id}: {url} -> {assignees_log}")


async def handle_w(update: Update, chat_id: int) -> None:
    """Handle !w command - list all tasks."""
    tasks = db.get_tasks(chat_id)
    
    if not tasks:
        await update.message.reply_text("No tasks in the queue.")
        return
    
    response = "\n".join(task_listing(task) for task in tasks)
    await update.message.reply_text(response, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


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
Add a merge request (optionally assign to one or more users)
Examples:
• <code>!wadd http://gitlab.example.com/group/repo/-/merge_requests/123</code>
• <code>!wadd http://gitlab.example.com/group/repo/-/merge_requests/123 @alice</code>
• <code>!wadd http://gitlab.example.com/group/repo/-/merge_requests/123 @alice @bob</code>

<code>!w</code>
List all tasks in the queue

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
