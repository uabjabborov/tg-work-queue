from html import escape as html_escape
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from database import Task

# Telegram rejects longer messages; raw HTML is never shorter than the parsed text.
MESSAGE_LIMIT = 4096
TITLE_LIMIT = 120
DESCRIPTION_PREVIEW_LIMIT = 300


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def review_link(task_id: str, url: str) -> str:
    return f'<a href="{html_escape(url)}">{html_escape(task_id)}</a>'


def review_heading(task_id: str, url: str, title: Optional[str]) -> str:
    heading = review_link(task_id, url)
    if title:
        heading += f" — {html_escape(shorten(title, TITLE_LIMIT))}"
    return heading


def review_listing(task_id: str, url: str, assignees: list[str], created_by: str,
                   title: Optional[str] = None) -> str:
    entry = review_heading(task_id, url, title)
    if assignees:
        entry += " → " + ", ".join(html_escape(assignee) for assignee in assignees)
    return f"{entry} (by {html_escape(created_by)})"


def task_listing(task: "Task") -> str:
    return review_listing(task.task_id, task.url, task.assignees, task.created_by, task.title)


def assigned_reviews(tasks: list["Task"]) -> str:
    """List one person's reviews with collapsed description previews when they fit."""
    if not tasks:
        return "No reviews are assigned to you in this chat."
    header = f"<b>Assigned to you ({len(tasks)})</b>"
    detailed = [header] + [
        task_listing(task) + (
            f"\n<blockquote expandable>{html_escape(shorten(task.description, DESCRIPTION_PREVIEW_LIMIT))}</blockquote>"
            if task.description else ""
        )
        for task in tasks
    ]
    message = "\n\n".join(detailed)
    if len(message) <= MESSAGE_LIMIT:
        return message

    lines = [header]
    for shown, task in enumerate(tasks):
        remaining = f"…and {len(tasks) - shown} more. Use <code>!w</code> for the full queue."
        if len("\n".join(lines + [task_listing(task), remaining])) > MESSAGE_LIMIT:
            return "\n".join(lines + [remaining])
        lines.append(task_listing(task))
    return "\n".join(lines)
