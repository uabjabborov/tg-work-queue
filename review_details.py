"""Fetch PR/MR titles and descriptions from GitHub and the configured GitLab host."""

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urlsplit

import httpx

from review_urls import parse_review_url

logger = logging.getLogger(__name__)

# Upper bound for one lookup, including connecting; callers wait for it before replying.
REQUEST_TIMEOUT = 4.0
# Results are reused so repeated listings don't use up GitHub's rate limit, and unreachable
# or private PRs are not retried on every listing.
SUCCESS_TTL = 10 * 60
FAILURE_TTL = 15 * 60
CACHE_SIZE = 1024
DESCRIPTION_LIMIT = 1000
HTML_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
BLANK_LINES_PATTERN = re.compile(r"\n\s*\n\s*\n+")


@dataclass(frozen=True)
class ReviewDetails:
    title: str
    description: str


# (API URL, whether a token was sent) -> (monotonic time, result). Token results are kept
# apart so a private title fetched for a trusted chat is never served to another one.
_cache: dict[tuple[str, bool], tuple[float, Optional[ReviewDetails]]] = {}


def _cached(key: tuple[str, bool]) -> tuple[bool, Optional[ReviewDetails]]:
    entry = _cache.get(key)
    if entry is None:
        return False, None
    stored_at, details = entry
    if time.monotonic() - stored_at < (SUCCESS_TTL if details else FAILURE_TTL):
        return True, details
    del _cache[key]
    return False, None


def _remember(key: tuple[str, bool], details: Optional[ReviewDetails]) -> None:
    now = time.monotonic()
    _cache.pop(key, None)
    _cache[key] = (now, details)
    if len(_cache) > CACHE_SIZE:
        for stale in [k for k, (at, d) in _cache.items() if now - at >= (SUCCESS_TTL if d else FAILURE_TTL)]:
            del _cache[stale]
    while len(_cache) > CACHE_SIZE:
        del _cache[next(iter(_cache))]  # Oldest entry first.


def chat_may_use_tokens(chat_id: int) -> bool:
    """Tokens unlock private PRs for anyone in the chat, so only listed team chats may use them."""
    allowed = os.getenv("ALLOWED_CHAT_IDS", "")
    return str(chat_id) in {part.strip() for part in allowed.split(",")}


def review_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=False)


def clean_description(description: Optional[str]) -> str:
    """Drop template comments and spare blank lines so previews start with content."""
    text = HTML_COMMENT_PATTERN.sub("", (description or "").replace("\r\n", "\n"))
    text = BLANK_LINES_PATTERN.sub("\n\n", text).strip()
    return text[:DESCRIPTION_LIMIT]


def _api_request(url: str, use_tokens: bool) -> tuple[str, dict[str, str], str] | None:
    """Return the API URL, headers, and description field for a supported PR/MR link.

    Only github.com and the host in GITLAB_URL are contacted, so chat members cannot
    point the bot at arbitrary servers or receive a token meant for another host. The GitHub
    token is used only for owners in GITHUB_ORGS; anyone in the chat can read what it unlocks.
    """
    parsed = parse_review_url(url)
    if parsed is None:
        return None
    _, project, number = parsed[1]
    # Compare hostnames, not netlocs, so an explicit default port still matches.
    hostname = urlsplit(url).hostname

    if hostname == "github.com":
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        owner = project.split("/")[1].lower()
        allowed_owners = {org.strip().lower() for org in os.getenv("GITHUB_ORGS", "").split(",")}
        if use_tokens and (token := os.getenv("GITHUB_TOKEN")) and owner in allowed_owners:
            headers["Authorization"] = f"Bearer {token}"
        return f"https://api.github.com/repos{project}/pulls/{number}", headers, "body"

    gitlab_url = os.getenv("GITLAB_URL", "https://gitlab.com").rstrip("/")
    if urlsplit(gitlab_url).hostname != hostname:
        return None
    headers = {}
    if use_tokens and (token := os.getenv("GITLAB_TOKEN")):
        headers["PRIVATE-TOKEN"] = token
    project_id = quote(project.lstrip("/"), safe="")
    return f"{gitlab_url}/api/v4/projects/{project_id}/merge_requests/{number}", headers, "description"


async def fetch_review_details(client: httpx.AsyncClient, url: str,
                               use_tokens: bool = False) -> Optional[ReviewDetails]:
    """Return the PR/MR title and description, or None when they cannot be fetched.

    Pass use_tokens only for trusted requests, such as chats allowed by chat_may_use_tokens.
    """
    request = _api_request(url, use_tokens)
    if request is None:
        return None
    api_url, headers, description_field = request
    key = (api_url, bool({"Authorization", "PRIVATE-TOKEN"} & headers.keys()))
    found, details = _cached(key)
    if found:
        return details
    try:
        response = await asyncio.wait_for(client.get(api_url, headers=headers), REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        title = (data.get("title") or "").strip()
        if not title:
            raise ValueError("response has no title")
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError, AttributeError) as error:
        logger.warning("Could not fetch details for %s: %r", url, error)
        _remember(key, None)
        return None
    details = ReviewDetails(title=title, description=clean_description(data.get(description_field)))
    _remember(key, details)
    return details
