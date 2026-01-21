import logging
from typing import TYPE_CHECKING
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram.constants import ParseMode
from html import escape as html_escape

if TYPE_CHECKING:
    from telegram.ext import Application
    from database import Database

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    """Get or create the global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler()
    return _scheduler


async def send_reminder(chat_id: int, application: "Application", db: "Database") -> None:
    """Send a reminder message with pending tasks to a chat."""
    try:
        tasks = db.get_tasks(chat_id)
        
        # Only send if there are pending tasks
        if not tasks:
            logger.info(f"No pending tasks for chat {chat_id}, skipping reminder")
            return
        
        # Format message similar to handle_w()
        lines = ["<b>📋 Reminder: Pending Reviews</b>\n"]
        for t in tasks:
            if t.assigned_to and t.assigned_to != "unassigned":
                lines.append(
                    f'[#{t.seq_num}] <a href="{html_escape(t.url)}">{html_escape(t.task_id)}</a> → '
                    f'{html_escape(t.assigned_to)} (by {html_escape(t.created_by)})'
                )
            else:
                lines.append(
                    f'[#{t.seq_num}] <a href="{html_escape(t.url)}">{html_escape(t.task_id)}</a> '
                    f'(by {html_escape(t.created_by)})'
                )
        
        message = "\n".join(lines)
        await application.bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
        logger.info(f"Sent reminder to chat {chat_id} with {len(tasks)} task(s)")
        
    except Exception as e:
        logger.error(f"Error sending reminder to chat {chat_id}: {e}", exc_info=True)


def add_reminder_job(
    chat_id: int,
    cron_expression: str,
    application: "Application",
    db: "Database"
) -> None:
    """Add or update a cron job for a chat's reminder."""
    scheduler = get_scheduler()
    job_id = f"reminder_{chat_id}"
    
    # Remove existing job if present
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    
    # Parse cron expression (5-part format: minute hour day month day_of_week)
    parts = cron_expression.split()
    if len(parts) != 5:
        raise ValueError("Cron expression must have 5 parts: minute hour day month day_of_week")
    
    minute, hour, day, month, day_of_week = parts
    
    # Create cron trigger
    trigger = CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
        timezone='UTC'
    )
    
    # Add job
    scheduler.add_job(
        send_reminder,
        trigger=trigger,
        args=[chat_id, application, db],
        id=job_id,
        name=f"Reminder for chat {chat_id}",
        replace_existing=True
    )
    
    logger.info(f"Added reminder job for chat {chat_id}: {cron_expression}")


def remove_reminder_job(chat_id: int) -> None:
    """Remove a cron job for a chat's reminder."""
    scheduler = get_scheduler()
    job_id = f"reminder_{chat_id}"
    
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logger.info(f"Removed reminder job for chat {chat_id}")


def load_existing_reminders(application: "Application", db: "Database") -> None:
    """Load all active reminders from database and schedule them."""
    reminders = db.get_all_active_reminders()
    
    for reminder in reminders:
        try:
            add_reminder_job(
                reminder.chat_id,
                reminder.cron_expression,
                application,
                db
            )
            logger.info(f"Loaded reminder for chat {reminder.chat_id}: {reminder.cron_expression}")
        except Exception as e:
            logger.error(
                f"Failed to load reminder for chat {reminder.chat_id}: {e}",
                exc_info=True
            )
    
    logger.info(f"Loaded {len(reminders)} active reminder(s)")


def setup_scheduler(application: "Application", db: "Database") -> AsyncIOScheduler:
    """Initialize and start the scheduler with existing reminders."""
    scheduler = get_scheduler()
    
    # Load existing reminders from database
    load_existing_reminders(application, db)
    
    # Start the scheduler if not already running
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started")
    
    return scheduler
