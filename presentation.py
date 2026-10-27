from html import escape as html_escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database import Task


def review_link(task_id: str, url: str) -> str:
    return f'<a href="{html_escape(url)}">{html_escape(task_id)}</a>'


def review_listing(task_id: str, url: str, assignees: list[str], created_by: str) -> str:
    entry = review_link(task_id, url)
    if assignees:
        entry += " → " + ", ".join(html_escape(assignee) for assignee in assignees)
    return f"{entry} (by {html_escape(created_by)})"


def task_listing(task: "Task") -> str:
    return review_listing(task.task_id, task.url, task.assignees, task.created_by)
