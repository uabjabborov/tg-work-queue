import os
import re
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode

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
WADD_PATTERN = re.compile(r"^!wadd\s+(https?://\S+)\s+@(\w+)$", re.IGNORECASE)
W_PATTERN = re.compile(r"^!w$", re.IGNORECASE)
WDONE_PATTERN = re.compile(r"^!wdone\s+(.+)$", re.IGNORECASE)
WHELP_PATTERN = re.compile(r"^!whelp$", re.IGNORECASE)

# Patterns for extracting task ID from MR/PR URLs
# GitLab: http://host/group/project/-/merge_requests/123
GITLAB_MR_PATTERN = re.compile(r"https?://[^/]+/(?:.+?/)*([^/]+)/-/merge_requests/(\d+)")
# GitHub: https://github.com/owner/repo/pull/123
GITHUB_PR_PATTERN = re.compile(r"https?://github\.com/[^/]+/([^/]+)/pull/(\d+)")


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
    wadd_match = WADD_PATTERN.match(text)
    if wadd_match:
        url = wadd_match.group(1)
        assigned_to = wadd_match.group(2)
        await handle_wadd(update, chat_id, url, assigned_to, created_by)
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
    
    # Check for !whelp command
    if WHELP_PATTERN.match(text):
        await handle_whelp(update)
        return


async def handle_wadd(update: Update, chat_id: int, url: str, assigned_to: str, created_by: str) -> None:
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
    
    assigned_to_formatted = f"@{assigned_to}"
    added = db.add_task(chat_id, task_id, url, assigned_to_formatted, created_by)
    
    if not added:
        await update.message.reply_text(f"Task {task_id} already exists in the queue.")
        return
    
    response = f"[{task_id}]({url}) → {assigned_to_formatted}"
    await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    logger.info(f"Added task {task_id} in chat {chat_id}: {url} -> {assigned_to_formatted}")


async def handle_w(update: Update, chat_id: int) -> None:
    """Handle !w command - list all tasks."""
    tasks = db.get_tasks(chat_id)
    
    if not tasks:
        await update.message.reply_text("No tasks in the queue.")
        return
    
    lines = []
    for t in tasks:
        lines.append(f"[{t.task_id}]({t.url}) → {t.assigned_to} (by {t.created_by})")
    
    response = "\n".join(lines)
    await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)


async def handle_wdone(update: Update, chat_id: int, task_id: str) -> None:
    """Handle !wdone command - remove a task."""
    removed_task = db.remove_task(chat_id, task_id)
    
    if removed_task is None:
        await update.message.reply_text(f"Task {task_id} not found.")
        return
    
    response = f"Removed [{task_id}]({removed_task.url}) (added by {removed_task.created_by})"
    await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    logger.info(f"Removed task {task_id} from chat {chat_id}")


async def handle_whelp(update: Update) -> None:
    """Handle !whelp command - display help instructions."""
    help_text = """*Work Queue Commands*

`!wadd <URL> @username`
Add a merge request and assign to user
Example: `!wadd http://gitlab.example.com/group/repo/-/merge_requests/123 @alice`

`!w`
List all tasks in the queue

`!wdone <task_id>`
Remove a completed task
Example: `!wdone repo/merge_requests/123`

`!whelp`
Show this help message

*Supported URLs:*
• GitLab: `http://host/group/project/-/merge_requests/N`
• GitHub: `https://github.com/owner/repo/pull/N`"""
    
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)


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
