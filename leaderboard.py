"""Tashkent reporting weeks and shared contributor/reviewer standings."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

from database import Database


TIMEZONE_NAME = "Asia/Tashkent"
TASHKENT = ZoneInfo(TIMEZONE_NAME)
UNKNOWN_NAMES = {"", "unknown", "anonymous", "@groupanonymousbot", "groupanonymousbot"}
PersonKey = tuple[str, int | str]
ReviewIdentity = tuple[str, str, str]


def local_now(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required")
    return now.astimezone(TASHKENT)


@dataclass(frozen=True)
class Week:
    start: datetime
    end: datetime

    @property
    def start_utc(self) -> datetime:
        return self.start.astimezone(timezone.utc)

    @property
    def end_utc(self) -> datetime:
        return self.end.astimezone(timezone.utc)


def reporting_week(now: datetime | None = None, previous: bool = False) -> Week:
    """Monday 00:00 through the next Monday 00:00, in Tashkent."""
    local = local_now(now)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=local.weekday())
    if previous:
        start -= timedelta(weeks=1)
    return Week(start, start + timedelta(weeks=1))


def most_recent_recap(now: datetime | None = None) -> tuple[Week, datetime]:
    """Return only the most recent due report; the current report is due at 09:00."""
    local = local_now(now)
    due = reporting_week(local).start.replace(hour=9)
    if local < due:
        due -= timedelta(weeks=1)
    week = reporting_week(due, previous=True)
    return week, due


@dataclass(frozen=True)
class RankingEntry:
    name: str
    count: int


@dataclass
class Leaderboard:
    week: Week
    contributors: list[RankingEntry]
    reviewers: list[RankingEntry]

    @property
    def empty(self) -> bool:
        return not self.contributors and not self.reviewers


def normalized_name(name: str) -> str:
    return name.strip().casefold()


class Identities:
    """Keep all aliases for conservative legacy matching and the latest display name."""

    def __init__(self, known_users: list[tuple[int, str]]):
        self.ids_by_name: dict[str, set[int]] = defaultdict(set)
        self.names_by_id: dict[int, str] = {}
        for user_id, name in known_users:
            self.names_by_id[user_id] = name
            if normalized_name(name) not in UNKNOWN_NAMES:
                self.ids_by_name[normalized_name(name)].add(user_id)

    def resolve(self, user_id: int | None, name: str) -> tuple[PersonKey, str] | None:
        if user_id is not None:
            return ("user", user_id), self.names_by_id.get(user_id, name)
        normalized = normalized_name(name)
        if normalized in UNKNOWN_NAMES:
            return None
        matches = self.ids_by_name.get(normalized, set())
        if len(matches) == 1:
            user_id = next(iter(matches))
            return ("user", user_id), self.names_by_id.get(user_id, name)
        return ("legacy", normalized), name

    def is_self_review(self, submitter_id: int | None, submitter: str,
                       reviewer_id: int | None, reviewer: str) -> bool:
        if submitter_id is not None and reviewer_id is not None:
            return submitter_id == reviewer_id
        author = self.resolve(submitter_id, submitter)
        sender = self.resolve(reviewer_id, reviewer)
        if not author or not sender:
            return False
        if author[0][0] == sender[0][0] == "user":
            return author[0] == sender[0]
        return normalized_name(submitter) == normalized_name(reviewer)


def build_leaderboard(db: Database, chat_id: int, week: Week) -> Leaderboard:
    identities = Identities(db.get_known_users(chat_id))
    names: dict[PersonKey, str] = {}
    contributors: dict[PersonKey, set[ReviewIdentity]] = defaultdict(set)
    reviewers: dict[PersonKey, set[ReviewIdentity]] = defaultdict(set)
    for event in db.get_review_activity(chat_id, week.start_utc, week.end_utc):
        if event.review_identity is None:
            continue
        author = identities.resolve(event.created_by_id, event.created_by)
        sender = identities.resolve(event.completed_by_id, event.completed_by)
        if event.outcome == "done" and author:
            key, name = author
            names[key] = name
            contributors[key].add(event.review_identity)
        if sender and not identities.is_self_review(
            event.created_by_id, event.created_by, event.completed_by_id, event.completed_by
        ):
            key, name = sender
            names[key] = name
            reviewers[key].add(event.review_identity)

    def rank(points: dict[PersonKey, set[ReviewIdentity]]) -> list[RankingEntry]:
        entries = [RankingEntry(names[key], len(reviews)) for key, reviews in points.items()]
        return sorted(entries, key=lambda entry: (-entry.count, entry.name.casefold(), entry.name))[:5]

    return Leaderboard(week, rank(contributors), rank(reviewers))


def format_leaderboard(board: Leaderboard) -> str:
    last_day = board.week.end - timedelta(days=1)
    lines = ["<b>Weekly Leaderboard</b>",
             f"{board.week.start:%Y-%m-%d} – {last_day:%Y-%m-%d} ({TIMEZONE_NAME})"]
    for title, entries in (("Contributors", board.contributors), ("Reviewers", board.reviewers)):
        lines.extend(["", f"<b>{title}</b>"])
        if entries:
            lines.extend(f"{position}. {escape(entry.name)} — {entry.count}"
                         for position, entry in enumerate(entries, start=1))
        else:
            lines.append(f"No {title.lower().rstrip('s')} points for this period.")
    return "\n".join(lines)
