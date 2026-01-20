import os
import re
import logging
from typing import Optional
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode
from html import escape as html_escape

from database import Database

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
WADD_PATTERN_WITH_USER = re.compile(r"^!wadd\s+(https?://\S+)\s+@(\w+)$", re.IGNORECASE)
WADD_PATTERN_NO_USER = re.compile(r"^!wadd\s+(https?://\S+)$", re.IGNORECASE)
WADD_PREFIX = re.compile(r"^!wadd\b", re.IGNORECASE)
W_PATTERN = re.compile(r"^!w$", re.IGNORECASE)
WDONE_PATTERN = re.compile(r"^!wdone\s+(.+)$", re.IGNORECASE)
WDONE_PREFIX = re.compile(r"^!wdone\b", re.IGNORECASE)
WHELP_PATTERN = re.compile(r"^!whelp$", re.IGNORECASE)

# Patterns for extracting task ID from MR/PR URLs
# GitLab: http://host/group/project/-/merge_requests/123
GITLAB_MR_PATTERN = re.compile(r"https?://[^/]+/(?:.+?/)*([^/]+)/-/merge_requests/(\d+)")
# GitHub: https://github.com/owner/repo/pull/123
GITHUB_PR_PATTERN = re.compile(r"https?://github\.com/[^/]+/([^/]+)/pull/(\d+)")


def validate_wadd_args(text: str) -> str:
    """Validate !wadd arguments and return specific error message."""
    parts = text.split(None, 2)  # Split into max 3 parts: !wadd, url, @user (optional)
    
    if len(parts) == 1:
        # Just "!wadd" with no arguments
        return (
            "Missing URL.\n"
            "Usage: <code>!wadd &lt;URL&gt; [@username]</code>\n"
            "Examples:\n"
            "• <code>!wadd http://gitlab.example.com/group/repo/-/merge_requests/123</code>\n"
            "• <code>!wadd http://gitlab.example.com/group/repo/-/merge_requests/123 @alice</code>"
        )
    
    if len(parts) == 2:
        arg = parts[1]
        if arg.startswith("@"):
            return "Missing URL. Provide a GitLab MR or GitHub PR link before the username."
        elif arg.startswith("http://") or arg.startswith("https://"):
            # URL only is valid, but check if it matches GitLab/GitHub pattern
            if not GITLAB_MR_PATTERN.match(arg) and not GITHUB_PR_PATTERN.match(arg):
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
    user_part = parts[2].split()[0] if parts[2] else ""
    
    if not (url_part.startswith("http://") or url_part.startswith("https://")):
        return "Invalid URL. Must start with http:// or https://"
    
    if not user_part.startswith("@"):
        return f"Invalid username format. Use <code>@username</code> (got: {html_escape(user_part)})"
    
    # URL looks valid but doesn't match GitLab/GitHub pattern
    if not GITLAB_MR_PATTERN.match(url_part) and not GITHUB_PR_PATTERN.match(url_part):
        return (
            "Unsupported URL format. Must be a GitLab merge request or GitHub pull request.\n"
            "Supported formats:\n"
            "• <code>http://host/group/project/-/merge_requests/N</code>\n"
            "• <code>https://github.com/owner/repo/pull/N</code>"
        )
    
    return (
        "Invalid command format.\n"
        "Usage: <code>!wadd &lt;URL&gt; [@username]</code>"
    )


