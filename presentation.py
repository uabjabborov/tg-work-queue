import re
from html import escape as html_escape, unescape as html_unescape
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from database import Task

# Telegram's limit applies to the visible text after entity parsing, counted in UTF-16 code units.
MESSAGE_LIMIT = 4096
TITLE_DISPLAY_LIMIT = 120
DESCRIPTION_PREVIEW_LIMIT = 300
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def review_link(task_id: str, url: str) -> str:
    return f'<a href="{html_escape(url)}">{html_escape(task_id)}</a>'


def review_heading(task_id: str, url: str, title: Optional[str]) -> str:
    heading = review_link(task_id, url)
    if title:
        heading += f" — {html_escape(shorten(title, TITLE_DISPLAY_LIMIT))}"
    return heading


def review_listing(task_id: str, url: str, assignees: list[str], created_by: str,
                   title: Optional[str] = None) -> str:
    entry = review_heading(task_id, url, title)
    if assignees:
        entry += " → " + ", ".join(html_escape(assignee) for assignee in assignees)
    return f"{entry} (by {html_escape(created_by)})"


def task_listing(task: "Task") -> str:
    return review_listing(task.task_id, task.url, task.assignees, task.created_by, task.title)


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
    """List one person's reviews with collapsed description previews, split to fit Telegram."""
    if not tasks:
        return ["No reviews are assigned to you."]
    entries = [
        task_listing(task) + (
            f"\n<blockquote expandable>{html_escape(shorten(task.description, DESCRIPTION_PREVIEW_LIMIT))}</blockquote>"
            if task.description else ""
        )
        for task in tasks
    ]
    return split_messages([f"<b>Assigned to you ({len(tasks)})</b>"] + entries, separator="\n\n")
