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
# Unreachable or private PRs are not retried on every listing.
FAILURE_COOLDOWN = 15 * 60
DESCRIPTION_LIMIT = 1000
HTML_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
BLANK_LINES_PATTERN = re.compile(r"\n\s*\n\s*\n+")


@dataclass(frozen=True)
class ReviewDetails:
    title: str
    description: str


# API URL -> monotonic time of the last failed lookup.
_recent_failures: dict[str, float] = {}


def review_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=False)


def clean_description(description: Optional[str]) -> str:
    """Drop template comments and spare blank lines so previews start with content."""
    text = HTML_COMMENT_PATTERN.sub("", (description or "").replace("\r\n", "\n"))
    text = BLANK_LINES_PATTERN.sub("\n\n", text).strip()
    return text[:DESCRIPTION_LIMIT]


def _api_request(url: str) -> tuple[str, dict[str, str], str] | None:
    """Return the API URL, headers, and description field for a supported PR/MR link.

    Only github.com and the host in GITLAB_URL are contacted, so chat members cannot
    point the bot at arbitrary servers or receive a token meant for another host. The GitHub
    token is used only for owners in GITHUB_ORGS; anyone in the chat can read what it unlocks.
    """
    parsed = parse_review_url(url)
    if parsed is None:
        return None
    host, project, number = parsed[1]

    if host == "github.com":
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        owner = project.split("/")[1].lower()
        allowed_owners = {org.strip().lower() for org in os.getenv("GITHUB_ORGS", "").split(",")}
        if (token := os.getenv("GITHUB_TOKEN")) and owner in allowed_owners:
            headers["Authorization"] = f"Bearer {token}"
        return f"https://api.github.com/repos{project}/pulls/{number}", headers, "body"

    gitlab_url = os.getenv("GITLAB_URL", "https://gitlab.com").rstrip("/")
    if urlsplit(gitlab_url).netloc.lower() != host:
        return None
    headers = {}
    if token := os.getenv("GITLAB_TOKEN"):
        headers["PRIVATE-TOKEN"] = token
    project_id = quote(project.lstrip("/"), safe="")
    return f"{gitlab_url}/api/v4/projects/{project_id}/merge_requests/{number}", headers, "description"


async def fetch_review_details(client: httpx.AsyncClient, url: str) -> Optional[ReviewDetails]:
    """Return the PR/MR title and description, or None when they cannot be fetched."""
    request = _api_request(url)
    if request is None:
        return None
    api_url, headers, description_field = request
    failed_at = _recent_failures.get(api_url)
    if failed_at is not None and time.monotonic() - failed_at < FAILURE_COOLDOWN:
        return None
    try:
        response = await asyncio.wait_for(client.get(api_url, headers=headers), REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        title = (data.get("title") or "").strip()
        if not title:
            raise ValueError("response has no title")
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError, AttributeError) as error:
        logger.warning("Could not fetch details for %s: %r", url, error)
        _recent_failures[api_url] = time.monotonic()
        return None
    _recent_failures.pop(api_url, None)
    return ReviewDetails(title=title, description=clean_description(data.get(description_field)))
