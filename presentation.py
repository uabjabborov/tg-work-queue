from html import escape as html_escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database import Task


def review_link(task_id: str, url: str) -> str:
    return f'<a href="{html_escape(url)}">{html_escape(task_id)}</a>'


def task_listing(task: "Task") -> str:
    entry = review_link(task.task_id, task.url)
    if task.assignees:
        entry += " → " + ", ".join(html_escape(assignee) for assignee in task.assignees)
    return f"{entry} (by {html_escape(task.created_by)})"
