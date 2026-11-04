import re
from html import escape as html_escape, unescape as html_unescape
from html.parser import HTMLParser
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


def text_preview(text: str, limit: int) -> str:
    """Truncate plain text before escaping so entities and UTF-16 pairs stay intact."""
    text = text.strip()
    encoded = text.encode("utf-16-le")
    if len(encoded) // 2 > limit:
        return encoded[:(limit - 1) * 2].decode("utf-16-le", errors="ignore").rstrip() + "…"
    return text


def review_heading(review: "Task | FinishedReview") -> str:
    entry = review_link(review.task_id, review.url)
    if review.title:
        entry += " — <b>" + html_escape(text_preview(" ".join(review.title.split()), 240)) + "</b>"
    return entry


def description_preview(review: "Task | FinishedReview") -> str:
    if review.description:
        return "\n" + html_escape(text_preview(review.description, 1200))
    return ""


def task_listing(task: "Task", include_description: bool = False) -> str:
    entry = review_heading(task)
    if task.assignees:
        entry += " → " + ", ".join(html_escape(assignee) for assignee in task.assignees)
    entry += f" (by {html_escape(task.created_by)})"
    return entry + (description_preview(task) if include_description else "")


def visible_length(message: str) -> int:
    """Length Telegram checks: tags and link targets are not counted, entities count once."""
    return len(html_unescape(HTML_TAG_PATTERN.sub("", message)).encode("utf-16-le")) // 2


class _HTMLSplitter(HTMLParser):
    """Split generated Telegram HTML, closing and reopening formatting at boundaries."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.messages = []
        self.parts = []
        self.tags = []
        self.length = 0

    def flush(self):
        self.messages.append("".join(self.parts) + "".join(f"</{tag}>" for tag, _ in reversed(self.tags)))
        self.parts = [opening for _, opening in self.tags]
        self.length = 0

    def handle_starttag(self, tag, attrs):
        opening = self.get_starttag_text()
        self.tags.append((tag, opening))
        self.parts.append(opening)

    def handle_endtag(self, tag):
        self.tags.pop()
        self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        for char in data:
            size = len(char.encode("utf-16-le")) // 2
            if self.length + size > MESSAGE_LIMIT:
                self.flush()
            self.parts.append(html_escape(char, quote=False))
            self.length += size

    def handle_entityref(self, name):
        self.handle_data(html_unescape(f"&{name};"))

    def handle_charref(self, name):
        self.handle_data(html_unescape(f"&#{name};"))


def split_messages(blocks: list[str], separator: str = "\n") -> list[str]:
    """Keep entries together where possible; safely split an entry that exceeds the limit."""
    messages = []
    for block in blocks:
        pieces = [block]
        if visible_length(block) > MESSAGE_LIMIT:
            splitter = _HTMLSplitter()
            splitter.feed(block)
            splitter.close()
            if splitter.length:
                splitter.flush()
            pieces = splitter.messages
        for index, piece in enumerate(pieces):
            # Only insert separators between original blocks, not inside an entry.
            joiner = separator if index == 0 else ""
            if messages and visible_length(messages[-1] + joiner + piece) <= MESSAGE_LIMIT:
                messages[-1] += joiner + piece
            else:
                messages.append(piece)
    return messages


def finished_listing(review: "FinishedReview", include_description: bool = False) -> str:
    verb = "approved" if review.outcome == "done" else "bounced"
    finished_on = review.completed_at.astimezone(TASHKENT).strftime("%b %d")
    return (f"{review_heading(review)} — {verb} by "
            f"{html_escape(review.completed_by)} on {finished_on}"
            + (description_preview(review) if include_description else ""))


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
        blocks += grouped_by_chat(assigned, lambda task: task_listing(task, include_description=True), chat_titles)
    elif has_username:
        blocks.append("No reviews are assigned to you.")
    else:
        blocks.append("Set a Telegram username to see reviews assigned to you. Reviews are assigned by @username.")
    approved = [review for review in finished if review.outcome == "done"]
    if approved:
        blocks.append(f"\n<b>Approved, ready to merge ({len(approved)})</b>")
        blocks += grouped_by_chat(approved, lambda review: finished_listing(review, include_description=True), chat_titles)
        blocks.append("<i>After merging, send <code>!wmerged &lt;PR/MR reference&gt;</code> in its chat.</i>")
    bounced = [review for review in finished if review.outcome == "bounce"]
    if bounced:
        blocks.append(f"\n<b>Your PRs/MRs that need changes ({len(bounced)})</b>")
        blocks += grouped_by_chat(bounced, lambda review: finished_listing(review, include_description=True), chat_titles)
        blocks.append("<i>Re-add one with <code>!wadd</code> once it's fixed.</i>")
    if finished:
        blocks.append(f"<i>Approvals and bounces older than {FINISHED_VISIBLE_DAYS} days are hidden. "
                      "Clear them sooner with <code>!wclear &lt;PR/MR references&gt;</code> or <code>!wclear all</code>.</i>")
    if unchecked_chats:
        groups = "1 of your groups" if unchecked_chats == 1 else f"{unchecked_chats} of your groups"
        blocks.append(f"\n⚠️ I couldn't check {groups} right now, so some reviews may be missing. "
                      "Send <code>!wme</code> again later.")
    return split_messages(blocks)
