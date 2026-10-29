import re
from html import escape as html_escape, unescape as html_unescape
from typing import TYPE_CHECKING, Callable, Optional

from leaderboard import TASHKENT

if TYPE_CHECKING:
    from database import FinishedReview, Task

# Telegram's limit applies to the visible text after entity parsing, counted in UTF-16 code units.
MESSAGE_LIMIT = 4096
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
# By then a bounced PR/MR that wasn't re-added was probably closed or replaced, and an approved one merged.
FINISHED_VISIBLE_DAYS = 14


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


def finished_listing(review: "FinishedReview") -> str:
    verb = "approved" if review.outcome == "done" else "bounced"
    finished_on = review.completed_at.astimezone(TASHKENT).strftime("%b %d")
    return (f"{review_link(review.task_id, review.url)} — {verb} by "
            f"{html_escape(review.completed_by)} on {finished_on}")


def grouped_by_chat(items: list, listing: Callable, chat_titles: Optional[dict[int, str]]) -> list[str]:
    """List items, under a line naming each chat when they come from several chats."""
    if chat_titles is None:
        return [listing(item) for item in items]
    blocks = []
    for chat_id in dict.fromkeys(item.chat_id for item in items):
        blocks.append(f"<i>{html_escape(chat_titles.get(chat_id, f'Chat {chat_id}'))}</i>")
        blocks += [listing(item) for item in items if item.chat_id == chat_id]
    return blocks


def my_reviews(assigned: list["Task"], finished: list["FinishedReview"], has_username: bool = True,
               chat_titles: Optional[dict[int, str]] = None, unchecked_chats: int = 0) -> list[str]:
    """The !wme reply: reviews waiting on you, then your PRs/MRs to merge, then ones that need changes.

    `chat_titles` names each chat when the reply covers several chats.
    """
    blocks = []
    if assigned:
        blocks.append(f"<b>Assigned to you ({len(assigned)})</b>")
        blocks += grouped_by_chat(assigned, task_listing, chat_titles)
    elif has_username:
        blocks.append("No reviews are assigned to you.")
    else:
        blocks.append("Set a Telegram username to see reviews assigned to you. Reviews are assigned by @username.")
    approved = [review for review in finished if review.outcome == "done"]
    if approved:
        blocks.append(f"\n<b>Approved, ready to merge ({len(approved)})</b>")
        blocks += grouped_by_chat(approved, finished_listing, chat_titles)
        blocks.append("<i>After merging, send <code>!wmerged &lt;PR/MR reference&gt;</code> in its chat.</i>")
    bounced = [review for review in finished if review.outcome == "bounce"]
    if bounced:
        blocks.append(f"\n<b>Your PRs/MRs that need changes ({len(bounced)})</b>")
        blocks += grouped_by_chat(bounced, finished_listing, chat_titles)
        blocks.append("<i>Re-add one with <code>!wadd</code> once it's fixed.</i>")
    if finished:
        blocks.append(f"<i>Approvals and bounces older than {FINISHED_VISIBLE_DAYS} days are hidden.</i>")
    if unchecked_chats:
        groups = "1 of your groups" if unchecked_chats == 1 else f"{unchecked_chats} of your groups"
        blocks.append(f"\n⚠️ I couldn't check {groups} right now, so some reviews may be missing. "
                      "Send <code>!wme</code> again later.")
    return split_messages(blocks)
