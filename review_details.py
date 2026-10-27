"""Fetch PR/MR titles and descriptions from GitHub and the configured GitLab host."""

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urlsplit

import httpx

from review_urls import parse_review_url

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 5.0
DESCRIPTION_LIMIT = 1000
HTML_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
BLANK_LINES_PATTERN = re.compile(r"\n\s*\n\s*\n+")


@dataclass(frozen=True)
class ReviewDetails:
    title: str
    description: str


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
    point the bot at arbitrary servers or receive a token meant for another host.
    """
    parsed = parse_review_url(url)
    if parsed is None:
        return None
    host, project, number = parsed[1]

    if host == "github.com":
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token := os.getenv("GITHUB_TOKEN"):
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
    try:
        response = await client.get(api_url, headers=headers)
        response.raise_for_status()
        data = response.json()
        title = (data.get("title") or "").strip()
    except (httpx.HTTPError, ValueError, AttributeError) as error:
        logger.warning("Could not fetch details for %s: %s", url, error)
        return None
    if not title:
        return None
    return ReviewDetails(title=title, description=clean_description(data.get(description_field)))
