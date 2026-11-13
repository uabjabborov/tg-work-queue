"""Playful private messages ("cheers") for reviewers after !wdone and !wbounce.

Each cheer has a factual header (which review, how fast, which bounce round) and a quip. Quips are
mixed for free from the English, Uzbek and Russian lines in cheer_lines.py. If ANTHROPIC_API_KEY is
set, Claude writes them instead (a paid API), with the templates as fallback. Only the review's speed tier and counts are sent to Claude,
never names, chats or links.
"""

import logging
import os
import random
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from typing import Optional

from cheer_lines import (
    BODIES, CALL_TO_ACTION, CLOSERS, COMBOS, EMPTY_QUEUE, NOTHING_TO_REVIEW, OPENERS, SLOTS, TIER_OPENERS,
)
from presentation import review_link

logger = logging.getLogger(__name__)

# Speed tiers for approvals, by the time from !wadd to !wdone of the fastest review in the command.
SPEED_TIERS = (
    (timedelta(minutes=10), "flash"),
    (timedelta(hours=1), "rocket"),
    (timedelta(hours=4), "steady"),
    (timedelta(hours=24), "turtle"),
)
SLOWEST_TIER = "fossil"
TIER_EMOJI = {
    "flash": "⚡", "rocket": "🚀", "steady": "💪", "turtle": "🐢", "fossil": "🦖",
    "guard": "🛡️", "round2": "🥊", "marathon": "🔁",
}
# A command covering at least this many reviews also gets a combo line.
COMBO_SIZE = 3
# Quips a person saw recently are avoided, so the same line doesn't come back soon.
RECENT_PER_PERSON = 12
MAX_QUIP_LENGTH = 300
# The public "queue is empty" celebration is posted at most this often per chat, so a quiet chat where
# every !wdone empties the queue doesn't get one each time.
EMPTY_QUEUE_COOLDOWN = timedelta(hours=1)
# Every public empty-queue message ends with a call to action and this command.
ADD_COMMAND = "<code>!wadd &lt;PR/MR link&gt;</code>"
# Optional Claude-written quips: the smallest current model is plenty for one line, and keeps the cost
# per cheer tiny. CHEERS_MODEL picks another model. max_tokens caps spend, with room for brief thinking.
DEFAULT_MODEL = "claude-haiku-5-5"
AI_MAX_TOKENS = 1024
AI_TIMEOUT_SECONDS = 8.0


@dataclass(frozen=True)
class Cheer:
    """What happened, reduced to the facts a quip needs."""
    outcome: str                  # "done" or "bounce"
    tier: str
    count: int                    # reviews in the command that count for this reviewer
    task_id: str                  # the highlighted review: fastest approval or most-bounced PR/MR
    url: str
    wait: Optional[timedelta]     # for approvals: queue time of the highlighted review
    bounce_round: int = 0         # for bounces: how often the highlighted PR/MR was sent back, this time included


def parse_queue_time(value) -> Optional[datetime]:
    """Queue timestamps are ISO strings; rows from before timezone-aware timestamps are naive UTC."""
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def queue_wait(created_at, now: datetime) -> Optional[timedelta]:
    queued = parse_queue_time(created_at)
    if queued is None or queued > now:
        return None
    return now - queued


def speed_tier(wait: Optional[timedelta]) -> str:
    if wait is None:
        return "steady"
    for limit, tier in SPEED_TIERS:
        if wait < limit:
            return tier
    return SLOWEST_TIER


def bounce_tier(bounce_round: int) -> str:
    if bounce_round <= 1:
        return "guard"
    return "round2" if bounce_round == 2 else "marathon"


def format_wait(wait: timedelta) -> str:
    seconds = int(wait.total_seconds())
    if seconds < 60:
        return f"{seconds} sec"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


def approval_cheer(reviews: list[tuple[str, str, Optional[timedelta]]]) -> Optional[Cheer]:
    """reviews: (task_id, url, queue wait) of every approval the reviewer gets credit for."""
    if not reviews:
        return None
    timed = [review for review in reviews if review[2] is not None]
    task_id, url, wait = min(timed, key=lambda review: review[2]) if timed else reviews[0]
    return Cheer("done", speed_tier(wait), len(reviews), task_id, url, wait)


def bounce_cheer(reviews: list[tuple[str, str, int]]) -> Optional[Cheer]:
    """reviews: (task_id, url, bounce round) of every bounce the reviewer gets credit for."""
    if not reviews:
        return None
    task_id, url, bounce_round = max(reviews, key=lambda review: review[2])
    return Cheer("bounce", bounce_tier(bounce_round), len(reviews), task_id, url, None, bounce_round)


def header(cheer: Cheer) -> str:
    """The factual first line, in HTML."""
    parts = []
    if cheer.count > 1:
        parts.append(f"×{cheer.count}")
    parts.append(review_link(cheer.task_id, cheer.url))
    if cheer.outcome == "done" and cheer.wait is not None:
        parts.append(format_wait(cheer.wait))
    elif cheer.outcome == "bounce" and cheer.bounce_round > 1:
        parts.append(f"round {cheer.bounce_round}")
    return f"{TIER_EMOJI[cheer.tier]} " + " · ".join(parts)