def extract_task_id(url: str) -> str | None:
    """Extract task ID from a GitLab MR or GitHub PR URL.
    
    Returns format: repo/merge_requests/N or repo/pull/N
    """
    # Try GitLab pattern
    gitlab_match = GITLAB_MR_PATTERN.match(url)
    if gitlab_match:
        repo = gitlab_match.group(1)
        mr_number = gitlab_match.group(2)
        return f"{repo}/merge_requests/{mr_number}"
    
    # Try GitHub pattern
    github_match = GITHUB_PR_PATTERN.match(url)
    if github_match:
        repo = github_match.group(1)
        pr_number = github_match.group(2)
        return f"{repo}/pull/{pr_number}"
    
    return None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming messages and route to appropriate command handlers."""
    if not update.message or not update.message.text:
        return
    
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    
    # Get the username of the message author
    user = update.effective_user
    created_by = f"@{user.username}" if user and user.username else user.first_name if user else "Unknown"
    
    # Check for !wadd command
    wadd_match_with_user = WADD_PATTERN_WITH_USER.match(text)
    wadd_match_no_user = WADD_PATTERN_NO_USER.match(text)
    
    if wadd_match_with_user:
        url = wadd_match_with_user.group(1)
        assigned_to = wadd_match_with_user.group(2)
        await handle_wadd(update, chat_id, url, assigned_to, created_by)
        return
    elif wadd_match_no_user:
        url = wadd_match_no_user.group(1)
        await handle_wadd(update, chat_id, url, None, created_by)
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
        task_id = wdone_match.group(1).strip()
        await handle_wdone(update, chat_id, task_id)
        return
    elif WDONE_PREFIX.match(text):
        await update.message.reply_text(
            "Usage: <code>!wdone &lt;N or task_id&gt;</code>\n"
            "Examples: <code>!wdone 1</code> or <code>!wdone repo/merge_requests/123</code>",
            parse_mode=ParseMode.HTML
        )
        return
    
    # Check for !whelp command
    if WHELP_PATTERN.match(text):
        await handle_whelp(update)
        return


async def handle_wadd(update: Update, chat_id: int, url: str, assigned_to: Optional[str], created_by: str) -> None:
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
    
    assigned_to_formatted = f"@{assigned_to}" if assigned_to else "unassigned"
    seq_num = db.add_task(chat_id, task_id, url, assigned_to_formatted, created_by)
    
    if seq_num is None:
        await update.message.reply_text(f"Task {task_id} already exists in the queue.")
        return
    
    if assigned_to:
        response = f'[#{seq_num}] <a href="{html_escape(url)}">{html_escape(task_id)}</a> → {html_escape(assigned_to_formatted)}'
    else:
        response = f'[#{seq_num}] <a href="{html_escape(url)}">{html_escape(task_id)}</a>'
    await update.message.reply_text(response, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    logger.info(f"Added task {task_id} in chat {chat_id}: {url} -> {assigned_to_formatted}")


async def handle_w(update: Update, chat_id: int) -> None:
    """Handle !w command - list all tasks."""
    tasks = db.get_tasks(chat_id)
    
    if not tasks:
        await update.message.reply_text("No tasks in the queue.")
        return
    
    lines = []
    for t in tasks:
        if t.assigned_to and t.assigned_to != "unassigned":
            lines.append(f'[#{t.seq_num}] <a href="{html_escape(t.url)}">{html_escape(t.task_id)}</a> → {html_escape(t.assigned_to)} (by {html_escape(t.created_by)})')
        else:
            lines.append(f'[#{t.seq_num}] <a href="{html_escape(t.url)}">{html_escape(t.task_id)}</a> (by {html_escape(t.created_by)})')
    
    response = "\n".join(lines)
    await update.message.reply_text(response, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def handle_wdone(update: Update, chat_id: int, task_ref: str) -> None:
    """Handle !wdone command - remove a task by sequence number or task_id."""
    # Try to parse as sequence number first
    if task_ref.isdigit():
        removed_task = db.remove_task_by_seq(chat_id, int(task_ref))
    else:
        removed_task = db.remove_task_by_id(chat_id, task_ref)
    
    if removed_task is None:
        await update.message.reply_text(f"Task {task_ref} not found.")
        return
    
    response = f'Removed [#{removed_task.seq_num}] <a href="{html_escape(removed_task.url)}">{html_escape(removed_task.task_id)}</a> (added by {html_escape(removed_task.created_by)})'
    await update.message.reply_text(response, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    logger.info(f"Removed task #{removed_task.seq_num} ({removed_task.task_id}) from chat {chat_id}")


async def handle_whelp(update: Update) -> None:
    """Handle !whelp command - display help instructions."""
    help_text = """<b>Work Queue Commands</b>

<code>!wadd &lt;URL&gt; [@username]</code>
Add a merge request (optionally assign to user)
Examples:
• <code>!wadd http://gitlab.example.com/group/repo/-/merge_requests/123</code>
• <code>!wadd http://gitlab.example.com/group/repo/-/merge_requests/123 @alice</code>

<code>!w</code>
List all tasks in the queue

<code>!wdone &lt;#N or task_id&gt;</code>
Remove a completed task by number or ID
Examples: <code>!wdone 1</code> or <code>!wdone repo/merge_requests/123</code>

<code>!whelp</code>
Show this help message

<b>Supported URLs:</b>
• GitLab: <code>http://host/group/project/-/merge_requests/N</code>
• GitHub: <code>https://github.com/owner/repo/pull/N</code>"""
    
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)


def main() -> None:
    """Start the bot."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set in environment variables")
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")
    
    # Create application
    application = Application.builder().token(token).build()
    
    # Add message handler for all text messages
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    
    # Start polling
    logger.info("Starting bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
