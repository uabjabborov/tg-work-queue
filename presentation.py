import re
from html import escape as html_escape, unescape as html_unescape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database import Task

# Telegram's limit applies to the visible text after entity parsing, counted in UTF-16 code units.
MESSAGE_LIMIT = 4096
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


def review_link(task_id: str, url: str) -> str:
    return f'<a href="{html_escape(url)}">{html_escape(task_id)}</a>'


def task_listing(task: "Task") -> str:
    entry = review_link(task.task_id, task.url)
    if task.assignees:
        entry += " → " + ", ".join(html_escape(assignee) for assignee in task.assignees)
    return f"{entry} (by {html_escape(task.created_by)})"


def visible_length(message: str) -> int:
    """Length Telegram checks: tags and link targets are not counted, entities count once."""
    return len(html_unescape(HTML_TAG_PATTERN.sub("", message)).encode("utf-16-le")) // 2


def split_messages(blocks: list[str], separator: str = "\n") -> list[str]:
    """Pack blocks into as few messages as fit under Telegram's limit, never splitting a block."""
    messages = []
    for block in blocks:
        if messages and visible_length(messages[-1] + separator + block) <= MESSAGE_LIMIT:
            messages[-1] += separator + block
        else:
            messages.append(block)
    return messages


def assigned_reviews(tasks: list["Task"]) -> list[str]:
    if not tasks:
        return ["No reviews are assigned to you."]
    return split_messages([f"<b>Assigned to you ({len(tasks)})</b>"] + [task_listing(task) for task in tasks])