def pieces(pool: dict[str, list[str]], cheer: Cheer) -> list[str]:
    """All lines of a pool, in every language, that this cheer has the facts for."""
    lines = [line for language in pool.values() for line in language]
    return [line for line in lines if cheer.wait is not None or "{time}" not in line]


def fill(line: str, cheer: Cheer, rng: random.Random) -> str:
    slots = {name: rng.choice(options) for name, options in SLOTS.items()}
    return line.format(time=format_wait(cheer.wait) if cheer.wait is not None else "",
                       count=cheer.count, round=cheer.bounce_round, **slots)


def template_quip(cheer: Cheer, rng: random.Random, recent: Optional[deque] = None) -> str:
    """Mix an opener, a tier line, an optional combo line and a closer, each in any of the languages."""
    recent = recent if recent is not None else deque()
    bodies = pieces(BODIES[cheer.tier], cheer)
    body = rng.choice([line for line in bodies if line not in recent] or bodies)
    recent.append(body)

    parts = []
    if rng.random() < 0.7:
        parts.append(rng.choice(pieces(OPENERS[TIER_OPENERS[cheer.tier]], cheer)))
    parts.append(body)
    if cheer.count >= COMBO_SIZE:
        parts.append(rng.choice(pieces(COMBOS[cheer.outcome], cheer)))
    if rng.random() < 0.6:
        parts.append(rng.choice(pieces(CLOSERS[cheer.outcome], cheer)))
    return " ".join(fill(part, cheer, rng) for part in parts)


def public_quip(pool: dict[str, list[str]], rng: random.Random, recent: Optional[deque] = None,
                opener_mood: Optional[str] = None) -> str:
    """A line from a public pool in any language, not one the chat saw recently, sometimes with an opener."""
    recent = recent if recent is not None else deque()
    lines = [line for language in pool.values() for line in language]
    line = rng.choice([line for line in lines if line not in recent] or lines)
    recent.append(line)
    parts = [line]
    if opener_mood and rng.random() < 0.5:
        parts.insert(0, rng.choice([opener for language in OPENERS[opener_mood].values() for opener in language]))
    slots = {name: rng.choice(options) for name, options in SLOTS.items()}
    return " ".join(part.format(**slots) for part in parts)


def empty_queue_quip(rng: random.Random, recent: Optional[deque] = None) -> str:
    return public_quip(EMPTY_QUEUE, rng, recent, opener_mood="hype")


AI_SYSTEM_PROMPT = """You write one short, playful private message to a software engineer in a team's Telegram chat, \
right after they finished a code review. Only they will see it.

Write like developers in Tashkent chat: mix Uzbek (Latin script), Russian and English, switching languages \
within the message. Keep it to one or two sentences and under 220 characters, with at most two emoji. Be warm \
and funny; jokes about programming, pop culture, sports, food or Uzbek proverbs are welcome. For example, a \
reviewer who approved something within minutes might be asked whether their real name is Barry Allen.

Never shame anyone. A slow review gets gentle teasing that still thanks them; a review that sent a PR back \
praises the care for quality, and a PR sent back many times can suggest a quick call with the author. \
Do not mention names, links or PR numbers, and do not invent facts beyond those given.

Reply with the message only: no quotes, no markdown, no explanation."""

TIER_DESCRIPTIONS = {
    "flash": "lightning fast (under 10 minutes)",
    "rocket": "very fast (under an hour)",
    "steady": "reasonably quick (same work session)",
    "turtle": "slow (several hours)",
    "fossil": "very slow (more than a day)",
}
# A random theme per request keeps Claude's quips from converging on the same jokes.
AI_THEMES = (
    "superheroes", "space travel", "football", "cooking plov", "chess", "video games", "movies",
    "Uzbek proverbs", "Tashkent traffic", "coffee and tea", "the Olympics", "wizards", "cars", "music",
)


def ai_request(cheer: Cheer, theme: str, recent: list[str]) -> str:
    if cheer.outcome == "done":
        facts = [f"They approved {cheer.count} review(s)."]
        if cheer.wait is not None:
            facts.append(f"The fastest one waited {format_wait(cheer.wait)} in the queue: "
                         f"{TIER_DESCRIPTIONS[cheer.tier]}.")
    else:
        facts = [f"They sent {cheer.count} PR(s) back to the author with requested changes."]
        if cheer.bounce_round > 1:
            facts.append(f"One of them has now been sent back {cheer.bounce_round} times.")
        else:
            facts.append("It is the first time this PR was sent back.")
    lines = facts + [f"Optional theme for the joke: {theme}."]
    if recent:
        lines.append("Don't repeat these recent messages or their jokes:\n" + "\n".join(f"- {line}" for line in recent))
    return "\n".join(lines)


class CheerWriter:
    """Writes quips from the free line library, or with Claude when configured, falling back to the
    library on any problem."""

    def __init__(self, client=None, model: Optional[str] = None, rng: Optional[random.Random] = None):
        self.client = client
        self.model = model or os.getenv("CHEERS_MODEL", DEFAULT_MODEL)
        self.rng = rng or random.Random()
        # Template lines each person got recently, so the library avoids repeating them.
        self.recent_lines: dict[object, deque] = {}
        # The quips each person actually saw, so Claude avoids repeating them.
        self.recent_quips: dict[object, deque] = {}
        # Public lines each chat saw recently, by pool, and when each chat was last congratulated.
        self.recent_public_lines: dict[tuple[int, str], deque] = {}
        self.last_empty_celebration: dict[int, datetime] = {}

    @classmethod
    def from_environment(cls) -> "CheerWriter":
        client = None
        if os.getenv("ANTHROPIC_API_KEY") and os.getenv("CHEERS_AI", "on").lower() not in ("0", "off", "false", "no"):
            try:
                import anthropic
                # A cheer that arrives long after the command is pointless; fall back to the library instead.
                client = anthropic.AsyncAnthropic(timeout=AI_TIMEOUT_SECONDS, max_retries=0)
            except ImportError:
                logger.warning("ANTHROPIC_API_KEY is set but the anthropic package is missing; using templates")
        return cls(client)

    async def quip(self, cheer: Cheer, person) -> str:
        seen = self.recent_quips.setdefault(person, deque(maxlen=RECENT_PER_PERSON))
        text = None
        if self.client is not None:
            try:
                text = await self.ai_quip(cheer, list(seen)[-5:])
            except Exception:
                logger.exception("Claude's cheer could not be read; using the line library")
        if not text:
            lines = self.recent_lines.setdefault(person, deque(maxlen=RECENT_PER_PERSON))
            text = template_quip(cheer, self.rng, lines)
        seen.append(text)
        return html_escape(text, quote=False)

    async def ai_quip(self, cheer: Cheer, recent: list[str]) -> Optional[str]:
        import anthropic
        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=AI_MAX_TOKENS,
                output_config={"effort": "low"},
                system=AI_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": ai_request(cheer, self.rng.choice(AI_THEMES), recent)}],
            )
        except anthropic.APIStatusError as error:
            logger.warning("Claude could not write a cheer (HTTP %s): %s", error.status_code, error.message)
            return None
        except anthropic.APIError as error:
            # Connection problems, timeouts and unexpected response bodies.
            logger.warning("Claude could not write a cheer: %r", error)
            return None
        # A refusal or a cut-off reply falls back to the library, so no server-side fallback model is needed.
        if response.stop_reason != "end_turn":
            logger.warning("Claude stopped writing a cheer early: %s", response.stop_reason)
            return None
        text = " ".join(block.text.strip() for block in response.content if block.type == "text").strip().strip('"«»')
        if not text or len(text) > MAX_QUIP_LENGTH:
            return None
        return text

    # Public messages always come from the free library, never Claude, so they stay predictable.

    def public_line(self, chat_id: int, name: str, pool: dict[str, list[str]],
                    opener_mood: Optional[str] = None) -> str:
        recent = self.recent_public_lines.setdefault((chat_id, name), deque(maxlen=RECENT_PER_PERSON))
        return html_escape(public_quip(pool, self.rng, recent, opener_mood), quote=False)

    def call_to_action(self, chat_id: int) -> str:
        return f"👉 {self.public_line(chat_id, 'cta', CALL_TO_ACTION)} {ADD_COMMAND}"

    def empty_queue_listing(self, chat_id: int) -> str:
        """The !w reply for an empty queue, in HTML."""
        return (f"📭 No tasks in the queue.\n{self.public_line(chat_id, 'empty', EMPTY_QUEUE, 'hype')}\n"
                f"{self.call_to_action(chat_id)}")

    def nothing_to_review(self, chat_id: int, outcome: str) -> str:
        """The reply to !wdone or !wbounce when the queue is already empty, in HTML."""
        action = "mark done" if outcome == "done" else "send back"
        return (f"📭 The queue is empty, so there's nothing to {action}.\n"
                f"{self.public_line(chat_id, 'nothing', NOTHING_TO_REVIEW, 'tease')}\n{self.call_to_action(chat_id)}")

    def empty_queue_celebration(self, chat_id: int, closer: Optional[str], now: datetime) -> Optional[str]:
        """The public note after the last review leaves the queue, or None if this chat had one recently."""
        last = self.last_empty_celebration.get(chat_id)
        if last is not None and now - last < EMPTY_QUEUE_COOLDOWN:
            return None
        self.last_empty_celebration[chat_id] = now
        headline = "🎉 <b>Queue is empty!</b>"
        if closer:
            headline += f" {html_escape(closer)} closed the last one."
        return (f"{headline}\n{self.public_line(chat_id, 'empty', EMPTY_QUEUE, 'hype')}\n"
                f"{self.call_to_action(chat_id)}")

    async def message(self, cheer: Cheer, person) -> str:
        return f"{header(cheer)}\n{await self.quip(cheer, person)}"
